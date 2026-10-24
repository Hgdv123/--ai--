"""文献获取与解析。

获取：OpenAlex 公开接口（无需密钥、无需登录），只下载标记为开放获取的论文。
解析：PyMuPDF 抽取全文、摘要、章节、参考文献区、局限句、贡献句。
本地优先：若本地已有 PDF，则不再联网。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import textutil
from .config import PDF_DIR, CORPUS_JSON, DOCS_JSON, ensure_dirs

UA = {"User-Agent": "WenMai/1.0 (student project; mailto:student@example.edu)"}
OPENALEX = "https://api.openalex.org/works"

# 单篇解析的页数上限。设得高是因为**文献表在文末**：截断页数会把文献表
# 整段切掉（实测 48 页综述在 40 页截断后"参考文献 0 条"）。200 页对普通
# 论文完全不设限，仅用于挡住异常巨大的文档。
_MAX_PARSE_PAGES = 200


# ────────────────────────── 获取 ──────────────────────────

def _get_json(url: str, timeout: int = 20, tries: int = 3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


# 出版商（MDPI、Springer 等）会对非浏览器 UA 直接返回 403，
# 用浏览器 UA 重试一次通常就能下到（实测 MDPI 第一次 403、换 UA 后成功）。
BROWSER_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/pdf,*/*",
}


def _download(url: str, path: str, timeout: int = 40, tries: int = 3):
    """下载 PDF 到 path，返回字节数。

    先用自己的 UA 试，被拒（403/406）再换浏览器 UA；仍失败则重试。
    原来只试一次且不重试，出版商一次 403 就会白白丢掉一篇好论文。
    """
    last = None
    for i in range(tries):
        headers = UA if i == 0 else BROWSER_UA
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if not data.startswith(b"%PDF"):
                raise ValueError("非 PDF 内容")
            with open(path, "wb") as f:
                f.write(data)
            return len(data)
        except Exception as e:
            last = e
            if os.path.isfile(path):          # 半截文件不能留下
                try:
                    os.remove(path)
                except Exception:
                    pass
            time.sleep(1.0)
    raise last


def _abstract_from_inverted(idx) -> str:
    if not idx:
        return ""
    pos = {}
    for word, ps in idx.items():
        for p in ps:
            pos[p] = word
    return " ".join(pos[k] for k in sorted(pos))


def import_openalex_work(work_id: str, verbose: bool = False) -> dict:
    """按 OpenAlex 作品 id 下载全文并加入文献库。

    用途：检查用户稿子时若发现"这条参考文献库里没有"，可由界面直接调用，
    把该文献补进文献库，从而让引用变得可核对——形成闭环。
    返回新加入的记录；没有可下载全文时返回 {}。
    """
    ensure_dirs()
    wid = (work_id or "").strip()
    if not wid:
        return {}
    # 归一化成 API 地址：OpenAlex 的**网页**地址（openalex.org/Wxxx）会返回 403，
    # 必须用 API 地址（api.openalex.org/works/Wxxx）。
    if wid.startswith("https://openalex.org/"):
        wid = wid.replace("https://openalex.org/", "https://api.openalex.org/works/", 1)
    elif wid.startswith("http://openalex.org/"):
        wid = wid.replace("http://openalex.org/", "https://api.openalex.org/works/", 1)
    elif re.match(r"^W\d+$", wid):
        wid = OPENALEX + "/" + wid
    try:
        w = _get_json(wid)
    except Exception:
        return {}
    if not isinstance(w, dict) or not w.get("id"):
        return {}

    loc = w.get("best_oa_location") or {}
    pdf_url = loc.get("pdf_url") or loc.get("landing_page_url")
    if not pdf_url:
        return {}

    # 分配不冲突的文件名（与上传流程一致：data/pdf/NNN.pdf）
    corpus = load_corpus()
    used = set()
    for r in corpus.get("records", []):
        m = os.path.basename(r.get("local_pdf") or r.get("local_file") or "")
        if m:
            used.add(m)
    idx = 1
    while ("%03d.pdf" % idx) in used:
        idx += 1
    name = "%03d.pdf" % idx
    path = os.path.join(PDF_DIR, name)
    try:
        size = _download(pdf_url, path)
    except Exception:
        return {}

    src = ((w.get("primary_location") or {}).get("source") or {})
    rec = {
        "id": w.get("id"),
        "title": (w.get("title") or "").strip(),
        "year": w.get("publication_year"),
        "venue": src.get("display_name") or "",
        "authors": [a.get("author", {}).get("display_name")
                    for a in (w.get("authorships") or [])][:10],
        "language": w.get("language"),
        "cited_by_count": w.get("cited_by_count") or 0,
        "doi": w.get("doi"),
        "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
        "pdf_url": pdf_url,
        "local_pdf": "pdf/" + name,
        "local_file": "pdf/" + name,
        "doc_type": "pdf",
        "pdf_bytes": size,
        "source": "OpenAlex（按引用补齐）",
    }
    records = list(corpus.get("records", []))
    records.append(rec)
    corpus["records"] = records
    corpus["count"] = len(records)
    corpus.setdefault("query", "")
    corpus["refilled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(CORPUS_JSON, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    if verbose:
        print("[补齐] 已加入：%s（%.1f MB）" % (rec["title"][:50], size / 1048576))
    # 立刻解析这一篇，使其马上可用于引用核对
    try:
        parsed = parse_one(rec, verbose=False)
        _sync_docs([parsed], replace_titles={rec["title"]})
    except Exception:
        pass
    return rec


_TITLE_STOP = {"a", "an", "the", "of", "for", "and", "on", "in", "with", "via",               "to", "using", "from", "by", "at", "its", "is", "are", "as"}
# 标题连续出现的最低相似度。实测校准：
#   · 同一篇论文的 PDF：0.96 ~ 1.00（标题换行、大小写、标点差异都不到 4%）
#   · 同一领域的另一篇论文：最高 0.768
#     （"Unsupervised Meta-Learning For Few-Shot Image Classification" 对
#      "Meta-Learning for Semi-Supervised Few-Shot Classification"）
# 0.85 落在两者之间，留有约 0.11 / 0.08 的双侧余量。
_TITLE_MIN_SIM = 0.85


def _title_norm(s: str) -> str:
    """标题归一化：只留下字母数字与汉字，消除大小写、连字符、空格差异。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def _title_trigrams(s: str) -> set:
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else ({s} if s else set())


