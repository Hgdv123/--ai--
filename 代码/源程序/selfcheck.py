"""引用自检：7 项检查 + 埋错测试集。

七项检查：
1. 引用真实性   —— 正文角标 [n] 在文末参考文献表中是否存在
2. 文末未引用   —— 文末条目在正文中从未出现
3. 编号顺序     —— 角标是否按首次出现顺序递增（顺序编码制）
4. 格式合规     —— 逐条对照 GB/T 7714
5. 重复著录     —— 同一文献被著录两次 / 编号重复
6. 论断有据     —— 陈述句缺少引用支撑
7. 引用越界     —— 编号超出参考文献表范围

埋错测试集（用于评测自检能力）：
故意向正文/文献表注入已知错误（错编号顺序、假引用、缺文献类型标识、重复著录），
再验证自检能否全部检出 —— 指标客观、可复现、无法造假。
"""
from __future__ import annotations

import json
import os
import re
import time

from . import gbt7714
from .config import CHECK_JSON, DRAFT_MD, OUT_DIR, ensure_dirs


def _split_draft(markdown: str) -> dict:
    """把草稿拆成正文与参考文献段。"""
    m = re.search(r"(?m)^#{1,3}\s*(?:参考文献|References)\s*$", markdown)
    if m:
        body = markdown[:m.start()]
        refs_block = markdown[m.end():]
    else:
        body, refs_block = markdown, ""
    body = re.sub(r"(?m)^#{1,3}.*$", "", body)  # 去掉标题行
    return {"body": body.strip(), "refs_block": refs_block.strip()}


def _parse_refs(refs_block: str) -> list:
    out = []
    for line in refs_block.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[(\d{1,3})\]\s*(.+)$", line)
        if m:
            out.append({"index": int(m.group(1)), "text": m.group(2).strip()})
    return out


def _cited_order(body: str) -> list:
    """正文中角标按出现顺序（含重复），并把 [1-5] 这类合并编号展开。"""
    out = []
    for m in re.finditer(r"\[(\d{1,3})(?:\s*[-,–]\s*(\d{1,3}))?\]", body):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if b < a:
            a, b = b, a
        out.extend(range(a, b + 1))
    return out


# 合法引用写法：[1] [1-5] [1–5]，以及逗号连写 [1,2]
CITATION_PAT = re.compile(r"\[\d{1,3}(?:\s*[-,–]\s*\d{1,3})*\]")


def _norm_title_ref(text: str) -> str:
    """取条目的题名部分用于重复检测。"""
    body = re.sub(r"^\[\d+\]\s*", "", text)
    body = re.sub(r"^[^.]{2,200}?\.\s*", "", body, count=1)  # 去作者
    m = re.split(r"\[[A-Z]{1,2}(?:/OL)?\]", body)
    return re.sub(r"[\s\W]+", "", m[0]).lower()[:60]


# ── 四态判定 ─────────────────────────────────────────────
# 判定必须区分"没找到依据"和"原文说的正好相反"：
#   · 找不到依据可能只是检索没命中（论文用了别的措辞），报成"引用错误"会误伤；
#   · 原文明确矛盾才是硬结论——正文说"提升 10 倍"、原文写的是 2 倍，
#     这种必须报，而且要给到最高严重度。
# 两者混为一谈，用户就分不清"你去核对一下"和"你写错了"。
ST_SUPPORTED = "supported"        # 有依据：原文找到相关段落
ST_NOT_EVIDENCE = "not_evidence"  # 找不到依据：检索到段落，但它在讲别的
ST_CONTRADICTED = "contradicted"  # 原文明确矛盾：数值或方向与原文冲突
ST_UNVERIFIABLE = "unverifiable"  # 无法核对：原文不在库里，或没检索到任何段落

STATUS_LABEL = {
    ST_SUPPORTED: "有依据",
    ST_NOT_EVIDENCE: "找不到依据",
    ST_CONTRADICTED: "原文明确矛盾",
    ST_UNVERIFIABLE: "无法核对",
}

# 旧报告里用的是 unresolved / weak / unsupported。读旧产物时归一到新名，
# 否则同一份历史报告在不同版本下会被解释成不同的结论。
_LEGACY_STATUS = {
    "unresolved": ST_UNVERIFIABLE,
    "unsupported": ST_NOT_EVIDENCE,
    "weak": ST_NOT_EVIDENCE,
}


def _norm_status(s: str) -> str:
    """把（可能来自旧报告的）状态名归一到四态之一。"""
    v = (s or "").strip()
    if v in STATUS_LABEL:
        return v
    return _LEGACY_STATUS.get(v, ST_UNVERIFIABLE)

# 增大/减小方向词。中英并列，避免只认一种语言时漏判。
_UP_WORDS = ("提升", "提高", "上升", "增加", "增大", "增强", "改善", "加快",
             "更高", "更高", "优于", "超过", "超越",
             "higher", "increase", "improve", "better", "outperform",
             "surpass", "gain", "greater", "faster")
_DOWN_WORDS = ("降低", "下降", "减少", "减小", "减弱", "变差", "更低", "低于",
               "劣于", "不如", "退化", "恶化",
               "lower", "decrease", "reduce", "reduction", "worse",
               "degrade", "less", "slower")
# 否定词：出现在方向词前后都会翻转语义（"未提升" / "not improve"）
_NEG_WORDS = ("未", "没有", "没", "无", "不", "非", "未能", "尚未",
              "not", "no", "never", "without", "fail")

# 数值：倍数、百分比、带单位的量。刻意不抓裸数字——年份、编号、页码全是裸数字，
# 抓进来只会制造误报。
_QUANT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(倍|%|％|个百分点|"
    r"percentage\s*points?|percent|times?\b|x\b)", re.I)
def _dir_numbers(text: str) -> set:
    """找出"被方向词描述的那个数值"的集合。

    做法是**按距离配对**，不用正则把方向词和数字粘在一起写死：
    "优于基线 20%"（中文 6 字）和 "is 20% worse than the baseline"（英文 22 字符）
    都是同一件事，用固定的"中间隔 N 个字符"去卡，必然漏掉一种语言。
    句子边界（。！？.!?）会截断配对——跨句的数字不该被算成同一个量。
    """
    out = set()
    s = text or ""
    marks = [m.start() for m in re.finditer(r"[。！？；;.!?]", s)]
    for dm in re.finditer("|".join(re.escape(w) for w in _UP_WORDS + _DOWN_WORDS),
                          s, re.I):
        for qm in _QUANT_RE.finditer(s):
            lo, hi = sorted((dm.end(), qm.start()))
            gap = s[lo:hi]
            # 中文信息密度高，14 字就够；英文按字符算要宽一些（约 40）。
            limit = 14 if re.search(r"[\u4e00-\u9fff]", gap) else 40
            if hi - lo > limit:
                continue
            if any(lo < p < hi for p in marks):
                continue
            out.add(_norm_num(float(qm.group(1))))
    return out


def _norm_num(v: float) -> float:
    return round(float(v), 4)


def _quantity_kinds(text: str) -> set:
    return {_kind_of(m.group(2)) for m in _QUANT_RE.finditer(text or "")}


# 单位同义归一：中文"倍"与英文 times/x 必须归成同一个量纲，
# 否则"提升 10 倍"和"by 2 times"会被当成两个不同量纲、漏掉最该报的那类矛盾。
_KIND_ALIASES = {
    "倍": "倍", "倍.": "倍", "折": "倍",
    "times": "倍", "time": "倍", "x": "倍", "×": "倍",
    "%": "%", "％": "%", "percent": "%", "percentage": "%",
    "个百分点": "个百分点", "percentage point": "个百分点",
    "percentage points": "个百分点",
}


