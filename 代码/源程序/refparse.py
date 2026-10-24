"""题录解析：把知网 / CNKI / RefWorks 等平台导出的题录文本拆成结构化字段。

支持三种常见格式：
1. **RefWorks / NoteExpress 标签式**（知网默认导出格式之一）：
       A1: 张三, 李四
       T1: 基于深度学习的图像分类
       JF: 计算机学报
       YR: 2022
   也兼容 `%A 张三` 这种百分号标签。
2. **明文键值式**（知网页面复制的常见样式）：
       【作者】张三;李四
       【题名】基于深度学习的图像分类
       【刊名】计算机学报
3. **多行块式**（每篇之间空行分隔，字段名后跟冒号）。

解析结果与 PDF/Word 解析一致，可直接进入卡片抽取、对比矩阵与引用核对的流程。
"""
from __future__ import annotations

import json
import os
import re
import time

from . import gbt7714

# 字段别名 → 统一字段名
# 注意：RefWorks / EndNote 的**单字母标签**也要支持：A=作者 T=题名 J=刊名 D=年份
FIELD_ALIASES = {
    # 作者
    "author": "authors", "authors": "authors", "a1": "authors", "af": "authors", "a": "authors",
    "作者": "authors", "责任者": "authors", "全部作者": "authors",
    # 题名
    "title": "title", "t1": "title", "ti": "title", "t": "title",
    "题名": "title", "篇名": "title", "标题": "title", "题名/责任者": "title",
    # 来源
    "journal": "venue", "jf": "venue", "jo": "venue", "ja": "venue", "j": "venue",
    "刊名": "venue", "期刊": "venue", "来源": "venue", "出处": "venue",
    "来源出版物": "venue", "报纸名": "venue", "母体文献": "venue",
    # 年份
    "year": "year", "yr": "year", "py": "year", "date": "year", "d": "year", "y": "year",
    "年": "year", "年份": "year", "出版年": "year", "发表时间": "year",
    # 其他
    "keyword": "keywords", "keywords": "keywords", "k1": "keywords", "k": "keywords",
    "关键词": "keywords",
    "abstract": "abstract", "ab": "abstract", "摘要": "abstract",
    "doi": "doi", "do": "doi",
    "url": "url", "lk": "url", "链接": "url", "网址": "url", "u": "url",
    "volume": "volume", "vo": "volume", "卷": "volume", "v": "volume",
    "issue": "issue", "is": "issue", "期": "issue", "n": "issue",
    "page": "pages", "pages": "pages", "sp": "pages", "p": "pages",
    "页码": "pages", "起止页码": "pages",
    "type": "ref_type", "文献类型": "ref_type",
    "fund": "fund", "基金": "fund",
    "机构": "org", "ad": "org", "作者单位": "org", "机构/作者": "org",
    "出版社": "publisher", "pb": "publisher", "出版地": "place",
    "副题名": "subtitle",
}

# RefWorks 标签式：`A1: 张三` / `%A 张三` / `A1 张三`
TAG_LINE = re.compile(r"^\s*%?([A-Za-z]{1,2}\d?)\s*[:：]?\s*(.+?)\s*$")
# 以 % 开头的标签行（RefWorks / EndNote 常见），单独识别以便优先判定为标签式
PERCENT_TAG = re.compile(r"^\s*%([A-Za-z0-9]{1,2})\s*[:：]?\s*(.*)$")
# 明文键值式：「【作者】张三」/「作者：张三」
KV_LINE = re.compile(r"^\s*[【\[]?\s*([\u4e00-\u9fffA-Za-z]{2,8})\s*[】\]]?\s*[:：]\s*(.+?)\s*$")
# 【作者】张三（无冒号）
BRACKET_LINE = re.compile(r"^\s*【\s*([\u4e00-\u9fffA-Za-z]{2,8})\s*】\s*(.*?)\s*$")


def _norm_field(name: str) -> str:
    key = (name or "").strip().lower().replace(" ", "")
    return FIELD_ALIASES.get(key, FIELD_ALIASES.get(name.strip(), ""))


