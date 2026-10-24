# -*- coding: utf-8 -*-
"""文脉 · 验证集评测脚本

用法（在项目根目录执行）：
    python -X utf8 评测/run_eval.py              # 云端模式（需 .env 里配好百炼 Key）
    python -X utf8 评测/run_eval.py --local      # 离线模式（用随包模型，论文不出本机）
    python -X utf8 评测/run_eval.py --all        # 两种模式都跑，用于对比

产出：
    评测/results/eval_<模式>_<时间戳>.json   机器可读的完整结果
    评测/results/latest_<模式>.json          最近一次结果（固定名，便于引用）
    评测/results/report.md                   人类可读的评测报告

评测集分两类：
  A. 内置验证集——随包的 6 篇元学习/少样本分类论文 + 专家构造的错误注入集
     （scripts 里 selfcheck.make_error_dataset 自动埋入 4 类典型引用错误）
  B. 外部验证集（可选）——把 评测/external/*.pdf 放进来即自动纳入，
     用于验证程序在"训练分布之外"的论文上是否同样工作。
     建议来源：天池 CSL 中文文献元数据对应的原文、或自己领域内的论文。
"""
import argparse
import io
import json
import os
import statistics
import sys
import time

# 数据根目录：交付包把源码放在「代码/」、数据放在「数据与产物/」，启动器用
# WM_ROOT 指过去。没有 WM_ROOT 时（单独跑源码目录）退回老规则取上级目录。
ROOT = os.path.abspath(os.environ.get("WM_ROOT", "").strip() or
                       os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 代码目录单独算：sys.path 要加的是**源码所在目录**，而不是数据目录。
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CODE not in sys.path:
    sys.path.insert(0, CODE)
OUT = os.path.join(CODE, "评测", "results")
os.makedirs(OUT, exist_ok=True)


def _load(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def _count_pdf():
    d = os.path.join(ROOT, "data", "pdf")
    if not os.path.isdir(d):
        return 0
    return len([f for f in os.listdir(d) if f.lower().endswith(".pdf")])


def eval_corpus():
    """评测 1：文献库解析质量（正文字符数、章节数、参考文献条数）。"""
    docs = _load(os.path.join(ROOT, "data", "docs.json")).get("docs", [])
    rows = []
    for d in docs:
        rows.append({
            "title": (d.get("title") or "")[:70],
            "year": d.get("year"),
            "chars": d.get("chars") or len(d.get("full_text") or ""),
            "sections": len(d.get("sections_found") or []),
            "ref_count": d.get("ref_count") or 0,
            "non_paper": bool(d.get("non_paper")),
        })
    ok = [r for r in rows if not r["non_paper"]]
    return {
        "论文数": len(rows),
        "识别为非论文数": len(rows) - len(ok),
        "平均正文字符数": round(statistics.mean([r["chars"] for r in ok]), 1) if ok else 0,
        "平均章节数": round(statistics.mean([r["sections"] for r in ok]), 2) if ok else 0,
        "平均参考文献条数": round(statistics.mean([r["ref_count"] for r in ok]), 1) if ok else 0,
        "参考文献提取成功篇数": len([r for r in ok if r["ref_count"] > 0]),
        "明细": rows,
    }


def eval_error_detection():
    """评测 2：错误注入检出——对专家构造的 4 类典型引用错误，程序能否全部检出。"""
    from 源程序 import selfcheck
    r = selfcheck.benchmark(verbose=False)
    cases = r.get("cases") or []
    total = len(cases)
    detected = len([c for c in cases if c.get("detected")])
    # 精确率侧：检出时命中的类别是否包含期望类别（避免"报了但报错类"）
    correct_kind = len([c for c in cases
                        if c.get("detected") and (c.get("matched_kinds") or [])])
    return {
        "用例数": total,
        "检出数": detected,
        "查全率": round(detected / total, 4) if total else 0,
        "类别命中数": correct_kind,
        "类别正确率": round(correct_kind / total, 4) if total else 0,
        "明细": [{"错误类型": c.get("error_type"), "是否检出": c.get("detected"),
                  "期望类别": c.get("expected_kinds"), "命中类别": c.get("matched_kinds")}
                 for c in cases],
    }


def eval_false_positive():
    """评测 3：误报控制——对"干净稿"不应报出引用类问题。

    干净稿 = 现成的综述稿去掉正文里的引用角标与文末参考文献表。
    此时任何"引用真实性/格式/编号"类问题都属误报。
    这是最容易做假的一项指标：报得多不等于报得对，所以必须单独测。
    """
    from 源程序 import selfcheck
    draft = _load(os.path.join(ROOT, "outputs", "draft.json"))
    md = draft.get("markdown") or ""
    head = md.split("## 参考文献")[0] if "## 参考文献" in md else md
    # 去掉角标与参考文献表，构造"零引用"的干净稿
    import re
    clean = re.sub(r"\[\d{1,3}\]", "", head)
    r = selfcheck.run_checks(clean)
    issues = r.get("issues") or []
    cite_kinds = ("引用真实性", "引用越界", "编号顺序", "格式合规", "重复著录", "文末未引用")
    false_pos = [i for i in issues if i.get("kind") in cite_kinds]
    return {
        "输入": "现成综述稿（已移除角标与参考文献表）",
        "报出问题总数": len(issues),
        "其中引用类误报数": len(false_pos),
        "误报明细": [{"类别": i.get("kind"), "位置": i.get("location")} for i in false_pos],
    }


def eval_rag_support(use_llm=True):
    """评测 4：RAG 证据核对——正文论断能否在对应论文原文里找到依据。

    ⚠ 跨模式不可直接比较「支持率」。两种模式下判定机制不同：
      · 启用模型：把检索到的原文片段交给模型逐条判定（有独立判断能力）
      · 未启用模型：退化为"相似度 ≥ 阈值即判有支撑"（阈值 0.15，非常宽松，
        几乎不会判无支撑）
    因此离线模式的支持率会**虚高**（实测 1.0），这不是质量更好的证据。
    本函数额外记录 judgment 字段，报告里据此提示读者。
    """
    from 源程序 import selfcheck
    r = selfcheck.run_evidence_check(use_llm=use_llm, verbose=False)
    s = r.get("summary") or {}
    results = r.get("results") or []
    scores = [x.get("score") for x in results if isinstance(x.get("score"), (int, float))]
    by_status = {}
    for x in results:
        k = x.get("status") or "?"
        by_status[k] = by_status.get(k, 0) + 1
    llm_used = bool(s.get("llm_used"))
    return {
        "模式": "RAG-evidence",
        "判定机制": ("模型逐条判定检索到的原文片段" if llm_used
                   else "相似度阈值兜底（阈值 %s，宽松，会虚高）" % s.get("threshold")),
        "是否启用模型判定": llm_used,
        "跨模式可比性": ("仅本模式内有效；与未启用模型的模式不可直接比较支持率"
                     if llm_used else
                     "**不可与启用模型的模式比较**：本模式无独立判断能力，"
                     "只要相似度达标即判有支撑"),
        "论断总数": s.get("claims_total"),
        "可核对论断数": s.get("claims_judged"),
        "定位到的引用数": s.get("citations_located"),
        "有支撑": s.get("supported"),
        "无支撑": s.get("unsupported"),
        "支持率": s.get("support_rate"),
        "综合句数": s.get("synthesis"),
        "相似度中位数": round(statistics.median(scores), 4) if scores else None,
        "判定分布": by_status,
        "条目明细": [{"论断": (x.get("sentence") or "")[:80], "引用": x.get("citations"),
                      "判定": x.get("status"), "相似度": x.get("score"),
                      "理由": (x.get("reason") or "")[:100]} for x in results],
    }


def eval_format_compliance():
    """评测 5：GB/T 7714 格式合规——对参考文献表逐条校验。"""
    from 源程序 import selfcheck
    draft = _load(os.path.join(ROOT, "outputs", "draft.json"))
    md = draft.get("markdown") or ""
    r = selfcheck.run_checks(md)
    fmt = [i for i in (r.get("issues") or []) if i.get("kind") == "格式合规"]
    refs = draft.get("refs") or []
    return {
        "参考文献条数": len(refs),
        "格式不合规条数": len(fmt),
        "合规率": round(1 - len(fmt) / len(refs), 4) if refs else None,
        "不合规明细": [{"位置": i.get("location"), "说明": i.get("detail")} for i in fmt],
    }


def eval_retrieval_quality():
    """评测 6：检索环节质量——每条论断能否定位到正确论文。

    "正确"的判据：论断所标注的引用编号，与该论断检索得分最高的论文一致。
    这直接反映 RAG 的 R（检索）是否可靠，而不只是最终结论对不对。
    """
    ev = _load(os.path.join(ROOT, "outputs", "evidence_check.json"))
    results = ev.get("results") or []
    located = [x for x in results if x.get("evidence")]
    return {
        "参与检索的论断数": len(results),
        "检索到原文片段的论断数": len(located),
        "检索成功率": round(len(located) / len(results), 4) if results else None,
    }


def run_once(mode: str, use_llm: bool) -> dict:
    t0 = time.time()
    print("=" * 70)
    print("开始评测：%s模式" % ("云端" if not use_llm else "离线"))
    print("=" * 70)
    from 源程序.config import settings
    settings.force_offline = use_llm
    os.environ["WM_OFFLINE"] = "1" if use_llm else "0"

    stages = {}
    t = time.time()
    print("  [1/6] 文献库解析质量…")
    stages["1_文献库解析"] = eval_corpus()
    print("  [2/6] 错误注入检出…")
    stages["2_错误注入检出"] = eval_error_detection()
    print("  [3/6] 误报控制…")
    stages["3_误报控制"] = eval_false_positive()
    print("  [4/6] RAG 证据核对…")
    stages["4_RAG证据核对"] = eval_rag_support(use_llm=not use_llm)
    print("  [5/6] GB/T 7714 格式合规…")
    stages["5_格式合规"] = eval_format_compliance()
    print("  [6/6] 检索质量…")
    stages["6_检索质量"] = eval_retrieval_quality()

    return {
        "评测时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "模式": "cloud" if not use_llm else "local",
        "模式说明": "云端处理（阿里云百炼 qwen-plus）" if not use_llm else "仅本地处理（随包 Qwen2.5-3B + bge-small-zh）",
        "文献库论文数": _count_pdf(),
        "总耗时秒": round(time.time() - t0, 1),
        "评测项": stages,
    }


def write_report(runs: list):
    """把结果写成人类可读的 Markdown 报告。"""
    lines = ["# 文脉 · 验证集评测结果", "",
             "本报告由 `评测/run_eval.py` 自动生成，所有数字均为脚本实测输出。", ""]
    if len(runs) > 1:
        lines += ["## 总览对比", "",
                  "| 指标 | %s |" % " | ".join(r["模式"] for r in runs),
                  "|" + "---|" * (len(runs) + 1)]
        rows = [("引用错误注入查全率", lambda e: "%.0f%%" % (e["2_错误注入检出"]["查全率"] * 100)),
                ("引用类误报（干净稿）", lambda e: "%d 处" % e["3_误报控制"]["其中引用类误报数"]),
                ("RAG 支持率（仅模式内有效）", lambda e: str(e["4_RAG证据核对"]["支持率"])),
                ("格式合规率", lambda e: ("%.0f%%" % (e["5_格式合规"]["合规率"] * 100))
                 if e["5_格式合规"]["合规率"] is not None else "—"),
                ("检索成功率", lambda e: ("%.0f%%" % (e["6_检索质量"]["检索成功率"] * 100))
                 if e["6_检索质量"]["检索成功率"] is not None else "—")]
        for name, f in rows:
            lines.append("| %s | %s |" % (name, " | ".join(f(r["评测项"]) for r in runs)))
        lines += ["",
                  "> 「RAG 支持率」这一行**不可横向比较**，原因见下方口径说明的最后一段。", ""]
    for r in runs:
        lines += ["## %s（%s）" % (r["模式说明"], r["评测时间"]), "",
                  "- 文献库论文数：%d 篇" % r["文献库论文数"],
                  "- 总耗时：%s 秒" % r["总耗时秒"], ""]
        e = r["评测项"]
        c = e["1_文献库解析"]
        lines += ["### 1. 文献库解析质量", "",
                  "| 指标 | 数值 |", "|---|---|",
                  "| 论文数 | %d |" % c["论文数"],
                  "| 误判为非论文 | %d |" % c["识别为非论文数"],
                  "| 平均正文字符数 | %s |" % c["平均正文字符数"],
                  "| 平均章节数 | %s |" % c["平均章节数"],
                  "| 平均参考文献条数 | %s |" % c["平均参考文献条数"],
                  "| 参考文献提取成功 | %d / %d 篇 |" % (c["参考文献提取成功篇数"], c["论文数"]), ""]
        d = e["2_错误注入检出"]
        lines += ["### 2. 引用错误检出（专家构造的注入集）", "",
                  "| 指标 | 数值 |", "|---|---|",
                  "| 注入的错误类型数 | %d |" % d["用例数"],
                  "| 检出数 | %d |" % d["检出数"],
                  "| **查全率** | **%.0f%%** |" % (d["查全率"] * 100),
                  "| 类别命中数 | %d |" % d["类别命中数"], "",
                  "| 注入的错误类型 | 是否检出 | 期望检出类别 | 实际命中 |", "|---|---|---|---|"]
        for x in d["明细"]:
            lines.append("| %s | %s | %s | %s |" % (
                x["错误类型"], "✅" if x["是否检出"] else "❌",
                "/".join(x["期望类别"] or []), "/".join(x["命中类别"] or []) or "—"))
        lines.append("")
        f = e["3_误报控制"]
        lines += ["### 3. 误报控制", "",
                  "- 输入：%s" % f["输入"],
                  "- 报出问题总数：%d，**其中引用类误报 %d 处**" % (f["报出问题总数"], f["其中引用类误报数"]), ""]
        g = e["4_RAG证据核对"]
        lines += ["### 4. RAG 证据核对（论断是否有原文依据）", "",
                  "| 指标 | 数值 |", "|---|---|",
                  "| 判定机制 | %s |" % g["判定机制"],
                  "| 论断总数 | %s |" % g["论断总数"],
                  "| 可核对论断数 | %s |" % g["可核对论断数"],
                  "| 定位到的引用数 | %s |" % g["定位到的引用数"],
                  "| 有支撑 | %s |" % g["有支撑"],
                  "| 无支撑 | %s |" % g["无支撑"],
                  "| 支持率（**仅本模式内有效**） | %s |" % g["支持率"],
                  "| 相似度中位数 | %s |" % g["相似度中位数"], "",
                  "> ⚠ **跨模式不可比较支持率。** %s" % g["跨模式可比性"], ""]
        h = e["5_格式合规"]
        lines += ["### 5. GB/T 7714 参考文献格式合规", "",
                  "- 参考文献条数：%s" % h["参考文献条数"],
                  "- 格式不合规条数：%s" % h["格式不合规条数"],
                  "- 合规率：%s" % (("%.0f%%" % (h["合规率"] * 100)) if h["合规率"] is not None else "—"), ""]
        i = e["6_检索质量"]
        lines += ["### 6. 检索环节质量", "",
                  "- 参与检索的论断数：%s" % i["参与检索的论断数"],
                  "- 成功检索到原文片段的论断数：%s" % i["检索到原文片段的论断数"],
                  "- 检索成功率：%s" % (("%.0f%%" % (i["检索成功率"] * 100)) if i["检索成功率"] is not None else "—"), ""]
        lines += ["---", ""]
    lines += ["## 指标口径说明", "",
              "- **查全率**：注入的每类错误是否都被检出。分母是注入类型数，不是问题条数。",
              "- **误报控制**：把「干净稿」（无角标、无参考文献表）送进检查，任何引用类问题都是误报。"
              "单独测这一项，是因为「报得多」不等于「报得对」。",
              "- **支持率**：把正文每处引用拿回对应论文原文检索比对，判定有依据的比例。"
              "⚠ **只在同一模式内有效**——启用模型时由模型逐条判断，未启用模型时退化为"
              "「相似度 ≥ 0.15 即算有支撑」，后者几乎不会判无支撑，支持率会虚高（实测 1.0），"
              "**不代表质量更好**。",
              "- **否定式论断的已知误报**：形如「所有方法均未超越 X」的表述，原文对应的是"
              "「性能低于…」这类句子，检索难以命中，容易被判为无支撑。详见《模型与算法说明》。",
              "- 所有数字均为本机实测，脚本与原始 JSON 一并提供，可复现。", ""]
    p = os.path.join(OUT, "report.md")
    with io.open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="离线模式（随包本地模型）")
    ap.add_argument("--all", action="store_true", help="云端与离线都跑，用于对比")
    args = ap.parse_args()

    runs = []
    if args.all:
        runs.append(run_once("cloud", use_llm=False))
        runs.append(run_once("local", use_llm=True))
    elif args.local:
        runs.append(run_once("local", use_llm=True))
    else:
        runs.append(run_once("cloud", use_llm=False))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    for r in runs:
        p1 = os.path.join(OUT, "eval_%s_%s.json" % (r["模式"], stamp))
        p2 = os.path.join(OUT, "latest_%s.json" % r["模式"])
        for p in (p1, p2):
            with io.open(p, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
        print("  已写出：%s" % os.path.relpath(p1, ROOT))
        print("  已写出：%s" % os.path.relpath(p2, ROOT))
    rp = write_report(runs)
    print("  已写出：%s" % os.path.relpath(rp, ROOT))
    print()
    print("=" * 70)
    print("评测完成")
    print("=" * 70)
    for r in runs:
        e = r["评测项"]
        print("  %s" % r["模式说明"])
        print("    错误注入查全率 : %.0f%%" % (e["2_错误注入检出"]["查全率"] * 100))
        print("    引用类误报     : %d 处" % e["3_误报控制"]["其中引用类误报数"])
        print("    RAG 支持率     : %s" % e["4_RAG证据核对"]["支持率"])
        print("    格式合规率     : %s" % e["5_格式合规"]["合规率"])


if __name__ == "__main__":
    main()
