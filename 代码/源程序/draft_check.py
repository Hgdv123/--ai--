"""检查"我的稿子"：针对用户自己写的论文做引用自检。

与"生成综述后的自检"的关键区别
--------------------------------
生成综述时，正文和参考文献都是程序生成的，自检只需查内部一致性。
而检查用户稿子时，**稿子的参考文献是用户自己写的**，所以核心问题是：

1. 稿子里引用的文献，**我的文献库里有没有**？（有全文才能核对事实）
2. 引用编号是否真实存在、是否有越界、顺序是否正确
3. 有没有"该引却没引"的论断
4. 参考文献著录是否符合 GB/T 7714
5. 稿子里引的条目有没有重复、有没有"列了却没引"

第 1 点是这个模式独有的价值：它把"用户稿子"与"用户已导入的文献"连起来，
明确告诉用户哪些引用**可核实**、哪些**因为没导入原文而无法核实**。
"""
from __future__ import annotations

import json
import os
import re
import time

from .config import DATA_DIR, OUT_DIR
from .draft import _clean_field  # noqa: F401  (保持与综述自检一致的清洗口径)
from .literature import load_corpus, load_docs
from .selfcheck import (_cited_order, _norm_title_ref, _parse_refs, _split_draft,
                        CITATION_PAT, decompose_claims, iterate_sentences,
                        make_error_dataset, suggest_missing_citations)
from . import gbt7714

DRAFT_DIR = os.path.join(DATA_DIR, "mydraft")
DRAFT_JSON = os.path.join(DRAFT_DIR, "my_draft.json")
REPORT_JSON = os.path.join(OUT_DIR, "mydraft_report.json")

# 正文中的引用角标：支持 [1] [1,2] [1-3]
BARE_CITE = re.compile(r"\[(\d{1,3})(?:\s*[-,–]\s*(\d{1,3}))?\]")