def _kind_of(raw: str) -> str:
    k = (raw or "").strip().lower().replace("％", "%")
    return _KIND_ALIASES.get(k, _KIND_ALIASES.get(k.rstrip("s"), k))


def _directions(text: str) -> dict:
    """提取方向词 → 是否被否定。否定靠"方向词前后 4 字内出现否定词"判断。"""
    out = {}
    s = (text or "").lower()
    for words, sign in ((_UP_WORDS, 1), (_DOWN_WORDS, -1)):
        for w in words:
            for m in re.finditer(re.escape(w.lower()), s):
                lo, hi = max(0, m.start() - 4), min(len(s), m.end() + 4)
                window = s[lo:hi]
                negated = any(n in window for n in _NEG_WORDS)
                out[m.start()] = (sign, negated, w)
    return out


def _same_language(a: str, b: str) -> bool:
    """两段文本是否以同一种语言为主（按中文字符占比判断）。

    跨语言比对数值/方向会大量误报——中文"2 倍"在英文原文里可能写成
    "two times"，数字形态对不上，判成矛盾就是冤枉用户。
    """
    def zh_ratio(s):
        s = s or ""
        if not s:
            return 0.0
        return len(re.findall(r"[\u4e00-\u9fff]", s)) / len(s)
    return (zh_ratio(a) > 0.15) == (zh_ratio(b) > 0.15)


def find_contradiction(claim: str, evidence: str, max_chars: int = 6000) -> list:
    """确定性地找出"原文与论断冲突"的证据，返回冲突原因列表（空 = 没发现冲突）。

    只做能站得住的判断，宁可漏报也不误报：
      · **同量纲不同数值**：都说"倍"，一个 10 倍一个 2 倍 → 冲突；
      · **数值在原文里完全找不到**：论断给了具体数值，检索到的原文里没有它；
      · **方向相反**：一个说提升、一个说降低，且两边在同一段证据里同时出现
        （同时出现说明确实在讲同一件事，不是各说各的）。
    刻意不做"裸数字比对"：年份、编号、页码都是裸数字，比它只会制造误报。
    """
    reasons = []
    ev = (evidence or "")[:max_chars]
    if not claim or not ev:
        return reasons

    # ① 同量纲数值对比：双方都给出倍数/百分比时，直接比数值
    def by_kind(text):
        d = {}
        for m in _QUANT_RE.finditer(text or ""):
            d.setdefault(_kind_of(m.group(2)), set()).add(_norm_num(float(m.group(1))))
        return d

    cq, eq = by_kind(claim), by_kind(ev)
    for kind in set(cq) & set(eq):
        if cq[kind] and eq[kind] and not (cq[kind] & eq[kind]):
            reasons.append("数值不一致：论断提到 %s，原文对应位置是 %s"
                           % ("/".join("%g%s" % (v, kind) for v in sorted(cq[kind])),
                              "/".join("%g%s" % (v, kind) for v in sorted(eq[kind]))))

    # ② 论断的数值在原文里完全找不到 —— **仅限同语言**时才敢这么判。
    # 跨语言时这条会大量误报：中文论断写"提升 2 倍"，英文原文写的可能是
    # "two times" 或 "doubles"，数字形态根本不同，判成矛盾就是冤枉用户。
    same_lang = _same_language(claim, ev)
    if same_lang:
        ev_nums = {_norm_num(float(m.group(1))) for m in _QUANT_RE.finditer(ev)}
        for m in _QUANT_RE.finditer(claim or ""):
            v = _norm_num(float(m.group(1)))
            if v not in ev_nums:
                reasons.append("论断给出的数值 %g%s 未出现在检索到的原文片段中"
                               % (v, m.group(2)))

    # ③ 方向相反的同类表述（只比"方向词直接描述的那个量"）
    ev_dir_nums = _dir_numbers(ev)
    claim_dir_text = " ".join((claim or "").split())
    if ev_dir_nums:
        cdirs = _directions(claim_dir_text)
        edirs = _directions(ev)
        for v in _dir_numbers(claim):
            if v not in ev_dir_nums:
                continue
            for _, (cs, cneg, cw) in cdirs.items():
                hit = False
                for _, (es, eneg, ew) in edirs.items():
                    if cneg == eneg and cs != es:
                        reasons.append("方向相反：论断说「%s」，原文说「%s」" % (cw, ew))
                        hit = True
                        break
                if hit:
                    break
    return reasons[:3]