def _title_sim(a: str, b: str) -> float:
    """字符三元组 Dice 相似度（对短文本比词袋更稳，能反映"连续短语"是否一致）。"""
    ta, tb = _title_trigrams(a), _title_trigrams(b)
    if not ta or not tb:
        return 0.0
    return 2.0 * len(ta & tb) / (len(ta) + len(tb))


def _pdf_matches_title(path: str, title: str, pages: int = 2) -> bool:
    """**内容级**校验：这份 PDF 首页是不是这篇论文。

    为什么必须查内容而不能只看文件名：实测踩到过——本地已存在 003.pdf 时
    fetch 直接复用，但那份文件其实是上一次运行留下的**另一篇**论文，
    于是元数据、卡片、引用核对全都张冠李戴，而表面一切"正常"。
    只看文件名或大小永远发现不了这种错配。

    又为什么不能只看"标题里的词出现没出现"：同一领域的论文共享
    meta-learning / few-shot 这类词，词命中法会放过 6/9 词都撞上的另一篇
    （实测）。所以改为比对**连续性**：标题必须作为一整串连续文本出现。

    只认**第 1 页**：arXiv 上常见"一份 PDF 里排多篇论文"或跨篇合集，
    第 2 页可能正好印着另一篇的标题（实测 pdf/004.pdf 如此），
    若连第 2 页也算就会把它误认成那篇。
    """
    if not os.path.isfile(path) or os.path.getsize(path) < 20000:
        return False
    t = _title_norm(title)
    if len(t) < 8:
        return True                       # 标题太短无法比对时不否决
    try:
        import fitz
        doc = fitz.open(path)
        head = [doc[0].get_text()] if doc.page_count else []
        doc.close()
    except Exception:
        return True                       # 读不了不当成错配，交给解析阶段报错
    lines = []
    for pg in head:
        for ln in (pg or "").split("\n"):
            ln = ln.strip()
            if len(ln) >= 12:
                lines.append(_title_norm(ln))
    best = 0.0
    for i, ln in enumerate(lines):
        best = max(best, _title_sim(t, ln))
        if best >= _TITLE_MIN_SIM:
            return True
        if i + 1 < len(lines):            # 标题常被排成两行
            best = max(best, _title_sim(t, ln + lines[i + 1]))
            if best >= _TITLE_MIN_SIM:
                return True
    return False