def _norm(s: str) -> str:
    """标题归一化：只保留字母数字，用于跨来源比对。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (s or "").lower())


def _norm_key(title: str) -> str:
    """去掉副标题（冒号/问号之后）后再归一化，提高匹配率。"""
    t = re.split(r"[:：?？]", title or "")[0]
    return _norm(t)


def load_my_draft() -> dict:
    """读取已上传的稿子解析结果。"""
    if not os.path.isfile(DRAFT_JSON):
        return {}
    try:
        with open(DRAFT_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _build_library_index() -> tuple:
    """建立"文献库标题 → 是否有全文"的索引。

    返回 (精确索引, 归一化索引, 标题列表)：
      - 精确索引：归一化完整标题 → {key, has_fulltext, title}
      - 归一化索引：去副标题后的标题 → 同上
    """
    corpus = load_corpus()
    docs = {d.get("title"): d for d in load_docs().get("docs", [])}
    exact, loose, titles = {}, {}, []
    for i, r in enumerate(corpus.get("records", []), 1):
        title = r.get("title") or ""
        if not title:
            continue
        doc = docs.get(title) or {}
        full = bool((doc.get("full_text") or "").strip())
        # 只有题录（doc_type=ref）或未解析的，视为无全文
        if (r.get("doc_type") or "") == "ref":
            full = False
        info = {"key": "d%d" % i, "title": title, "has_fulltext": full,
                "year": r.get("year"), "source": r.get("source")}
        titles.append(info)
        exact.setdefault(_norm(title), info)
        loose.setdefault(_norm_key(title), info)
    return exact, loose, titles


def _match_library(entry: str, exact: dict, loose: dict) -> dict:
    """把稿子里的参考文献条目匹配到文献库中的某篇。"""
    # 先从条目里抽一个"疑似标题"：GB/T 7714 里标题在作者之后、[类型] 之前
    m = re.search(r"\]\s*([^\[\]]{6,200}?)\s*\[[A-Z]{1,2}(?:/OL)?\]", entry or "")
    cand = m.group(1) if m else ""
    if not cand:
        # 退路：取最长的一段连续文字
        parts = re.split(r"[.。;；]", entry or "")
        cand = max(parts, key=len).strip() if parts else ""
    for probe in (cand, _norm_title_ref(entry)):
        if not probe:
            continue
        hit = exact.get(_norm(probe)) or loose.get(_norm_key(probe))
        if hit:
            return hit
    # 再退一步：用标题前 12 个字符做前缀匹配
    k = _norm_key(cand)[:12]
    if len(k) >= 8:
        for nk, info in loose.items():
            if nk.startswith(k) or k.startswith(nk[:12]):
                return info
    return {}


# 参考文献区标题：Word 抽出的纯文本没有 Markdown 的 "##"，所以两种都要认
REF_HEAD = re.compile(r"(?m)^\s*(?:#{1,4}\s*)?(?:参考文献|references|bibliography)\s*:?\s*$",
                      re.I)


def split_draft_text(text: str) -> tuple:
    """把稿子切成 (正文, 参考文献区)。

    为什么不用 selfcheck._split_draft：那个函数是给**程序生成的 Markdown**
    用的，只认 "## 参考文献" 这种带井号的标题；而用户上传的 Word 稿子抽出来
    是纯文本 "参考文献"，于是整篇被当成正文、参考文献区为空（实测踩到这个坑）。
    """
    t = text or ""
    m = REF_HEAD.search(t)
    if not m:
        return t, ""
    return t[:m.start()], t[m.end():]


def check_my_draft(verbose: bool = True) -> dict:
    """对"我的稿子"执行引用自检，返回结构化报告。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    rec = load_my_draft()
    if not rec:
        raise RuntimeError("还没有上传稿子，请先上传 .docx")

    body_text = rec.get("full_text") or ""
    body, refs_block = split_draft_text(body_text)
    # 优先用 docx_parse 已经识别好的参考文献区（它对"参考文献"标题更敏感）
    if not refs_block.strip() and (rec.get("refs_text") or "").strip():
        refs_block = rec["refs_text"]
        if rec.get("refs_text"):
            body = body_text.replace(rec["refs_text"], "", 1)
    entries = _parse_refs(refs_block)
    entry_texts = [e.get("text") or "" for e in entries] if entries else []
    if not entry_texts:
        # 没有标题或解析失败时，按行退化解析（行首为 [n] 的行）
        entry_texts = [l.strip() for l in refs_block.splitlines()
                       if re.match(r"^\s*\[\d{1,3}\]", l)]

    exact, loose, lib_titles = _build_library_index()

    issues = []

    def add(kind, severity, location, detail, suggestion=""):
        issues.append({"kind": kind, "severity": severity, "location": location,
                       "detail": detail, "suggestion": suggestion})

    # ── 1. 正文角标 vs 参考文献表：真实性、越界、顺序 ──
    cited = sorted(set(_cited_order(body)))
    ref_nums = []
    for i, t in enumerate(entry_texts, 1):
        m = re.match(r"\s*\[(\d{1,3})\]", t)
        ref_nums.append(int(m.group(1)) if m else i)

    for n in cited:
        if n not in ref_nums:
            add("引用真实性", "高", "正文",
                "正文引用了 [%d]，但参考文献表中没有这一条" % n,
                "补上该条目，或删除这个角标")
    for n in ref_nums:
        if n not in cited:
            add("文末未引用", "中", "参考文献",
                "参考文献 [%d] 在正文中从未被引用" % n, "删除该条目或在正文中引用它")
    inrange = [n for n in cited if n in ref_nums]
    if inrange and inrange != sorted(inrange):
        add("编号顺序", "中", "正文",
            "引用编号未按首次出现顺序递增：%s" % inrange[:8],
            "按顺序编码制重排编号")

    # ── 2. 参考文献格式合规（GB/T 7714）──
    fmt_bad = 0
    for i, t in enumerate(entry_texts, 1):
        probs = gbt7714.check_entry(t)
        if probs:
            fmt_bad += 1
            if fmt_bad <= 5:
                add("格式合规", "中", "参考文献 [%d]" % i,
                    "；".join(probs)[:80], "按 GB/T 7714 补全该条目")

    # ── 3. 重复著录 ──
    seen = {}
    for i, t in enumerate(entry_texts, 1):
        k = _norm(_norm_title_ref(t))
        if not k:
            continue
        if k in seen:
            add("重复著录", "中", "参考文献 [%d]" % i,
                "与 [%d] 疑似同一条文献" % seen[k], "合并重复条目")
        else:
            seen[k] = i

    # ── 4. 引用可核实性：稿子引的文献，库里有没有（本模式的核心）──
    matched, unmatched = [], []
    for i, t in enumerate(entry_texts, 1):
        info = _match_library(t, exact, loose)
        if info:
            matched.append({"index": i, "library": info})
        else:
            unmatched.append({"index": i, "entry": t[:90]})
    if unmatched:
        add("引用可核实性", "低", "参考文献",
            "%d 条参考文献未在已导入文献中找到，无法核对原文" % len(unmatched),
            "导入这些文献的原文后可核对事实；只导入题录无法核对")
    nofull = [m for m in matched if not m["library"]["has_fulltext"]]
    if nofull:
        add("引用可核实性", "低", "参考文献",
            "%d 条已匹配到文献库，但库中没有其全文（可能只导入了题录）" % len(nofull),
            "补充这些文献的 PDF/Word 原文")

    # ── 5. 论断有据（缺角标）──
    # 必须用 iterate_sentences（返回全部句子）：decompose_claims 只返回带引用的
    # 句子，用它永远查不出"完全没引用"的陈述。
    all_sents = iterate_sentences(body)
    bare_long = [c for c in all_sents
                 if not (c.get("citations") or []) and len(c.get("sentence") or "") >= 40]
    if bare_long:
        add("论断有据", "中", "正文",
            "%d 处陈述未见引用标注" % len(bare_long),
            "综述类陈述应标注来源；若为作者自身主张可保留")
        for c in bare_long[:3]:
            add("论断有据", "低", "正文",
                "未标注引用的陈述：「%s…」" % (c.get("sentence") or "")[:46],
                "补充引用，或确认这是作者自己的主张")

    # ── 6. 该引未引（用文献库做相关性检索）──
    missing = {"total": 0, "suggestions": []}
    try:
        from .selfcheck import build_docs_index
        docs = load_docs().get("docs", [])
        retr, by_key = build_docs_index(docs)
        if retr:
            missing = suggest_missing_citations(body, retr, by_key)
        if missing.get("suggestions"):
            for s in missing["suggestions"][:5]:
                cand = (s.get("candidates") or [{}])[0]
                add("该引未引", "中", "正文",
                    "该陈述需要文献支撑但未标注引用：「%s…」" % (s.get("sentence") or "")[:50],
                    "建议引用：%s" % (cand.get("paper") or "（未找到明显相关文献）")[:50])
    except Exception:
        pass

    # ── 7. 意图核对：稿子说"自研/提出"？给客观提示（不做主观判定）──
    invent = len(re.findall(r"(?:本文|我们)(?:提出|设计|实现|构建)", body))
    if invent == 0 and len(body) > 800:
        add("作者贡献", "低", "正文",
            "正文中未见「本文提出/我们设计」这类作者贡献表述", "确认是否为综述类文稿")

    bench = make_error_dataset(body) if body else {"total": 0, "detected": 0, "recall": 0.0}

    # ── 汇总 ──
    by_kind, by_sev = {}, {"高": 0, "中": 0, "低": 0}
    for it in issues:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
        by_sev[it["severity"]] = by_sev.get(it["severity"], 0) + 1

    report = {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "doc": {"name": rec.get("original_name"), "title": rec.get("title"),
                "chars": rec.get("chars_total"), "sections": rec.get("sections_found") or []},
        "summary": {"total": len(issues), "high": by_sev["高"], "medium": by_sev["中"],
                    "low": by_sev["低"], "by_kind": by_kind,
                    "citations_in_text": len(cited), "refs_entries": len(entry_texts),
                    "matched_library": len(matched), "unmatched": len(unmatched),
                    "library_total": len(lib_titles),
                    "missing_citations": missing.get("total", 0)},
        "issues": issues,
        "match": {"matched": matched[:40], "unmatched": unmatched[:40]},
        "benchmark": bench,
    }
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if verbose:
        s = report["summary"]
        print("[稿子检查] %s" % (rec.get("title") or rec.get("original_name")))
        print("  正文引用 %d 处；参考文献 %d 条；文献库 %d 篇" % (
            s["citations_in_text"], s["refs_entries"], s["library_total"]))
        print("  可核实性：命中文献库 %d 条，未命中 %d 条" % (
            s["matched_library"], s["unmatched"]))
        print("  问题：%d 处（高 %d / 中 %d / 低 %d）" % (
            s["total"], s["high"], s["medium"], s["low"]))
        for it in issues[:8]:
            print("    [%s] %s —— %s" % (it["severity"], it["kind"], it["detail"][:60]))
    return report