def _split_authors(raw: str) -> list:
    """拆分作者；去掉知网作者项后附带的机构与数字编号。"""
    if not raw:
        return []
    s = re.sub(r"\(\d+\)|\[\d+\]|（\d+）", "", raw)
    parts = re.split(r"[;,；，、]\s*|\s{2,}", s)
    out = []
    for p in parts:
        p = p.strip(" .;；,，")
        if not p or len(p) > 30:
            continue
        # 知网常在作者后跟机构名："张三(北京邮电大学)"
        p = re.sub(r"[（(].*?[）)]", "", p).strip()
        if p:
            out.append(p)
    return out[:15]


def _split_keywords(raw: str) -> list:
    if not raw:
        return []
    return [k.strip(" .;；,，") for k in re.split(r"[;,；，、]\s*", raw) if k.strip()][:15]


def _year_of(raw: str):
    m = re.search(r"\b(19\d{2}|20\d{2})\b", raw or "")
    return int(m.group(1)) if m else None


def _clean_title(raw: str) -> str:
    """去掉题名里粘连的作者与文献类型标识。"""
    t = re.sub(r"\[[A-Z]{1,2}(?:/OL)?\]", "", raw or "").strip()
    # "题名/责任者" 形式："基于XX的研究/张三"
    if "/" in t:
        head = t.split("/")[0].strip()
        if len(head) >= 6:
            t = head
    return t.strip(" .。;；")


def detect_format(text: str) -> str:
    """识别题录格式：refworks | bracket | kv | plain。"""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return "plain"
    # 以 % 开头的标签行：直接判为标签式（RefWorks/EndNote）
    pct = sum(1 for l in lines if PERCENT_TAG.match(l))
    if pct >= 2:
        return "refworks"
    tag_hits = sum(1 for l in lines
                   if re.match(r"^\s*([A-Za-z]{1,2}\d?)\s*[:：]\s*\S", l)
                   and _norm_field(re.match(r"^\s*([A-Za-z]{1,2}\d?)\s*[:：]", l).group(1)))
    bracket_hits = sum(1 for l in lines if BRACKET_LINE.match(l))
    kv_hits = sum(1 for l in lines if KV_LINE.match(l) and _norm_field(KV_LINE.match(l).group(1)))
    if tag_hits >= 2 and tag_hits >= bracket_hits:
        return "refworks"
    if bracket_hits >= 2:
        return "bracket"
    if kv_hits >= 2:
        return "kv"
    return "plain"