def _sync_docs(upsert: list = None, replace_titles: set = None) -> dict:
    """把 docs.json 与语料库对齐：先剔除已不在语料库中的条目，再插入/替换。

    为什么需要它：docs 是「有全文的文献」的解析结果。删除文献、或补齐文献后
    若只做局部增删，docs 会与语料库脱节（实测：删除后 docs 仍留着该篇，
    导致 docs 数与"非题录篇数"不一致，新鲜度被误判为"需要重新生成"）。
    """
    from .config import DATA_DIR as _DD  # noqa: F401
    corpus = load_corpus()
    live = {}
    for r in corpus.get("records", []):
        if (r.get("doc_type") or "pdf") == "ref":
            continue          # 题录没有全文，不进入 docs
        t = (r.get("title") or "").strip()
        if t:
            live[t] = True
    data = load_docs()
    keep = [d for d in data.get("docs", [])
            if (d.get("title") or "").strip() in live
            and (d.get("title") or "").strip() not in (replace_titles or set())]
    for u in (upsert or []):
        keep.append(u)
    out = {"query": corpus.get("query", ""), "count": len(keep),
           "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "docs": keep}
    with open(DOCS_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def fetch(query: str, limit: int = 8, oa_only: bool = True, verbose: bool = True) -> dict:
    """按研究方向检索并下载开放获取论文。返回 corpus 字典。"""
    ensure_dirs()
    filt = ["has_abstract:true"]
    if oa_only:
        filt.append("open_access.is_oa:true")
    params = {
        "search": query,
        "filter": ",".join(filt),
        "per-page": str(min(limit * 4, 100)),
        "mailto": "student@example.edu",
    }
    data = _get_json(OPENALEX + "?" + urllib.parse.urlencode(params))
    total = (data.get("meta") or {}).get("count", 0)
    if verbose:
        print("[获取] 检索「%s」命中 %s 篇，开始筛选可下载的开放获取论文…" % (query, total))

    records, skipped, mismatched = [], 0, 0
    for w in data.get("results", []):
        if len(records) >= limit:
            break
        loc = w.get("best_oa_location") or {}
        pdf_url = loc.get("pdf_url") or loc.get("landing_page_url")
        title = (w.get("title") or "").strip()
        if not pdf_url:
            skipped += 1
            continue
        name = "%03d.pdf" % (len(records) + 1)
        path = os.path.join(PDF_DIR, name)
        # 复用已下载的文件时**必须校验内容**：只按文件名复用会把上一次运行
        # 遗留的其它论文当成这一篇（实测踩到过，元数据与正文张冠李戴）。
        if os.path.isfile(path) and _pdf_matches_title(path, title):
            size = os.path.getsize(path)
        else:
            tmp = path + ".part"
            try:
                size = _download(pdf_url, tmp)
            except Exception as e:
                skipped += 1
                if verbose:
                    print("   [跳过] %s → %s" % (title[:40], str(e)[:60]))
                continue
            if not _pdf_matches_title(tmp, title):
                # 上游链接指向了别的论文（开放获取仓库常见）。宁可换一篇，
                # 也不能让"引用核对"拿错误的正文去核对用户的引用。
                mismatched += 1
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                if verbose:
                    print("   [跳过] %s → 该链接指向的 PDF 不是这篇论文" % title[:40])
                continue
            os.replace(tmp, path)
        src = ((w.get("primary_location") or {}).get("source") or {})
        records.append({
            "id": w.get("id"),
            "title": (w.get("title") or "").strip(),
            "year": w.get("publication_year"),
            "venue": src.get("display_name") or "",
            "authors": [a.get("author", {}).get("display_name")
                        for a in (w.get("authorships") or [])][:10],
            "language": w.get("language"),
            "cited_by_count": w.get("cited_by_count") or 0,
            "doi": w.get("doi"),
            "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
            "pdf_url": pdf_url,
            "local_pdf": os.path.relpath(path, os.path.dirname(CORPUS_JSON)).replace(os.sep, "/"),
            "local_file": os.path.relpath(path, os.path.dirname(CORPUS_JSON)).replace(os.sep, "/"),
            "doc_type": "pdf",
            "pdf_bytes": size,
            "source": "OpenAlex",
        })
        if verbose:
            print("   [%d] %s（%s，%.1f MB）" % (
                len(records), records[-1]["title"][:52], records[-1]["year"], size / 1048576))

    corpus = {"query": query, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "total_hit": total, "count": len(records), "skipped": skipped,
              "mismatched": mismatched, "records": records}
    with open(CORPUS_JSON, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    if verbose:
        msg = "[获取] 完成：%d 篇入库（跳过 %d 篇无开放全文/被拒" % (len(records), skipped)
        if mismatched:
            msg += "，%d 篇链接指向的 PDF 与标题不符" % mismatched
        print(msg + "）")
    return corpus


def load_corpus() -> dict:
    """读取语料库。

    文件缺失、损坏或字段类型不对时**降级为空库**而不是抛异常——
    否则一个坏掉的 JSON 会让所有命令（含 serve）直接崩溃，用户无从下手。
    """
    empty = {"query": "", "count": 0, "records": []}
    if not os.path.isfile(CORPUS_JSON):
        return dict(empty)
    try:
        with open(CORPUS_JSON, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return dict(empty)
    if not isinstance(d, dict):
        return dict(empty)
    recs = d.get("records")
    if not isinstance(recs, list):
        recs = []
    d["records"] = [r for r in recs if isinstance(r, dict)]
    if not isinstance(d.get("count"), int) or d.get("count") != len(d["records"]):
        d["count"] = len(d["records"])
    d.setdefault("query", "")
    return d


# ────────────────────────── 解析 ──────────────────────────

# 文献表标题行（可有编号前缀，如 "6 References" / "参考文献"）
_REF_HEAD = re.compile(
    r"(?im)^[ \t]*(?:\d{1,2}[ \t]*[.、]?[ \t]*)?"
    r"(?:references|bibliography|参\s*考\s*文\s*献)[ \t]*[:：]?[ \t]*$")
# 条目编号：[1] / (1) / 1. 1、
_REF_NUM = re.compile(r"(?m)^[ \t]*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.、])[ \t]*\S")


def _refs_region(full_text: str) -> str:
    """取正文末尾的文献表文本（找不到标题时返回空串）。

    为什么要取**最后一次**出现的标题：正文里常先出现一个叫 "References" 的
    小节（Related Work 的引用列表、章节内的分节标题），第一次命中的位置
    往往不是真正的文献表。取最后一次出现可稳定落到文末的文献表上。
    """
    m = _ref_head_match(full_text)
    if not m:
        return ""
    return (full_text or "")[m.end():]


def _count_refs(refs_text: str) -> int:
    """统计文献条目数，兼容编号制（[1]/1.）与作者-年份制（AAAI/CVPR 等）。"""
    if not refs_text:
        return 0
    n = len(_REF_NUM.findall(refs_text))
    if n >= 8:
        return n
    # 作者-年份制没有编号，改用"看起来像条目首行"的行来数：
    # "Abadi, M., Agarwal, A., ..." / "Finn, C., Abbeel, P., and Levine, S."
    ent = re.compile(
        r"(?m)^[ \t]*(?:[A-Z][\w'\u2019-]+[ \t]*,[ \t]*(?:[A-Z]\.|and\b|&)"
        r"|(?:19|20)\d\d[ \t]*[a-z]?[ \t]*[.、)]|\(\s*(?:19|20)\d\d)")
    n2 = len(ent.findall(refs_text))
    return n2 if n2 >= 3 else n


def _find_sections(full_text: str) -> dict:
    """定位各章节起始位置，返回 {章节名: 起始下标}。"""
    marks = {}
    for name, pat in textutil.SECTION_PATTERNS:
        m = re.search(pat, full_text)
        if m:
            marks[name] = m.start()
    return marks


def _heading_end(full_text: str, name: str) -> int:
    """返回章节**标题之后**的下标（-1 表示未找到）。

    中文期刊里摘要常写成"摘  要  小样本图像分类旨在…"，标题与正文同一行。
    因此不能只取标题起点：否则摘要标题后的正文被当成不存在（实测摘要为空），
    而其它章节会把标题行本身混进正文（"3 方 法 3.1 问题定义…"）。
    """
    for n, pat in textutil.SECTION_PATTERNS:
        if n != name:
            continue
        pat2 = pat.replace(r"\s*$", "")          # 去掉行尾锚点，允许标题后有正文
        m = re.search(pat2, full_text)
        return m.end() if m else -1
    return -1


def _slice_between(full_text: str, marks: dict, name: str, max_len: int = 4000) -> str:
    if name not in marks:
        return ""
    start = marks[name]
    he = _heading_end(full_text, name)
    if he > start:                                # 从标题之后开始切
        start = he
    later = [v for k, v in marks.items() if v > marks[name]]
    end = min(later) if later else len(full_text)
    return full_text[start:end][:max_len]


def _pick_sentences(text: str, cues: list, top_k: int = 3, max_len: int = 260,
                    require_strong: list = None) -> list:
    """按线索词挑出相关句子。

    require_strong 给定时，句子还必须命中其中至少一条，避免把套话当成正例。
    """
    out = []
    for s in textutil.split_sentences(text):
        if len(s) > max_len or len(s) < 15:
            continue
        if textutil.is_boilerplate(s):
            continue
        if require_strong is not None and not any(re.search(c, s) for c in require_strong):
            continue
        for c in cues:
            if re.search(c, s):
                out.append(textutil.strip_section_head(s.strip()))
                break
        if len(out) >= top_k:
            break
    return out


def _record_path(rec: dict) -> str:
    """定位记录对应的本地文件（PDF 或 Word）。题录没有文件时返回空字符串。

    注意：记录里的相对路径**可能来自不同来源**——`fetch` 用 os.path.relpath
    产生的是平台分隔符（Windows 上是 `pdf\\001.pdf`），而 `upload` 存的是
    `pdf/001.pdf`。这里统一归一化为正斜杠，否则 Windows 上会出现
    "文件明明存在却报文件不存在"（实测踩到过）。
    """
    rel = (rec.get("local_file") or "").replace("\\", "/")
    if not rel:
        lp = (rec.get("local_pdf") or "").replace("\\", "/")
        # pdf/xxx.pdf 或 docx/xxx.docx 才算物理文件
        if lp.startswith(("pdf/", "docx/")):
            rel = lp
    if not rel:
        return ""
    # 有目录前缀就直接拼，否则可能在 data/ 根下
    return os.path.join(os.path.dirname(CORPUS_JSON), rel.replace("/", os.sep))


def is_word_record(rec: dict) -> bool:
    rel = (rec.get("local_file") or rec.get("local_pdf") or "").lower()
    return rec.get("doc_type") == "docx" or rel.endswith(".docx")


def parse_one_word(rec: dict, verbose: bool = False) -> dict:
    """解析 Word 记录（复用 docx_parse）。"""
    from .docx_parse import parse_docx

    path = _record_path(rec)
    out = dict(rec)
    if not os.path.isfile(path):
        out["parse_error"] = "文件不存在"
        return out
    try:
        parsed = parse_docx(path, title_hint=rec.get("original_name") or path)
    except Exception as e:
        out["parse_error"] = "Word 解析失败：%s" % str(e)[:120]
        return out
    for k, v in parsed.items():
        if v not in (None, "", [], 0) or k not in out:
            out[k] = v
    out["local_pdf"] = rec.get("local_pdf") or rec.get("local_file")
    if verbose:
        print("   解析 %s：%d 字 / 章节 %s / 参考文献区 %d 条" % (
            os.path.basename(path), out.get("chars_total", 0),
            ",".join(out.get("sections_found") or []) or "未识别", out.get("ref_count", 0)))
    return out


# ───────────────────── 页眉/页脚识别（证据索引用） ─────────────────────
# 目的：把"每页重复出现的页眉/页脚/页码/arXiv 水印"从证据索引里去掉。
# 实测教训：这些行是**每页都有**的短行，任何谈论该论文的句子都与它们高度相似，
# 于是检索首条命中往往是页眉或论文标题本身，再被判定为"有支撑"——
# 支持率因此虚高。判定证据不能拿论文自己的抬头当证据。
# 明显不是正文的行：纯页码、"第 3 页"、arXiv 水印、期刊卷期页脚
_PAGENUM_RE = re.compile(r"^(?:[-–—\s]*\d{1,4}[-–—\s]*|第\s*\d{1,4}\s*页(?:\s*/\s*共\s*\d+\s*页)?|"
                         r"page\s*\d{1,4}(?:\s*of\s*\d{1,4})?)$", re.I)
_STAMP_RE = re.compile(r"^(?:arXiv:\S+|doi:\s*10\.\S+|https?://\S+)$", re.I)


def _is_pagenum_line(s: str, page_no: int = 0) -> bool:
    """判断某行是不是"页码"。

    为什么要带页号：论文表格里常有**单独成行的数字**（`0`、`1`、`2`、`3`），
    只看"这行是不是纯数字"会把它们当页码删掉——实测把一整页表格删成了空页。
    页码的特征是**数值等于所在页的页号**（允许 ±2 的偏移，因为页码可能从封面起算），
    或者形如"第 3 页""Page 3 of 12"这种带标记的写法。
    """
    s = (s or "").strip()
    if not s:
        return False
    m = re.match(r"^第\s*(\d{1,4})\s*页(?:\s*/\s*共\s*\d+\s*页)?$", s)
    if m:
        return True
    m = re.match(r"^page\s*(\d{1,4})(?:\s*of\s*\d{1,4})?$", s, re.I)
    if m:
        return True
    m = re.match(r"^[-–—\s]*(\d{1,4})[-–—\s]*$", s)
    if m and page_no:
        try:
            return abs(int(m.group(1)) - int(page_no)) <= 2
        except ValueError:
            return False
    return False

# 页码区间的重叠余量（字符）：段落会跨页，证据起点落在页首几十字里时，
# 不留重叠会把它算到上一页。200 字远小于一页正文，不会造成跨页混判。
_PAGE_OVERLAP = 200


def _page_lines(text: str) -> list:
    """取页面里长度合适的候选行（用于判断"是否每页都在重复"）。"""
    out = []
    for raw in (text or "").split("\n"):
        s = re.sub(r"\s+", " ", raw).strip()
        if 3 <= len(s) <= 100:
            out.append(s)
    return out


def repeated_page_lines(pages: list, min_pages: int = 3) -> set:
    """找出在 >= min_pages 个页面上重复出现的短行。

    页眉/页脚的特征就是"跨页重复"；正文段落不会在 3 页以上逐字重复。
    """
    counts = {}
    for t in pages or []:
        for s in set(_page_lines(t)):
            counts[s] = counts.get(s, 0) + 1
    return {s for s, c in counts.items() if c >= min_pages}


def _ref_head_match(text: str):
    """最后一个像文献表标题的位置（正则匹配对象），找不到返回 None。

    取**最后**一个：正文里先出现的 "References" 往往不是文献表
    （实测 MAML 那篇如此，认早了会数出 0 条）；
    且只认落在全文 40% 之后的，避免把分节标题当文献表。
    解析取区与证据索引排除必须用同一判据，否则两边对不上。
    """
    ms = [m for m in _REF_HEAD.finditer(text or "")
          if m.start() >= 0.4 * len(text or "")]
    return ms[-1] if ms else None


def _ref_start(text: str) -> int:
    """参考文献区在全文中的起点（找不到返回 -1）。"""
    m = _ref_head_match(text)
    return m.start() if m else -1


def strip_page_furniture(text: str, repeated: set, drop_refs: bool = False) -> tuple:
    """去掉页眉/页脚/水印（可选：参考文献区）。

    返回 (干净文本, 被忽略的区间表, 坐标平移表)：
      · 被忽略的区间要留下来——语料库页面要能回答"为什么这段没进证据索引"；
      · 坐标平移表把**原文位置**映射到**过滤后位置**，页码锚点要靠它平移，
        否则过滤把文本变短之后，页码会整体前移。

    drop_refs 默认 False：解析阶段**必须保留**参考文献区，
    否则 _refs_region/_count_refs 取不到文献表（实测 ref_count 直接从 22–178 掉到 0）。
    只有构建证据索引时才传 True —— 文献表里的条目讲的是**别人**的论文，
    拿它当"本论文的原文证据"是错的。
    """
    if not text:
        return "", [], []
    skip = []
    if drop_refs:
        m = _ref_head_match(text)
        if m:
            # 从标题**之后**开始排除：把 "References" 这行留在正文里，
            # 与 _refs_region 的切法一致（它也是从标题末尾之后取）。
            skip = [(m.end(), len(text))]
    parts, removed = [], []
    pos = 0
    page_no = 0
    for raw in re.split(r"(\n)", text):
        if raw == "\n":
            # clean_text 把连续空行压成 \n\n，所以一个空行标记着一页结束
            if pos > 0 and text[pos - 1] == "\n":
                page_no += 1
            parts.append(raw)
            pos += 1
            continue
        s = re.sub(r"\s+", " ", raw).strip()
        drop = ""
        if s and any(a <= pos < b for a, b in skip):
            drop = "参考文献区"
        elif s and (s in (repeated or ()) or _is_pagenum_line(s, page_no + 1)
                    or _STAMP_RE.match(s)):
            drop = "页眉/页脚"
        if drop:
            removed.append({"start": pos, "end": pos + len(raw), "kind": drop})
            parts.append("")
        else:
            parts.append(raw)
        pos += len(raw)
    out = "".join(parts)
    return out, removed, _filter_boundary(removed, len(text), len(out))


def _filter_boundary(removed: list, total: int, after_len: int = 0) -> list:
    """给出"原文位置 → 该位置之前被删掉的字符数"的关键帧表。

    只需要**边界**，不需要整篇逐字符映射：页码换算只关心每页的起点与终点。
    查表时用精确公式 过滤后下标 = 原文位置 − 累计删除数（见 _map_pos），
    而不是直接取关键帧的值——否则关键帧之间的位置会取到错的一端（实测）。
    """
    spans = sorted((max(0, min(int(s.get("start", 0)), total)),
                    max(0, min(int(s.get("end", 0)), total)))
                   for s in (removed or ())
                   if int(s.get("end", 0)) > int(s.get("start", 0)))
    keys, removed_before, cursor = [], 0, 0
    for a, b in spans:
        if a > cursor:
            keys.append((cursor, removed_before))
            keys.append((a, removed_before))
        removed_before += b - max(cursor, a)
        cursor = max(cursor, b)
        keys.append((cursor, removed_before))
    keys.append((total, removed_before))
    out = []
    for pos, r in keys:
        if out and pos <= out[-1][0]:
            continue
        out.append((pos, r))
    return out or [(0, 0)]


def _map_pos(pos: int, keys: list, after_len: int) -> int:
    """原文位置 → 过滤后位置：先查"该位置前删了多少"，再按公式算。"""
    lo, hi = 0, len(keys) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if keys[mid][0] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return max(0, min(after_len, pos - keys[lo][1]))


def _translate_spans(spans: list, keys: list, after_len: int = 0) -> list:
    """把页码区间从"过滤前"坐标平移到"过滤后"坐标。

    只映射**页边界**：页码换算只关心每页的起点与终点，
    整篇逐字符映射既慢又容易把删除区间内部压成一堆同值（实测导致末页塌陷）。
    两条硬规则：
      · 区间必须非空且单调 —— 空区间等于这一页不存在，内容会被算到上一页；
      · **末页必须覆盖到正文末尾** —— 否则正文最后一段的证据会被算成倒数第二页。
    """
    if not keys:
        return spans
    n = len(spans)
    for i, s in enumerate(spans):
        st = _map_pos(int(s.get("start", 0)), keys, after_len)
        # 终点是"切片边界"：允许等于正文长度
        en = _map_pos(int(s.get("end", 0)), keys, after_len)
        if en <= st and i + 1 < n:
            en = _map_pos(int(spans[i + 1].get("start", 0)), keys, after_len)
        s["start"], s["end"] = st, max(st + 1, en)
    # 逐页夹紧，保证单调不重叠
    for i in range(n - 1):
        if spans[i]["end"] > spans[i + 1]["start"]:
            spans[i]["end"] = max(spans[i]["start"] + 1, spans[i + 1]["start"])
    # 末页到底：末页若整体落在被删区间里，起点会顶到正文末尾，这里拉回一格
    last = spans[-1]
    if after_len:
        if last["start"] >= after_len:
            last["start"] = max(0, after_len - 1)
        last["end"] = max(last["start"] + 1, after_len)
    return spans


def page_spans(pages: list, text: str) -> list:
    """标出每页在 text 中的起止位置 → [{page, start, end}]。

    用**长度累加**定页码，不做文本搜索：text 由 pages 按顺序拼成
    （见 parse_one），每页清洗后的长度是精确已知的，
    所以第 i 页的起点就是前 i-1 页长度之和再加换行，天然单调、零歧义。

    为什么不用"拿每页开头一段去全文里找锚点"：
      · 每页页首都是同一行页眉，锚点毫无区分度；
      · 顺次搜索的游标一旦超前，后面的页就再也匹配不上，区间整段坍塌；
      · 实测 13 页论文里 6 页区间退化成 1 字符，页码大面积错。
    长度累加没有这些失效模式。
    """
    spans = []
    if not pages or not text:
        return spans
    off = 0
    for i, p in enumerate(pages, 1):
        n = len(p or "")
        if i > 1:
            off += 1                       # 拼接时插入的换行
        begin = max(0, off - _PAGE_OVERLAP)
        end = min(len(text), off + n + _PAGE_OVERLAP)
        spans.append({"page": i, "start": begin, "end": max(begin + 1, end)})
        off += n
    return spans


def parse_one(rec: dict, max_pages: int = _MAX_PARSE_PAGES, verbose: bool = False) -> dict:
    """解析单篇文档 → 结构化文献记录（自动分派 PDF / Word）。

    max_pages 的上限设得比较高（默认 _MAX_PARSE_PAGES）：**文献表在文末**，
    截断页数会把文献表整段切掉。实测一篇 48 页的综述在 40 页处截断后
    "参考文献 0 条"，而它的文献表正好在第 45–48 页。
    """
    # 题录只有元数据、没有全文，不参与解析（但保留在语料库中供引用核对定位）
    if (rec.get("doc_type") or "") == "ref" and not _record_path(rec):
        out = dict(rec)
        out["full_text"] = ""
        out["abstract"] = rec.get("abstract") or ""
        out["abstract_pdf"] = out["abstract"]
        out["no_fulltext"] = True
        if verbose:
            print("   [题录] %s（只有元数据，无全文）" % (rec.get("title") or "")[:44])
        return out

    if is_word_record(rec):
        return parse_one_word(rec, verbose=verbose)

    import fitz  # PyMuPDF

    path = _record_path(rec)
    if not path or not os.path.isfile(path):
        rec = dict(rec)
        rec["parse_error"] = "文件不存在"
        return rec

    doc = fitz.open(path)
    pages = min(doc.page_count, max_pages)
    page_texts = [doc[i].get_text() for i in range(pages)]
    # 逐页清洗后拼接：清洗会改空白，若拿"整篇清洗"的文本去对"原始页文本"的锚点，
    # 每页开头都会匹配到别处（实测 54 条证据有 20 条页码落到末页）。
    # 逐页清洗保证 page_spans 与 full 在同一坐标系里。
    pages_clean = [textutil.clean_text(t or "") for t in page_texts]
    full_raw = "\n".join(pages_clean)
    repeated = repeated_page_lines(page_texts)
    # 页码锚点必须在**过滤前**的文本上算：过滤会删掉整行、让文本变短，
    # 之后再用锚点定位就会整体前移。算完用 _removal_map 平移到过滤后的坐标。
    spans_raw = page_spans(pages_clean, full_raw)
    full, ignored, shift = strip_page_furniture(full_raw, repeated)
    if not full.strip():                      # 兜底：过滤过头就退回原文
        full, ignored, shift = full_raw, [], list(range(len(full_raw) + 1))
    spans = _translate_spans(spans_raw, shift, len(full))
    marks = _find_sections(full)
    # 摘要：优先取 PDF 正文（比 OpenAlex 的倒排索引更全）
    abstract = textutil.clean_text(_slice_between(full, marks, "abstract", 2500))
    # 剥离残留的标题前缀：中文期刊写作"摘  要"（字间有空格），
    # 只写"摘要"会剥离失败、标题混进摘要（实测踩到过）。
    abstract = re.sub(r"(?i)^\s*(?:abstract(?![a-z])|摘\s*要)[\s:：]*", "",
                      abstract).strip()
    if len(abstract) < 120:
        abstract = rec.get("abstract") or abstract
    abstract = abstract[:1800]

    method = _slice_between(full, marks, "method", 3000)
    experiment = _slice_between(full, marks, "experiment", 3000)
    conclusion = _slice_between(full, marks, "conclusion", 2000)
    related = _slice_between(full, marks, "related", 2000)

    # 参考文献区：不从 _find_sections 取，因为它记录的是**首次**命中的位置，
    # 而正文里先出现的 "References" 往往不是文献表（实测 MAML 那篇如此，
    # 导致明明有文献表却数出 0 条）。
    refs_text = _refs_region(full)
    ref_count = _count_refs(refs_text)
    # 文献区**留在正文里**（_refs_region/_count_refs 要用它），但**不进证据索引**——
    # 文献表里的条目讲的是别人的论文，拿它当"本论文的原文证据"是错的。
    # 这里只把它记成"未索引区间"，供界面解释；真正的排除在 EvidenceRetriever 里做。
    ref_head = _ref_head_match(full)
    if ref_head:
        ignored = list(ignored) + [{"start": ref_head.end(), "end": len(full),
                                    "kind": "参考文献区"}]

    limitations = _pick_sentences(conclusion + "\n" + full, textutil.LIMITATION_CUES, 3,
                                  require_strong=textutil.LIMITATION_STRONG)
    contributions = _pick_sentences(
        (related + "\n" + method + "\n" + abstract), textutil.CONTRIBUTION_CUES, 3)
    # 结论句：摘要末两句
    concl_sents = textutil.split_sentences(abstract)[-2:] if abstract else []

    rec = dict(rec)
    rec.update({
        "pages": doc.page_count,
        "chars_total": len(full),
        "sections_found": sorted(marks.keys()),
        "abstract_pdf": abstract,
        "method_text": method,
        "experiment_text": experiment,
        "conclusion_text": conclusion,
        "related_text": related,
        "ref_count": ref_count,
        "limitation_sentences": limitations,
        "contribution_sentences": contributions,
        "conclusion_sentences": concl_sents,
        "keywords": textutil.keywords(abstract + " " + method, 12),
        "full_text": full,
        # 证据核对要能把命中的原文段落回指到具体页码 —— 用户得能翻过去自己看，
        # 否则"有支撑"只是一句无法验证的断言。
        "page_spans": spans,
        # 未进**证据索引**的区间（页眉/页脚在此；文献区在下面单独补记）。
        # 保留它，界面才能回答"为什么这段没被当作证据"。
        "ignored_spans": ignored,
        # 跨页重复出现的行（页眉/页脚）。检索端要能在不重新解析的前提下
        # 用同一口径过滤，否则"索引里有没有页眉"会因入口不同而不一致。
        "repeated_lines": sorted(repeated),
    })
    if verbose:
        print("   解析 %s：%d 页 / %d 字 / 章节 %s / 参考文献约 %d 条" % (
            os.path.basename(path), doc.page_count, len(full),
            ",".join(rec["sections_found"]) or "未识别", ref_count))
    return rec


def parse_all(max_pages: int = _MAX_PARSE_PAGES, verbose: bool = True) -> dict:
    """解析语料库中需要全文的文档，写出 docs.json。

    注意：**题录（doc_type=ref）没有全文，不进入 docs**。
    此前把题录也塞进 docs，会使 docs 数比"非题录篇数"多，
    导致新鲜度判断误报"有新论文尚未处理"（实测踩到过）。
    题录仍保留在语料库中，供引用匹配与参考文献表使用。
    """
    corpus = load_corpus()
    docs = []
    todo = [r for r in corpus.get("records", [])
            if (r.get("doc_type") or "pdf") != "ref"]
    if verbose:
        print("[解析] 共 %d 篇待处理（另有 %d 条题录，无需全文）" % (
            len(todo), corpus.get("count", 0) - len(todo)))
    for rec in todo:
        # 用户上传的文件：标题若只是文件名（如 "001"），用**原始文件名**兜底。
        # 否则卡片与参考文献表里会出现 "001[EB/OL]. n.d.." 这种没有意义的条目
        #（实测踩到过：上传 6 篇后卡片标题全是 001..006）。
        rec = dict(rec)
        title = (rec.get("title") or "").strip()
        orig = (rec.get("original_name") or "").strip()
        if orig:
            stem = os.path.splitext(orig)[0].strip()
            if (not title) or (len(title) <= 12 and title.isdigit()) or title == stem[:120]:
                if stem and not stem.isdigit():
                    rec["title"] = stem[:120]
                    rec["title_from"] = "original_name"
        try:
            docs.append(parse_one(rec, max_pages=max_pages, verbose=verbose))
        except Exception as e:
            rec = dict(rec)
            rec["parse_error"] = str(e)[:200]
            docs.append(rec)
            if verbose:
                print("   [失败] %s → %s" % ((rec.get("title") or "")[:40], str(e)[:80]))
    out = {"query": corpus.get("query", ""), "count": len(docs),
           "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "docs": docs}
    with open(DOCS_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    if verbose:
        ok = sum(1 for d in docs if not d.get("parse_error"))
        print("[解析] 完成：%d/%d 篇成功 → %s" % (ok, len(docs), DOCS_JSON))
    return out


def looks_like_non_paper(doc: dict) -> bool:
    """解析结果看起来**不是学术论文**吗（用于提醒用户可能传错了文件）。

    判据：既没有任何论文章节（摘要/引言/方法/实验/结论/参考文献…），
    也没有参考文献表。真论文几乎不可能两者皆无。
    实测案例：用户把"宣传视频策划案"传进了文献库，文档里没有一节符合论文结构，
    但界面只显示"就绪"，看不出问题，还会被当成综述素材。
    """
    if not doc:
        return False
    text = (doc.get("full_text") or "").strip()
    if not text:
        return False                       # 空正文另有"无正文"状态，不重复报
    if doc.get("sections_found"):
        return False
    if doc.get("ref_count"):
        return False
    return True


def load_docs() -> dict:
    if not os.path.isfile(DOCS_JSON):
        return {"count": 0, "docs": []}
    with open(DOCS_JSON, encoding="utf-8") as f:
        return json.load(f)
