"""GB/T 7714 参考文献格式生成与校验。

覆盖常见文献类型：
- 期刊论文 [J]
- 会议论文 [C]
- 学位论文 [D]
- 专著 [M]
- 电子资源/预印本 [EB/OL]
- 报纸 [N]

规则要点（GB/T 7714-2015）：
作者. 题名[文献类型标识]. 刊名, 年, 卷(期): 起止页码.
作者. 题名[文献类型标识]. 出版地: 出版者, 出版年.
电子资源需给出引用日期与访问路径。
"""
from __future__ import annotations

import re

TYPE_J, TYPE_C, TYPE_D, TYPE_M, TYPE_N, TYPE_EB = "J", "C", "D", "M", "N", "EB/OL"

# 会议/期刊关键词线索 → 文献类型
CONF_HINTS = ("conference", "proceedings", "symposium", "workshop", "cvpr", "iccv", "eccv",
              "neurips", "nips", "icml", "iclr", "aaai", "ijcai", "acl", "emnlp", "naacl",
              "sigir", "kdd", "www", "mm", "会议", "研讨会", "论文集")
JOURNAL_HINTS = ("journal", "transactions", "letters", "review", "学报", "杂志", "期刊")


def guess_type(venue: str, title: str = "") -> str:
    v = (venue or "").lower()
    t = (title or "").lower()
    if any(h in v for h in CONF_HINTS) or any(h in t for h in ("proceedings of", "conference on")):
        return TYPE_C
    if any(h in v for h in JOURNAL_HINTS):
        return TYPE_J
    if "arxiv" in v or "preprint" in v:
        return TYPE_EB
    if "thesis" in v or "dissertation" in v or "大学" in (venue or "") and "论文" in (venue or ""):
        return TYPE_D
    return TYPE_J if venue else TYPE_EB


def _fmt_authors(authors: list, max_n: int = 3) -> str:
    """作者格式：三名以内全列，超过三名列前三名后加 et al."""
    names = [a.strip() for a in (authors or []) if a and a.strip()]
    if not names:
        return ""
    if len(names) > max_n:
        return ", ".join(names[:max_n]) + ", et al"
    return ", ".join(names)


def format_entry(rec: dict, idx: int = None) -> str:
    """把一条文献记录格式化为 GB/T 7714 条目。"""
    authors = _fmt_authors(rec.get("authors"))
    title = (rec.get("title") or "").strip().rstrip(".")
    year = rec.get("year") or "n.d."
    venue = (rec.get("venue") or "").strip()
    typ = rec.get("ref_type") or guess_type(venue, title)
    doi = (rec.get("doi") or "").strip()
    url = (rec.get("pdf_url") or "").strip()

    parts = []
    if authors:
        parts.append(authors + ".")
    parts.append("%s[%s]." % (title, typ))

    if typ == TYPE_J and venue:
        parts.append("%s, %s." % (venue, year))
    elif typ == TYPE_C and venue:
        parts.append("%s, %s." % (venue, year))
    elif typ == TYPE_EB:
        seg = ""
        if venue:
            seg += venue + ", "
        seg += str(year)
        if url:
            seg += ". " + url
        parts.append(seg + ".")
    else:
        seg = (venue + ", " if venue else "") + str(year)
        parts.append(seg + ".")

    if doi:
        parts.append("DOI: %s." % doi.replace("https://doi.org/", ""))

    body = " ".join(p for p in parts if p)
    body = re.sub(r"\s+", " ", body).strip()
    return ("[%d] %s" % (idx, body)) if idx else body


def build_reference_list(records: list) -> list:
    """按传入顺序生成参考文献列表（顺序编码制：按正文出现顺序编号）。"""
    out = []
    for i, rec in enumerate(records, 1):
        out.append({"index": i, "cite": "[%d]" % i,
                    "text": format_entry(rec, i),
                    "title": rec.get("title"), "year": rec.get("year"),
                    "venue": rec.get("venue"), "type": rec.get("ref_type") or guess_type(
                        rec.get("venue", ""), rec.get("title", ""))})
    return out


# ────────────────────────── 校验 ──────────────────────────

REQUIRED_PIECES = {
    TYPE_J: ["题名", "刊名", "年份"],
    TYPE_C: ["题名", "会议/论文集名", "年份"],
    TYPE_EB: ["题名", "年份", "访问路径"],
}


def check_entry(text: str) -> list:
    """校验单条参考文献格式，返回问题列表。"""
    issues = []
    body = re.sub(r"^\[\d+\]\s*", "", text).strip()

    m = re.search(r"\[([A-Z]{1,2}(?:/OL)?)\]", body)
    if not m:
        issues.append("缺少文献类型标识（如 [J]/[C]/[M]/[D]/[EB/OL]）")
        typ = None
    else:
        typ = m.group(1)

    if not re.match(r"^[^.]{2,}\.", body):
        issues.append("作者项缺失或格式不正确")

    if not re.search(r"\b(19|20)\d{2}\b", body):
        issues.append("缺少出版年份")

    if typ == TYPE_EB and not re.search(r"https?://", body):
        issues.append("电子资源缺少访问路径")

    if re.search(r"\bet al\b", body) and not re.search(r",\s*et al", body):
        issues.append("et al 前应使用逗号")

    if body.endswith("。"):
        issues.append("中文句号不应出现在英文格式条目末尾")

    if re.search(r"\s{2,}", body):
        issues.append("存在多余连续空格")

    return issues