def _parse_refworks(text: str) -> list:
    """按标签式解析。

    记录边界有两种：
    1. **空行**（知网导出常以空行分条）——优先按空行切块，块内独立解析；
    2. **块内出现第二个 T1**（题名）——说明上一条结束。
    """
    blocks = [b for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    if not blocks:
        return []
    # 若只有一块（没有空行分隔），整体作为一块处理
    out = []
    for blk in blocks:
        out.extend(_parse_refworks_block(blk))
    return out


def _parse_refworks_block(text: str) -> list:
    """解析单个题录块（块内可能有多个 T1）。"""
    records, cur = [], {}
    pending_authors, pending_keywords = [], []

    def flush():
        nonlocal cur, pending_authors, pending_keywords
        if cur.get("title") or cur.get("authors"):
            records.append(cur)
        cur = {}
        pending_authors, pending_keywords = [], []

    for line in (text or "").splitlines():
        if not line.strip():
            continue
        # 先试 %标签（%A 张三），再试常规标签（A1: 张三 / A1 张三）
        pm = PERCENT_TAG.match(line)
        if pm and _norm_field(pm.group(1)):
            fld, val = _norm_field(pm.group(1)), (pm.group(2) or "").strip()
        else:
            # 支持 "A1: 张三" / "A1 张三" / "A1:张三"（无空格）
            m = re.match(r"^\s*([A-Za-z]{1,2}\d?)\s*[:：]\s*(.+?)\s*$", line)
            if m and _norm_field(m.group(1)):
                fld, val = _norm_field(m.group(1)), m.group(2).strip()
            else:
                m2 = re.match(r"^\s*([A-Za-z]{1,2}\d?)\s+(.+?)\s*$", line)
                if not (m2 and _norm_field(m2.group(1))):
                    continue
                fld, val = _norm_field(m2.group(1)), m2.group(2).strip()
        if not fld:
            continue

        if fld == "title":
            if cur.get("title"):
                flush()
            cur["title"] = val
            if pending_authors:
                cur.setdefault("authors", []).extend(pending_authors)
                pending_authors = []
            if pending_keywords:
                cur.setdefault("keywords", []).extend(pending_keywords)
                pending_keywords = []
            continue

        if fld == "authors":
            # 同一块内出现第二组作者且已有一条完整记录 → 视为新记录（无题名的情况少见，兜底）
            if cur.get("title"):
                cur.setdefault("authors", []).extend(_split_authors(val))
            else:
                pending_authors.extend(_split_authors(val))
        elif fld == "keywords":
            if cur.get("title"):
                cur.setdefault("keywords", []).extend(_split_keywords(val))
            else:
                pending_keywords.extend(_split_keywords(val))
        elif fld == "abstract":
            cur["abstract"] = (cur.get("abstract", "") + " " + val).strip()
        else:
            if fld not in cur or not cur[fld]:
                cur[fld] = val
            # 年份字段若粘连（"%D2023" → val 为空），从整行兜底取年份
            if fld == "year" and not _year_of(str(cur.get(fld) or "")):
                ry = _year_of(line)
                if ry:
                    cur[fld] = ry
    flush()
    return records


def _clean_venue(raw: str) -> str:
    """刊名里若混入了下一个字段（如"计算机学报 【年】2021"），把尾部清理掉。"""
    if not raw:
        return ""
    v = re.split(r"【|\[[^\]]{1,6}\]\s*[:：]|\s{2,}", raw)[0]
    v = re.sub(r"\s*(?:19|20)\d{2}\s*$", "", v)
    return v.strip(" .。;；,，")


def _parse_kv(text: str) -> list:
    """按「【字段】值」或「字段: 值」解析，空行作为记录分隔。"""
    records, cur = [], {}
    last_field = {"name": ""}

    def flush():
        nonlocal cur
        if cur.get("title") or cur.get("authors"):
            records.append(cur)
        cur = {}
        last_field["name"] = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            # 空行 = 记录分隔（题录常见的分条方式）
            flush()
            continue
        m = BRACKET_LINE.match(line)
        if m:
            fld, val = _norm_field(m.group(1)), (m.group(2) or "").strip()
        else:
            m2 = KV_LINE.match(line)
            if not m2:
                # 续行：接到上一个字段
                if cur and last_field["name"]:
                    k = last_field["name"]
                    cur[k] = (str(cur.get(k, "")) + " " + line.strip()).strip()
                continue
            fld, val = _norm_field(m2.group(1)), (m2.group(2) or "").strip()
        if not fld:
            continue
        if fld == "title" and cur.get("title"):
            flush()
        if fld == "authors":
            cur.setdefault("authors", []).extend(_split_authors(val))
        elif fld == "keywords":
            cur.setdefault("keywords", []).extend(_split_keywords(val))
        elif fld == "abstract":
            cur["abstract"] = (cur.get("abstract", "") + " " + val).strip()
        else:
            cur[fld] = val
        last_field["name"] = fld
    flush()
    return records


def _parse_plain(text: str) -> list:
    """兜底：按空行分块，块内逐行猜测字段。

    **严格模式**：一块必须至少命中 2 个可识别字段，或命中 1 个字段且有年份，
    否则视为普通文本、不做成题录 —— 避免把随便一段文字当成一条文献。
    """
    blocks = re.split(r"\n\s*\n", (text or "").strip())
    records = []
    for b in blocks:
        lines = [l.strip() for l in b.splitlines() if l.strip()]
        if not lines:
            continue
        cur = {}
        matched_fields = 0
        for i, l in enumerate(lines):
            m = KV_LINE.match(l) or BRACKET_LINE.match(l)
            if m:
                fld = _norm_field(m.group(1))
                if fld:
                    val = (m.group(2) if m.lastindex and m.lastindex >= 2 else "").strip()
                    if fld == "authors":
                        cur.setdefault("authors", []).extend(_split_authors(val))
                    elif fld == "keywords":
                        cur.setdefault("keywords", []).extend(_split_keywords(val))
                    else:
                        cur[fld] = val
                    matched_fields += 1
                    continue
            if re.search(r"(19|20)\d{2}", l) and not cur.get("year"):
                cur["year"] = _year_of(l)
                matched_fields += 1
        # 严格模式：证据不足就不当作题录
        has_year = cur.get("year") is not None
        if matched_fields < 2 and not (matched_fields >= 1 and has_year):
            continue
        # 没有显式题名时，取最长的中文/英文行当标题
        if not cur.get("title"):
            cands = [l for l in lines if 6 <= len(l) <= 120 and not re.match(r"^[\d\s\W]+$", l)]
            if not cands:
                continue
            cur["title"] = _clean_title(max(cands, key=len))
        records.append(cur)
    return records


def _finalize(rec: dict) -> dict:
    """统一字段名与类型，补齐可用的默认值。"""
    authors = rec.get("authors") or []
    if isinstance(authors, str):
        authors = _split_authors(authors)
    title = _clean_title(rec.get("title") or "")
    year = rec.get("year")
    if not isinstance(year, int):
        year = _year_of(str(year or ""))
    if not year:
        # 刊名里若残留年份（"计算机学报 2021"），补出来
        year = _year_of(str(rec.get("venue") or ""))
    venue = _clean_venue(rec.get("venue") or "")
    abstract = (rec.get("abstract") or "").strip()
    keywords = rec.get("keywords") or []
    if isinstance(keywords, str):
        keywords = _split_keywords(keywords)

    return {
        "doc_type": "ref",
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": (rec.get("doi") or "").strip() or None,
        "url": (rec.get("url") or "").strip() or None,
        "volume": (rec.get("volume") or "").strip() or None,
        "issue": (rec.get("issue") or "").strip() or None,
        "pages": (rec.get("pages") or "").strip() or None,
        "keywords": keywords,
        "abstract": abstract,
        "ref_type": gbt7714.guess_type(venue, title),
        "source": "用户粘贴题录",
        "full_text": "",              # 题录没有全文，引用核对时会明确提示
    }


def parse_refs(text: str) -> list:
    """解析题录文本，返回结构化记录列表。"""
    text = (text or "").strip()
    if not text:
        return []
    fmt = detect_format(text)
    if fmt == "refworks":
        raw = _parse_refworks(text)
    elif fmt == "bracket":
        raw = _parse_kv(text)
    elif fmt == "kv":
        raw = _parse_kv(text)
    else:
        raw = _parse_plain(text)
    out = []
    for r in raw:
        r.pop("_last", None)
        f = _finalize(r)
        if f["title"]:
            out.append(f)
    return out


def append_to_corpus(records: list, original_name: str = "粘贴的题录") -> int:
    """把解析出的题录追加到语料库，返回新增条数。"""
    from .config import DATA_DIR
    from .literature import load_corpus

    corpus = load_corpus()
    recs = list(corpus.get("records", []))
    used = set()
    for r in recs:
        m = os.path.basename(r.get("local_pdf") or r.get("local_file") or "")
        if m:
            used.add(m)

    added = 0
    for r in records:
        idx = 1
        while ("ref%03d.json" % idx) in used:
            idx += 1
        target = "ref%03d.json" % idx
        used.add(target)
        r = dict(r)
        r["id"] = None
        # 题录没有本地文件，但需要一个唯一标识符用于列表展示与删除
        # （local_file 留空，表示"没有可删除的物理文件"）
        r["local_pdf"] = target
        r["local_file"] = ""
        r["ref_id"] = target
        r["cited_by_count"] = 0
        r["pdf_url"] = r.get("url") or ""
        r["original_name"] = original_name
        recs.append(r)
        added += 1

    corpus["records"] = recs
    corpus["count"] = len(recs)
    corpus.setdefault("query", "")
    corpus["refs_added_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(DATA_DIR, "corpus.json"), "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    return added