def run_checks(markdown: str, docs: list = None, evidence: dict = None,
               missing: dict = None) -> dict:
    """执行七项检查（可并入 RAG 证据核对与"该引未引"结果），返回 {issues, summary}。

    evidence: check_claim_support() 的返回值。
    missing:  suggest_missing_citations() 的返回值。
    """
    parts = _split_draft(markdown)
    body, refs = parts["body"], _parse_refs(parts["refs_block"])
    ref_by_idx = {r["index"]: r for r in refs}
    order = _cited_order(body)
    first_seen = []
    for n in order:
        if n not in first_seen:
            first_seen.append(n)
    cited_set = set(order)
    ref_idx_set = set(ref_by_idx.keys())

    issues = []

    def add(kind, severity, location, detail, suggestion):
        issues.append({"kind": kind, "severity": severity, "location": location,
                       "detail": detail, "suggestion": suggestion})

    # 1. 引用真实性
    for n in sorted(cited_set - ref_idx_set):
        add("引用真实性", "高", "正文", "角标 [%d] 在参考文献表中不存在" % n,
            "补全该文献条目，或删除该处引用")

    # 2. 文末未引用
    for n in sorted(ref_idx_set - cited_set):
        add("文末未引用", "中", "参考文献表", "条目 [%d] 在正文中从未被引用" % n,
            "在正文相应位置引用该文献，或从表中删除")

    # 3. 编号顺序（顺序编码制：首次出现应递增）
    for i in range(1, len(first_seen)):
        if first_seen[i] < first_seen[i - 1]:
            add("编号顺序", "中", "正文第 %d 处引用" % (i + 1),
                "角标顺序为 %s —— [%d] 出现在 [%d] 之前，不符合顺序编码制" % (
                    ",".join(str(x) for x in first_seen[:max(i + 1, 6)]),
                    first_seen[i], first_seen[i - 1]),
                "按首次出现顺序重新编号，或调整正文引用位置")
            break

    # 4. 格式合规（逐条 GB/T 7714）
    for r in refs:
        probs = gbt7714.check_entry("[%d] %s" % (r["index"], r["text"]))
        for p in probs:
            add("格式合规", "中" if "标识" not in p else "高", "条目 [%d]" % r["index"],
                p, "按 GB/T 7714 补全该字段")

    # 5. 重复著录
    seen, dup = {}, []
    for r in refs:
        key = _norm_title_ref(r["text"])
        if len(key) < 6:
            continue
        if key in seen:
            dup.append((seen[key], r["index"]))
        else:
            seen[key] = r["index"]
    for a, b in dup:
        add("重复著录", "中", "条目 [%d] 与 [%d]" % (a, b),
            "两处指向同一篇文献", "合并为一条并统一编号")
    idx_list = [r["index"] for r in refs]
    if len(idx_list) != len(set(idx_list)):
        add("重复著录", "高", "参考文献表", "存在重复编号", "编号必须唯一")

    # 6. 论断有据：句长足够但没有角标的陈述句
    # 注意：[1-5]、[3,4] 这类合并写法同样是合法引用，不能误判为"无引用"
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", body) if len(s.strip()) >= 25]
    bare = []
    for s in sentences:
        if CITATION_PAT.search(s):
            continue
        if s.startswith("#") or s.startswith("|"):
            continue
        bare.append(s[:70])
    for s in bare[:6]:
        add("论断有据", "中", "正文", "该陈述没有引用支撑：「%s…」" % s,
            "补充引用来源，或改写为不依赖文献的表述")
    if len(bare) > 6:
        add("论断有据", "低", "正文", "另有 %d 处陈述缺少引用" % (len(bare) - 6),
            "同上一并处理")

    # 7. 引用越界（编号远超表内最大编号）
    if refs:
        max_idx = max(ref_idx_set) if ref_idx_set else 0
        for n in sorted(cited_set):
            if n > max_idx:
                add("引用越界", "高", "正文", "角标 [%d] 超出参考文献表范围（最大 [%d]）" % (n, max_idx),
                    "核对编号或补全缺失条目")

    # 8. RAG 证据核对结果并入（回原文比对，比"有没有角标"更硬）
    # 四态分开报：原文明确矛盾是硬结论；找不到依据只是"检索没命中"，
    # 两者混在一起，用户分不清"你去核对一下"和"你写错了"。
    if evidence and evidence.get("results"):
        for r in evidence["results"]:
            st = _norm_status(r.get("status"))
            cite = "引用 [%s]" % r.get("citation")
            page = (r.get("page_range") or r.get("page") or "").strip()
            where = "（证据在第 %s 页）" % page if page else ""
            if st == ST_CONTRADICTED:
                add("原文明确矛盾", "高", cite,
                    "%s%s" % ((r.get("reason") or "原文与该论断冲突")[:90], where),
                    "这是硬结论：正文的数值或方向与原文不一致，"
                    "请以原文为准修改论断，或更换引用文献")
            elif st == ST_NOT_EVIDENCE:
                add("论断有据（回原文核对）", "高", cite,
                    "原文中找不到支撑：%s%s" % ((r.get("reason") or "")[:80], where),
                    "核对原文后修正该论断，或更换引用文献")
            elif st == "weak":       # 旧结果兼容
                add("论断有据（回原文核对）", "中", cite,
                    "%s" % (r.get("reason") or "")[:90],
                    "人工确认该引用是否贴切")
            elif st == ST_UNVERIFIABLE:
                # 关键：这一类**不是**引用出错，而是"原文不在库里，核对不了"。
                # 若与"找不到依据"混在一起报，用户会去改自己的引用，
                # 而真正的问题其实是解析失败或没导入原文。
                add("原文缺失（无法核对）", "低", cite,
                    "%s" % (r.get("reason") or "未能在已导入文献中定位该引用")[:90],
                    "这不是引用写错了：把该文献的原文（PDF/Word）导入文献库，"
                    "或到「管理已放入的论文」看它是否解析失败，之后重新检查即可核对")

    # 9. 该引未引（需要文献支撑却未标注引用的陈述）
    if missing and missing.get("suggestions"):
        for s in missing["suggestions"][:8]:
            cand = s.get("candidates") or []
            tip = ""
            if cand:
                tip = "建议引用：%s（相关度 %.2f）" % ((cand[0].get("paper") or "")[:40],
                                                     cand[0].get("score", 0))
            else:
                tip = "未找到明显相关的已导入文献；若为作者自身主张，可不加引用"
            add("该引未引", "中", "正文", "该陈述需要文献支撑但未标注引用：「%s…」" % s["sentence"][:60],
                tip)

    severity_rank = {"高": 0, "中": 1, "低": 2}
    issues.sort(key=lambda x: (severity_rank.get(x["severity"], 3), x["kind"]))

    bare_in_issues = sum(1 for i in issues if i["kind"] == "论断有据")
    # 统计正文中所有缺引用的陈述句总数（可与"论断有据"问题数区分）
    all_bare = 0
    for s in sentences:
        if not CITATION_PAT.search(s) and not s.startswith(("#", "|")):
            all_bare += 1

    summary = {
        "total": len(issues),
        "high": sum(1 for i in issues if i["severity"] == "高"),
        "medium": sum(1 for i in issues if i["severity"] == "中"),
        "low": sum(1 for i in issues if i["severity"] == "低"),
        "by_kind": {},
        "refs_count": len(refs),
        "citations_count": len(order),
        "bare_sentences_total": all_bare,
        "bare_reported": bare_in_issues,
        "evidence_check": (evidence or {}).get("summary", {}),
        "missing_citations": (missing or {}).get("total", 0),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    for i in issues:
        summary["by_kind"][i["kind"]] = summary["by_kind"].get(i["kind"], 0) + 1
    return {"issues": issues, "summary": summary,
            "body": body, "refs": refs}


def self_check(markdown: str = None, verbose: bool = True) -> dict:
    ensure_dirs()
    if markdown is None:
        if not os.path.isfile(DRAFT_MD):
            raise RuntimeError("尚未生成综述草稿，请先执行 generate")
        with open(DRAFT_MD, encoding="utf-8") as f:
            markdown = f.read()

    # 先做回原文的证据核对（RAG 环节），结果并入检查报告
    evidence = None
    try:
        evidence = run_evidence_check(use_llm=True, verbose=verbose)
    except Exception as e:
        # 打印异常类型与位置：曾经这里只印一句 str(e)，结果一个格式化异常
        # 让整个证据核对被静默跳过，界面上只是"没有核对结果"，查了很久。
        if verbose:
            print("[证据核对/RAG] 跳过（%s: %s）" % (type(e).__name__, str(e)[:120]))
    if evidence is None and verbose:
        print("[证据核对/RAG] 未执行（可能缺少已解析论文）")

    # 再做"该引未引"检测
    missing = None
    try:
        missing = run_missing_citation_check(verbose=verbose)
    except Exception as e:
        if verbose:
            print("[该引未引] 跳过（%s）" % str(e)[:80])

    result = run_checks(markdown, evidence=evidence, missing=missing)
    result["evidence"] = evidence
    result["missing_citations"] = missing
    with open(CHECK_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if verbose:
        s = result["summary"]
        print("[自检] 共发现 %d 处问题（高 %d / 中 %d / 低 %d）" % (
            s["total"], s["high"], s["medium"], s["low"]))
        for i in result["issues"][:10]:
            print("   [%s] %s —— %s" % (i["severity"], i["kind"], i["detail"][:70]))
        print("[自检] 报告已写出：%s" % CHECK_JSON)
    return result


# ══════════════════ RAG 环节：论断 → 回原文检索证据 ══════════════════

# 论断与原文段落的相似度阈值（字符 n-gram 哈希向量，量级偏小）
# 经验值：>=0.15 视为原文明确相关，直接判"有支撑"（省一次模型调用）
#         0.15 以下一律交模型核对 —— 检索相似度低也可能是表述差异，不能只凭分数定罪
SUPPORT_THRESHOLD = 0.15
SUPPORT_BORDERLINE = 0.02

def _judge_by_llm(claim: str, evidence: str, fallback_reason: str,
                  client=None, llm_on: bool = False) -> tuple:
    """把检索到的原文片段交给模型判定。返回 (status, reason)。

    提示词刻意**三态**且保守：只有原文明确冲突才判 contradicted；
    拿不准时判 not_supported（"找不到依据"），不升级成"引用错误"。
    误报几次用户就再也不信这个工具，宁可漏报。
    """
    if not llm_on:
        return ST_UNVERIFIABLE, fallback_reason
    prompt = (
        "下面是一篇（或数篇）论文的原文片段，以及综述中的一句话。\n"
        "请判断：这些片段与该句是什么关系？\n\n"
        "判定要点：\n"
        "1. 判 supported（有依据）的门槛是：**片段确实讲到了该句的核心内容**。"
        "综述句子常是综合概括，措辞不必与原文一致，但必须能在片段里指出"
        "支撑该句的那句话（同一概念、同一实验、同一结论）。"
        "**只是同一大主题不算有依据**——例如句子说「X 方法把准确率提升了一成」，"
        "而片段只是泛泛提到「X 方法是元学习方法」，这不算有依据。\n"
        "2. 片段在讲别的事情、或只有主题词重合而没有实质内容 → "
        "判 not_supported（找不到依据）。\n"
        "3. **只有片段明确否定了该论断时**才判 contradicted（原文明确矛盾），"
        "例如：论断说「提升 10 倍」而原文写的是 2 倍；"
        "论断说「提升了准确率」而原文说的是下降；论断说「原文未讨论」而原文讨论了。"
        "**片段只是没提到该点，不算矛盾**，必须回到第 2 条判 not_supported。\n"
        "4. 拿不准时判 not_supported，不要判 contradicted —— "
        "把「没找到」说成「写错了」会误导用户去改本来正确的引用。\n\n"
        "只输出 JSON。\n\n原文片段：\n%(ev)s\n\n综述句子：\n%(claim)s\n\n"
        '输出：{"status": "supported" 或 "not_supported" 或 "contradicted", '
        '"reason": "不超过40字：判 supported 要写出片段里支撑该句的原话要点；'
        '判 contradicted 要写明原文的原话或数值；判 not_supported 要写片段实际在讲什么"}'
    ) % {"ev": evidence[:1400], "claim": claim[:320]}
    # 用关键字占位而不是位置占位：提示词正文里会出现"提升 10%"这类字面百分号，
    # 位置式 %-格式化会把它当格式符解析并抛异常，而调用方往往把异常吞掉，
    # 结果是整个证据核对被静默跳过（实测踩到过）。
    data, meta = client.chat_json(
        [{"role": "system", "content": "你是学术事实核查助手，只输出 JSON。"},
         {"role": "user", "content": prompt}], max_tokens=260)
    if data and data.get("status") in ("supported", "not_supported", "contradicted"):
        st = {"supported": ST_SUPPORTED, "not_supported": ST_NOT_EVIDENCE,
              "contradicted": ST_CONTRADICTED}[data["status"]]
        return st, str(data.get("reason") or "")[:60]
    # 兼容旧字段名（模型偶尔仍按旧格式回 supported: true/false）
    if data and "supported" in data:
        return (ST_SUPPORTED if data.get("supported") else ST_NOT_EVIDENCE), \
               str(data.get("reason") or "")[:60]
    return ST_UNVERIFIABLE, fallback_reason


def iterate_sentences(body: str, min_len: int = 25) -> list:
    """拆出正文里的**全部**陈述句（不论有没有引用）。

    为什么需要它：decompose_claims() 末尾有 `if cites:` 过滤，只返回**带引用**的
    句子，于是"论断有据"这一检查项永远看不到"完全没有引用"的句子——而"漏引"
    恰恰是最该报的问题。此外，段落若不以句末标点结尾（Word 稿子常见），
    原正则会把整段丢掉。

    返回 [{sentence, position, citations}]，citations 可为空列表。
    """
    out = []
    if not body:
        return out
    pos = 0
    for line in body.split("\n"):
        line_len = len(line) + 1
        s_line = line.strip()
        if not s_line or s_line.startswith(("#", "|", ">", "-", "*")):
            pos += line_len
            continue
        # 段内按句末标点切；末尾无标点的残段也保留（用 $ 分支）
        for m in re.finditer(r"[^。！？!?；;]{%d,}(?:[。！？!?；;]|$)" % min_len, s_line):
            s = m.group(0).strip()
            if not s:
                continue
            cites = []
            for cm in re.finditer(r"\[(\d{1,3})(?:\s*[-,–]\s*(\d{1,3}))*\]", s):
                a = int(cm.group(1))
                b = int(cm.group(2)) if cm.group(2) else a
                if b < a:
                    a, b = b, a
                for n in range(a, b + 1):
                    if n not in cites:
                        cites.append(n)
            out.append({"sentence": s, "position": pos + m.start(), "citations": cites})
        pos += line_len
    return out


def decompose_claims(body: str, min_len: int = 25) -> list:
    """把正文拆成"带有引用的论断"。

    返回 [{sentence, position, citations}]，citations 已展开（[1-3] → [1,2,3]）。
    """
    out = []
    for m in re.finditer(r"[^。！？\n]{%d,}[。！？]" % min_len, body):
        s = m.group(0).strip()
        if s.startswith(("#", "|")):
            continue
        cites = []
        for cm in re.finditer(r"\[(\d{1,3})(?:\s*[-,–]\s*(\d{1,3}))*\]", s):
            a = int(cm.group(1))
            b = int(cm.group(2)) if cm.group(2) else a
            if b < a:
                a, b = b, a
            for n in range(a, b + 1):
                if n not in cites:
                    cites.append(n)
        if cites:
            out.append({"sentence": s, "position": m.start(), "citations": cites})
    return out


def build_docs_index(docs: list):
    """为已解析论文建立证据检索索引。返回 (EvidenceRetriever, {key: doc})。"""
    from .retrieval import EvidenceRetriever
    usable = [d for d in docs
              if not d.get("parse_error") and (d.get("full_text") or d.get("abstract_pdf"))]
    if not usable:
        return None, {}
    retr = EvidenceRetriever(usable)
    by_key = {}
    for i, d in enumerate(usable):
        key = d.get("id") or d.get("local_pdf") or ("doc-%d" % i)
        by_key[key] = d
    return retr, by_key


def _key_for_citation(num: int, title_by_idx: dict, doc_of_index: dict):
    """把正文里的引用编号映射到论文库中的记录。"""
    t = (title_by_idx.get(num) or "").strip().lower()
    if not t:
        return None, None
    for k, d in doc_of_index.items():
        dt = (d.get("title") or "").strip().lower()
        if not dt:
            continue
        if dt == t or dt[:40] == t[:40] or dt[:25] in t or t[:25] in dt:
            return k, d
    return None, None


def check_claim_support(body: str, draft: dict, retr, doc_of_index: dict,
                        use_llm: bool = True, verbose: bool = False) -> dict:
    """核心 RAG 校验：正文每一处引用 → 回对应论文原文检索证据 → 判断是否成立。

    三类结论：
      supported   原文找到相关段落，相似度达标
      weak        相似度偏低，建议人工复核（可选交模型判定）
      unsupported 原文找不到相关段落 —— 该引用疑似错配或虚构
    """
    from .llm import LLMClient

    claims = decompose_claims(body)
    refs = draft.get("refs") or []
    title_by_idx = {}
    for i, r in enumerate(refs, 1):
        title_by_idx[r.get("index", i)] = r.get("title")

    client = LLMClient() if use_llm else None
    llm_on = bool(client and client.available)

    # 中文论断检索英文论文时，中文关键词几乎匹配不上原文。
    # 这里先把论断翻成英文检索词（一次调用覆盖全部论断，成本极低），再参与检索。
    en_queries = {}
    if llm_on and claims:
        numbered = "\n".join("%d. %s" % (i + 1, c["sentence"][:180]) for i, c in enumerate(claims))
        data, _ = client.chat_json([
            {"role": "system", "content": "你是学术检索助手，只输出 JSON。"},
            {"role": "user", "content":
                "下面每条是中文文献综述里的论断。请为每条给出 6 个英文检索关键词（"
                "要覆盖其中的专业术语与概念，用空格分隔）。\n"
                "输出 JSON：{\"1\": \"keyword1 keyword2 ...\", \"2\": \"...\", ...}\n\n" + numbered}
        ], max_tokens=1200)
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    en_queries[int(k)] = str(v)
                except Exception:
                    continue
        if verbose and en_queries:
            print("[证据核对/RAG] 已生成 %d 条英文检索词（用于中文论断检索英文论文）" % len(en_queries))

    results, n_checked = [], 0
    for ci, c in enumerate(claims):
        nums = c["citations"]
        extra = en_queries.get(ci + 1)
        # 收集该句涉及的全部论文及其检索结果
        per_num = []
        for num in nums:
            key, doc = _key_for_citation(num, title_by_idx, doc_of_index)
            hits = retr.find(key, c["sentence"], top_k=2,
                             extra_queries=[extra] if extra else None) if (key and retr) else []
            per_num.append({"citation": num, "key": key, "doc": doc, "hits": hits})

        located = [p for p in per_num if p["key"]]
        if not located:
            # 无法核对 ≠ 引用写错了：原文不在库里（只导入了题录、或没上传原文）。
            # 这一类绝不能升级成"引用错误"，否则用户会去改本来正确的引用。
            for p in per_num:
                results.append({"sentence": c["sentence"][:130], "citation": p["citation"],
                                "title": None, "status": ST_UNVERIFIABLE, "score": 0.0,
                                "evidence": None, "page": "", "page_range": "",
                                "reason": "该文献的原文不在文献库中，无法回原文核对"})
            continue

        n_checked += len(located)

        # 单篇被引：按该篇判定；多篇被引（综合句）：合并证据一起判定
        if len(located) == 1:
            p = located[0]
            top = p["hits"][0] if p["hits"] else None
            entry = {"sentence": c["sentence"][:130], "citation": p["citation"],
                     "title": (p["doc"] or {}).get("title"),
                     "evidence": (top or {}).get("chunk", "")[:220],
                     "page": (top or {}).get("page", ""),
                     "page_range": (top or {}).get("page_range", ""),
                     "score": round((top or {}).get("score", 0.0), 4)}
            if not top:
                # 库里有全文却一个相关段落都没检索到：给不出依据，
                # 但不能断定矛盾（也可能只是措辞差异导致漏检）。
                entry["status"] = ST_NOT_EVIDENCE
                entry["reason"] = "该论文原文中未检索到任何相关段落"
            else:
                # 先做确定性矛盾检查：命中冲突就直接判"原文明确矛盾"。
                # 这是唯一能给出硬结论的路径，不交给模型自由裁量。
                reasons = find_contradiction(c["sentence"], top["chunk"])
                if reasons:
                    entry["status"] = ST_CONTRADICTED
                    entry["reason"] = "；".join(reasons)[:110]
                    entry["contradiction"] = reasons
                elif not llm_on and entry["score"] >= SUPPORT_THRESHOLD:
                    # 无模型时只能用相似度兜底（并在报告里说明判定依据较弱）
                    entry["status"] = ST_SUPPORTED
                    entry["reason"] = "原文找到相关段落（相似度 %.3f；未启用模型判定）" % entry["score"]
                else:
                    st, rs = _judge_by_llm(
                        c["sentence"], top["chunk"],
                        "相似度 %.3f，未启用模型判定，建议人工复核" % entry["score"],
                        client=client, llm_on=llm_on)
                    entry["status"], entry["reason"] = st, rs
            results.append(entry)
        else:
            pooled = "\n---\n".join(
                "[%d] %s" % (p["citation"], p["hits"][0]["chunk"])
                for p in located if p["hits"])
            best_hits = [p["hits"][0] for p in located if p["hits"]]
            best = max((h.get("score", 0.0) for h in best_hits), default=0.0)
            best_hit = max(best_hits, key=lambda h: h.get("score", 0.0)) if best_hits else {}
            if not pooled:
                for p in located:
                    results.append({"sentence": c["sentence"][:130], "citation": p["citation"],
                                    "title": (p["doc"] or {}).get("title"),
                                    "status": ST_NOT_EVIDENCE, "score": 0.0,
                                    "evidence": None, "page": "", "page_range": "",
                                    "reason": "该论文原文中未检索到任何相关段落"})
                continue
            reasons = find_contradiction(c["sentence"], pooled)
            if reasons:
                st, rs = ST_CONTRADICTED, "；".join(reasons)[:110]
            elif not llm_on and best >= SUPPORT_THRESHOLD:
                st, rs = ST_SUPPORTED, "综合 %d 篇文献的原文找到相关段落（最高相似度 %.3f）" % (
                    len(located), best)
            else:
                st, rs = _judge_by_llm(
                    c["sentence"], pooled,
                    "综合 %d 篇文献的原文相似度最高 %.3f，未启用模型判定，建议人工复核" % (
                        len(located), best),
                    client=client, llm_on=llm_on)
            # 综合句按整体结论记一条，同时列出涉及的编号
            results.append({"sentence": c["sentence"][:130],
                            "citation": nums[0], "citations": nums,
                            "title": "、".join((p["doc"] or {}).get("title", "")[:24]
                                             for p in located)[:80],
                            "status": st, "score": round(best, 4),
                            "evidence": pooled[:260], "reason": rs,
                            "page": best_hit.get("page", ""),
                            "page_range": best_hit.get("page_range", ""),
                            "contradiction": reasons or None,
                            "synthesis": True})

    judged = len(results)
    n_contra = sum(1 for r in results if r["status"] == ST_CONTRADICTED)
    n_not = sum(1 for r in results if r["status"] == ST_NOT_EVIDENCE)
    n_unver = sum(1 for r in results if r["status"] == ST_UNVERIFIABLE)
    summary = {
        "mode": "RAG-evidence",
        "claims_total": len(claims),                       # 正文中带引用的论断句数
        "citations_located": n_checked,                    # 成功定位到论文的引用处数
        "claims_judged": judged,                           # 实际给出结论的条目数
        "supported": sum(1 for r in results if r["status"] == ST_SUPPORTED),
        "contradicted": n_contra,
        "not_evidence": n_not,
        "unverifiable": n_unver,
        # 兼容字段：unsupported 记"没能给出依据"的总数（含明确矛盾）；
        # weak 不再产生新值，保留键位以免旧的下游读取报错。
        "unsupported": n_contra + n_not,
        "weak": 0,
        "unresolved": n_unver,
        "synthesis": sum(1 for r in results if r.get("synthesis")),
        "llm_used": llm_on,
        "threshold": SUPPORT_THRESHOLD,
        "status_labels": dict(STATUS_LABEL),
    }
    # 支持率的分母只算"真能核对"的条目：无法核对的不算通过、也不算失败。
    # 把无法核对的算进分母会虚低，算进分子会虚高，两者都是自欺。
    verifiable = summary["supported"] + n_contra + n_not
    summary["support_rate"] = round(summary["supported"] / verifiable, 4) if verifiable else 0.0
    if verbose:
        print("[证据核对/RAG] 论断 %d 句 → 核对引用 %d 处 → 判定 %d 条："
              "有依据 %d / 找不到依据 %d / 原文明确矛盾 %d / 无法核对 %d（可核对范围内支持率 %.2f）"
              % (len(claims), n_checked, judged, summary["supported"], n_not,
                 n_contra, n_unver, summary["support_rate"]))
    return {"summary": summary, "results": results}


def run_evidence_check(use_llm: bool = True, verbose: bool = True) -> dict:
    """独立入口：读草稿与已解析论文，执行回原文的证据核对，报告落盘。"""
    ensure_dirs()
    from .draft import load_draft
    from .literature import load_docs

    draft = load_draft()
    if not draft:
        raise RuntimeError("尚未生成综述草稿，请先执行 draft")
    docs = load_docs().get("docs", [])
    retr, by_key = build_docs_index(docs)
    if retr is None:
        res = {"summary": {"error": "没有可用的已解析论文，无法回原文核对",
                           "citations_checked": 0, "support_rate": 0.0}, "results": []}
    else:
        parts = _split_draft(draft.get("markdown", ""))
        res = check_claim_support(parts["body"], draft, retr, by_key,
                                  use_llm=use_llm, verbose=verbose)
    with open(os.path.join(OUT_DIR, "evidence_check.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    if verbose:
        print("[证据核对/RAG] 报告已写出：%s" % os.path.join(OUT_DIR, "evidence_check.json"))
    return res


# ══════════════════ Agent 可调用的原子工具 ══════════════════
# 这些函数把已有能力拆成"单次调用"粒度，供 Agent 按需选择。

def tool_list_sentences(markdown: str = None) -> dict:
    """工具：列出正文句子（带编号与引用），供 Agent 挑选可疑项。"""
    md = markdown
    if md is None:
        if not os.path.isfile(DRAFT_MD):
            return {"error": "尚未生成综述草稿"}
        with open(DRAFT_MD, encoding="utf-8") as f:
            md = f.read()
    parts = _split_draft(md)
    refs = _parse_refs(parts["refs_block"])
    sents = []
    for i, s in enumerate([x.strip() for x in re.split(r"(?<=[。！？])", parts["body"]) if len(x.strip()) >= 15], 1):
        cites = [int(x) for x in re.findall(r"\[(\d{1,3})\]", s)]
        sents.append({"no": i, "text": s[:200], "citations": cites})
    return {"sentences": sents, "total": len(sents),
            "references": [{"index": r["index"], "text": r["text"][:160]} for r in refs]}


def tool_get_sentence(no: int, markdown: str = None) -> dict:
    """工具：按编号取某一句。"""
    d = tool_list_sentences(markdown)
    for s in d.get("sentences", []):
        if s["no"] == no:
            return s
    return {"error": "第 %s 句不存在" % no}


def tool_reference_by_index(idx: int, markdown: str = None) -> dict:
    """工具：按编号取参考文献条目。"""
    d = tool_list_sentences(markdown)
    for r in d.get("references", []):
        if r["index"] == idx:
            return r
    return {"error": "参考文献 [%s] 不存在" % idx}


def tool_check_citation_existence(markdown: str = None) -> dict:
    """工具：检查引用真实性（角标 ↔ 文末条目是否一致）。"""
    md = markdown
    if md is None:
        with open(DRAFT_MD, encoding="utf-8") as f:
            md = f.read()
    parts = _split_draft(md)
    refs = {r["index"] for r in _parse_refs(parts["refs_block"])}
    cited = set(_cited_order(parts["body"]))
    return {"phantom": sorted(cited - refs),          # 引用了但表中没有
            "never_cited": sorted(refs - cited),      # 表中有但正文没引
            "total_citations": len(cited), "total_refs": len(refs)}


def tool_check_ordering(markdown: str = None) -> dict:
    """工具：检查编号顺序是否符合顺序编码制。"""
    md = markdown
    if md is None:
        with open(DRAFT_MD, encoding="utf-8") as f:
            md = f.read()
    parts = _split_draft(md)
    seq = _cited_order(parts["body"])
    first = []
    for n in seq:
        if n not in first:
            first.append(n)
    bad = [(first[i - 1], first[i]) for i in range(1, len(first)) if first[i] < first[i - 1]]
    return {"first_appearance_order": first, "violations": bad,
            "ok": not bad}


def tool_check_format(markdown: str = None) -> dict:
    """工具：逐条检查 GB/T 7714 格式。"""
    md = markdown
    if md is None:
        with open(DRAFT_MD, encoding="utf-8") as f:
            md = f.read()
    parts = _split_draft(md)
    out = []
    for r in _parse_refs(parts["refs_block"]):
        probs = gbt7714.check_entry("[%d] %s" % (r["index"], r["text"]))
        if probs:
            out.append({"index": r["index"], "problems": probs, "text": r["text"][:140]})
    return {"entries_checked": len(_parse_refs(parts["refs_block"])),
            "entries_with_problems": out, "ok": not out}


def _en_keywords(sentence: str, llm_on: bool = True) -> list:
    """把中文论断翻成英文检索词。

    中文概括检索英文论文时，字符 n-gram 几乎匹配不上原文；
    实测不带扩展会漏掉关键证据（例如把"自蒸馏"相关内容检索不到），
    进而把真实引用误判为"无支撑"。
    """
    if not sentence:
        return []
    if llm_on:
        try:
            from .llm import LLMClient
            c = LLMClient()
            if c.available:
                data, _ = c.chat_json([
                    {"role": "system", "content": "你是学术检索助手，只输出 JSON。"},
                    {"role": "user", "content":
                        "把下面这句中文综述论断转成 8 个英文检索关键词（覆盖其中的专业术语与概念，空格分隔）。\n"
                        '只输出 JSON：{"keywords": "..."}\n\n' + sentence[:400]}], max_tokens=200)
                if isinstance(data, dict) and data.get("keywords"):
                    return [str(data["keywords"])]
        except Exception:
            pass
    # 无模型时：抽出论断中的英文词作为兜底
    en = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", sentence)
    return [" ".join(en[:10])] if en else []


def tool_find_evidence(citation: int, sentence: str = "", use_llm: bool = False,
                       extra_queries: list = None) -> dict:
    """工具：针对某处引用回到原文检索证据（RAG 检索环节）。

    sentence 省略时自动从草稿里找包含该编号的句子。
    extra_queries 省略时自动生成英文检索词（中文论断检索英文论文必需）。
    """
    from .draft import load_draft
    from .literature import load_docs

    draft = load_draft()
    if not draft:
        return {"error": "尚未生成综述草稿"}
    if not sentence:
        d = tool_list_sentences()
        for s in d.get("sentences", []):
            if citation in s.get("citations", []):
                sentence = s["text"]
                break
    if not sentence:
        return {"error": "草稿中找不到引用 [%s] 的句子" % citation}

    docs = load_docs().get("docs", [])
    retr, by_key = build_docs_index(docs)
    if retr is None:
        return {"error": "没有可用的已解析论文，无法回原文检索"}

    refs = draft.get("refs") or []
    title_by_idx = {r.get("index", i): r.get("title") for i, r in enumerate(refs, 1)}
    key, doc = _key_for_citation(citation, title_by_idx, by_key)
    if not key:
        return {"error": "未能在论文库中定位文献 [%s]（可能只导入了题录，没有原文）" % citation}

    if extra_queries is None:
        extra_queries = _en_keywords(sentence, llm_on=use_llm)
    hits = retr.find(key, sentence, top_k=4, extra_queries=extra_queries)
    return {
        "citation": citation,
        "paper": (doc or {}).get("title"),
        "sentence": sentence[:200],
        # 给足原文长度：截得太短会把证据切掉，导致模型误判"原文未提及"
        # page 是页码（PDF 才有；Word 稿或定位不到时为空串）。
        # 用户要能照着页码翻过去自己看，否则"有支撑"只是一句无法验证的断言。
        "evidence": [{"text": h["chunk"][:900], "score": round(h["score"], 4),
                      "page": h.get("page") or "",
                      "page_range": h.get("page_range") or ""} for h in hits],
        "best_score": round(hits[0]["score"], 4) if hits else 0.0,
        "en_queries": extra_queries,
    }


def tool_check_claim_support(citation: int, sentence: str = "", use_llm: bool = True) -> dict:
    """工具：判断某处引用是否真有原文支撑（RAG 检索 + 四态判定）。

    与 check_claim_support 用同一套判定：先做**确定性矛盾检查**，
    命中即判"原文明确矛盾"（不交给模型自由裁量），否则再交模型判有依据/找不到依据。
    """
    ev = tool_find_evidence(citation, sentence, use_llm=use_llm)
    if ev.get("error"):
        return ev
    if not ev.get("evidence"):
        # 库里有全文却检索不到段落：给不出依据，但不能断定矛盾。
        return {"citation": citation, "status": ST_NOT_EVIDENCE,
                "reason": "该论文原文中未检索到相关段落", "score": 0.0,
                "paper": ev.get("paper"), "status_label": STATUS_LABEL[ST_NOT_EVIDENCE]}
    # 合并前若干段证据一起判断（单段常不完整）
    evidence = "\n\n---\n\n".join(
        "【片段%d｜相似度 %.3f】\n%s" % (i, e["score"], e["text"])
        for i, e in enumerate(ev["evidence"][:3], 1))
    top = ev["evidence"][0]
    base = {"citation": citation, "score": top["score"], "paper": ev.get("paper"),
            "page": top.get("page") or "", "page_range": top.get("page_range") or ""}

    reasons = find_contradiction(ev.get("sentence") or "", evidence)
    if reasons:
        base.update({"status": ST_CONTRADICTED, "reason": "；".join(reasons)[:110],
                     "contradiction": reasons})
        base["status_label"] = STATUS_LABEL[ST_CONTRADICTED]
        return base

    if use_llm:
        from .llm import LLMClient
        c = LLMClient()
        if c.available:
            st, rs = _judge_by_llm(ev.get("sentence") or "", evidence,
                                   "相似度 %.3f" % top["score"],
                                   client=c, llm_on=True)
            if st != ST_UNVERIFIABLE:
                base.update({"status": st, "reason": rs, "status_label": STATUS_LABEL[st]})
                return base
    st = ST_SUPPORTED if top["score"] >= SUPPORT_THRESHOLD else ST_UNVERIFIABLE
    base.update({"status": st,
                 "reason": "相似度 %.3f（未做模型判定）" % top["score"],
                 "status_label": STATUS_LABEL[st]})
    return base


def tool_generate_report() -> dict:
    """工具：汇总当前所有检查结果，生成最终报告。"""
    r = self_check(verbose=False)
    return {"issues_total": r["summary"]["total"],
            "high": r["summary"]["high"], "medium": r["summary"]["medium"],
            "low": r["summary"]["low"],
            "by_kind": r["summary"]["by_kind"],
            "evidence_check": r["summary"].get("evidence_check", {})}


# ══════════════════ 该引未引：找出需要引用却没引的陈述 ══════════════════

# 需要文献支撑的"论断性"表述（出现这些词，说明在陈述研究结论/对比/数据）
CLAIM_CUES = [
    r"(?i)\b(?:outperform|achiev(?:e|es|ed)|surpass|improve[sd]?|reduce[sd]?|"
    r"demonstrat(?:e|es|ed)|show(?:s|ed)? that|prove[sd]?|report(?:s|ed)?|"
    r"state[- ]of[- ]the[- ]art|sota|significantly|superior)\b",
    r"(?:表明|证明|显示|验证|优于|超过|提升|降低|改善|达到|取得|显著|"
    r"效果更好|性能更佳|精度更高|准确率|F1|召回率|相较|相比)",
    r"\d+(?:\.\d+)?\s*%",
]
# 明显不需要引用的句子（背景铺垫、行文过渡、自述）
NO_CITE_CUES = [
    r"(?i)^\s*(?:this paper|we |our )\b",
    r"(?:本文|本研究|我们)(?:提出|采用|设计|实现|工作)",
    r"^\s*(?:综上|总之|首先|其次|最后|此外|另外|因此|然而|同时)",
    r"^(?:研究现状|参考文献)\s*$",
]


def _needs_citation(sentence: str) -> bool:
    """判断该句是否"需要引用"（而不是自述或过渡句）。"""
    s = (sentence or "").strip()
    if len(s) < 25:
        return False
    for p in NO_CITE_CUES:
        if re.search(p, s):
            return False
    return any(re.search(p, s) for p in CLAIM_CUES)


def suggest_missing_citations(body: str, retr, doc_of_index: dict,
                              max_items: int = 8, min_score: float = 0.25) -> dict:
    """找出需要引用却没有引用的陈述，并推荐最相关的文献。

    只有"确实能找到相关文献"（相似度达阈值）时才给出建议，
    否则说明该句可能是作者自己的主张，不需要强行加引用。
    """
    # 正文里所有句子
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", body or "") if len(s.strip()) >= 25]
    # 已有引用的句子
    cited_texts = set()
    for s in sentences:
        if CITATION_PAT.search(s):
            cited_texts.add(s)

    # 已引用的文献（用于标注"未引用"）
    cited_nums = set()
    for s in cited_texts:
        for m in re.finditer(r"\[(\d{1,3})(?:\s*[-,–]\s*(\d{1,3}))*\]", s):
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            for n in range(min(a, b), max(a, b) + 1):
                cited_nums.add(n)

    suggestions = []
    for s in sentences:
        if CITATION_PAT.search(s):
            continue
        if not _needs_citation(s):
            continue
        if not retr:
            suggestions.append({"sentence": s[:150], "reason": "该陈述像是研究结论，但未标注引用",
                                "candidates": []})
            continue
        # 全库检索最相关的文献
        best = []
        for key, doc in (doc_of_index or {}).items():
            hits = retr.find(key, s, top_k=1)
            if hits and hits[0]["score"] >= min_score:
                best.append({"paper": doc.get("title"), "score": round(hits[0]["score"], 4),
                             "evidence": hits[0]["chunk"][:200]})
        best.sort(key=lambda x: -x["score"])
        if best:
            suggestions.append({
                "sentence": s[:150],
                "reason": "该陈述需要文献支撑，但未标注引用",
                "candidates": best[:2],
            })
        if len(suggestions) >= max_items:
            break

    return {
        "total": len(suggestions),
        "suggestions": suggestions,
        "cited_count": len(cited_nums),
        "note": "建议基于全文语义检索结果，仅供参考；若该句是作者自己的主张，可不加引用。",
    }


def run_missing_citation_check(verbose: bool = True) -> dict:
    """独立入口：检测"该引未引"，结果落盘。"""
    ensure_dirs()
    from .draft import load_draft
    from .literature import load_docs

    draft = load_draft()
    if not draft:
        raise RuntimeError("尚未生成综述草稿，请先执行 draft")
    docs = load_docs().get("docs", [])
    retr, by_key = build_docs_index(docs)
    parts = _split_draft(draft.get("markdown", ""))
    res = suggest_missing_citations(parts["body"], retr, by_key)
    with open(os.path.join(OUT_DIR, "missing_citations.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    if verbose:
        print("[该引未引] 发现 %d 处需要引用但未标注的陈述" % res["total"])
        for i, s in enumerate(res["suggestions"][:5], 1):
            print("   %d. %s" % (i, s["sentence"][:60]))
            for c in s.get("candidates", [])[:1]:
                print("      建议引用: %s（相关度 %.3f）" % ((c["paper"] or "")[:40], c["score"]))
    return res


# ────────────────────────── 埋错测试 ──────────────────────────

INJECTIONS = {
    "错编号顺序": "交换正文前两处引用的编号，使编号顺序不再递增",
    "假引用": "在正文插入一个参考文献表中不存在的角标 [99]",
    "缺文献类型标识": "删除某条目中的 [J]/[C] 等文献类型标识",
    "重复著录": "把第一条文献复制到表末，形成同一文献两条著录",
}

# 每类埋错对应的"应检出问题类别"，用于计算严格查全率
EXPECTED_KINDS = {
    "错编号顺序": {"编号顺序"},
    "假引用": {"引用真实性", "引用越界"},
    "缺文献类型标识": {"格式合规"},
    "重复著录": {"重复著录"},
}


def make_error_dataset(markdown: str, strict: bool = False) -> dict:
    """生成 4 类"埋错"样本，用于验证自检的检出能力（严格判定：必须检出对应类别）。

    strict 决定"参考文献区为空"时的行为，两种调用方的语义**相反**：
      · strict=False（默认，供「检查我的稿子」用）：用户的稿子可以还没有
        参考文献区，那是正常输入 → 返回带 skipped 原因的结果，不报错。
      · strict=True（供管线里的埋错测试用）：此时"没测"和"没检出"必须区分开，
        静默写出 total:0 会伪装成一次失败的评测（实测踩到过：控制台 4/4、
        落盘却是 0/0）→ 直接报错。
    """
    parts = _split_draft(markdown)
    body, refs_block = parts["body"], parts["refs_block"]
    refs = _parse_refs(refs_block)
    cases = []

    # 参考文献为空的草稿造不出埋错用例。这里**不能抛异常**：本函数同时被
    # 「检查我的稿子」流程调用（draft_check.py），而用户的稿子完全可能
    # 还没有参考文献区——那是正常输入，不是错误（加了硬报错后该接口直接 500，
    # 实测踩到过）。所以这里返回一个带 skipped 原因的结构化结果；
    # 真正需要"失败要响"的是管线里的 benchmark()，由它负责报错。
    if not refs:
        if strict:
            raise RuntimeError(
                "埋错测试无法进行：草稿的参考文献区解析不出条目"
                "（正文 %d 字，参考文献区 %d 字）。"
                "请确认草稿含『## 参考文献』且条目为『[n] …』格式。" % (
                    len(body), len(refs_block or "")))
        return {
            "cases": [], "total": 0, "detected": 0, "recall": 0.0,
            "skipped": True,
            "skip_reason": "草稿的参考文献区没有可识别的条目（形如「[1] …」），"
                           "无法构造埋错样本",
            "source_draft": {"body_chars": len(body),
                             "refs_chars": len(refs_block or ""),
                             "refs_parsed": 0},
        }

    if refs:
        # 类型 1：编号顺序错乱 —— 用可控文本，确保只制造"顺序"这一处错误
        b = ("少样本学习在近年受到广泛关注[2]。已有工作提出了多种元学习框架[1]。"
             "在此基础上，后续研究进一步改进了特征表示[3]。")
        cases.append({"error_type": "错编号顺序", "markdown": b + "\n## 参考文献\n" + refs_block})

        # 类型 2：假引用（角标 [99] 在表中不存在）
        cases.append({"error_type": "假引用",
                      "markdown": body + "\n综上，该结论已在多项工作中得到验证[99]。\n\n## 参考文献\n" + refs_block})

        # 类型 3：缺文献类型标识
        b0 = refs[0]["text"]
        broken = re.sub(r"\[[A-Z]{1,2}(?:/OL)?\]", "", b0, count=1)
        new_refs = "\n".join(("[%d] " % r["index"]) + (broken if r["index"] == refs[0]["index"] else r["text"])
                             for r in refs)
        cases.append({"error_type": "缺文献类型标识",
                      "markdown": body + "\n## 参考文献\n" + new_refs})

        # 类型 4：重复著录（同一文献两条，且新增条被正文引用）
        # 用**自带构造**的规范条目，不依赖输入草稿的参考文献质量：
        # 若草稿本身条目残缺（例如模型输出 "[5] 003[EB/OL]. n.d.."），
        # 拿它复制出来的两条无法被去重逻辑识别，基准会误报漏检（实测踩到过）。
        base = ("[101] Zhang S, Li M, Wang Q. A robust baseline for few-shot "
                "classification[J]. Journal of Testing, 2021, 12(3): 45-58.")
        dup_entry = "[102] " + base.split("] ", 1)[1]
        new_refs = refs_block.rstrip() + "\n" + base + "\n" + dup_entry
        cases.append({"error_type": "重复著录",
                      "markdown": body + "\n该问题在相关研究中已有讨论[102]。\n\n## 参考文献\n" + new_refs})

    # 运行自检，严格统计：是否检出**对应类别**的问题
    results = []
    for c in cases:
        r = run_checks(c["markdown"])
        kinds = set(i["kind"] for i in r["issues"])
        expect = EXPECTED_KINDS.get(c["error_type"], set())
        hit = bool(kinds & expect) if expect else bool(r["issues"])
        results.append({"error_type": c["error_type"], "detected": hit,
                        "expected_kinds": sorted(expect),
                        "matched_kinds": sorted(kinds & expect),
                        "issue_kinds": sorted(kinds), "issue_count": r["summary"]["total"]})

    detected = sum(1 for r in results if r["detected"])
    report = {
        "cases": results, "total": len(results), "detected": detected,
        "recall": (detected / len(results)) if results else 0.0,
        "note": "每类埋入 1 处已知错误；严格判定标准为「检出对应类别的问题」",
        # 把被测文本的证据一并落盘：以后若出现"控制台与文件不一致"，
        # 一眼能看出测的是哪份草稿、解析出几条参考文献。
        "source_draft": {
            "path": DRAFT_MD,
            "body_chars": len(body),
            "refs_chars": len(refs_block or ""),
            "refs_parsed": len(refs),
        },
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(os.path.join(OUT_DIR, "selfcheck_benchmark.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def benchmark(verbose: bool = True) -> dict:
    if not os.path.isfile(DRAFT_MD):
        raise RuntimeError("尚未生成综述草稿")
    with open(DRAFT_MD, encoding="utf-8") as f:
        md = f.read()
    rep = make_error_dataset(md, strict=True)
    if verbose:
        print("[埋错测试] %d/%d 类错误被检出，查全率 %.2f" % (
            rep["detected"], rep["total"], rep["recall"]))
        for c in rep["cases"]:
            print("   %-12s %s  检出项：%s" % (
                c["error_type"], "✓ 已检出" if c["detected"] else "✗ 漏检",
                "/".join(c["issue_kinds"]) or "—"))
    return rep
