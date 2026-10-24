"""Word 文档解析：把 .docx 转成与 PDF 一致的结构化记录。

用途分两类：
1. 论文类 Word（导师给的文稿、期刊模板）→ 与 PDF 一样参与卡片抽取与对比矩阵；
2. **自己的论文初稿** → 用「引用自检」检查引了什么、缺什么引用。

实现方式：用 python-docx 读取段落与表格，按"标题样式 + 编号 + 字号"识别章节，
抽取摘要、方法、结论、参考文献区、局限句，输出与 literature.parse_one 相同的字段结构。
"""
from __future__ import annotations

import os
import re

from . import textutil

# Word 里常见的中英文章节标题（用于把正文切成章节）
SECTION_PATTERNS = [
    ("abstract", r"^\s*(?:abstract|摘\s*要)\s*[:：]?$"),
    ("introduction", r"^\s*(?:\d{1,2}[\.、]?\s*)?(?:introduction|引\s*言|绪\s*论)\s*$"),
    ("related", r"^\s*(?:\d{1,2}[\.、]?\s*)?(?:related\s+work|相关工作|文献综述|研究现状)\s*$"),
    ("method", r"^\s*(?:\d{1,2}[\.、]?\s*)?(?:method(?:s|ology)?|approach|proposed\s+method|"
               r"方\s*法|研究方法|模型|系统设计)\s*$"),
    ("experiment", r"^\s*(?:\d{1,2}[\.、]?\s*)?(?:experiment(?:s)?|evaluation|results?|"
                   r"实\s*验|实验结果|结果与分析)\s*$"),
    ("conclusion", r"^\s*(?:\d{1,2}[\.、]?\s*)?(?:conclusion(?:s)?|discussion|"
                   r"结\s*论|结论与展望|总结与展望|讨\s*论)\s*$"),
    ("references", r"^\s*(?:references|bibliography|参\s*考\s*文\s*献)\s*$"),
]

REF_LINE = re.compile(
    r"^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[\.、])\s*\S")


def _is_heading(para) -> bool:
    """判断段落是否为标题：靠 Word 样式名 + 长度 + 编号特征。"""
    name = (para.style.name or "") if para.style is not None else ""
    if re.search(r"(?i)heading|标题", name):
        return True
    return False


def _para_texts(doc) -> list:
    """按文档顺序取出段落文本（含表格内文本），过滤空行。"""
    out = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            out.append((t, _is_heading(p)))
    for tbl in doc.tables:
        for row in tbl.rows:
            cells = []
            for cell in row.cells:
                ct = " ".join(x.strip() for x in (cell.text or "").split("\n") if x.strip())
                if ct:
                    cells.append(ct)
            if cells:
                out.append((" | ".join(cells), False))
    return out


def parse_docx(path: str, title_hint: str = "") -> dict:
    """解析 .docx，返回与 PDF 解析一致的结构。"""
    from docx import Document

    doc = Document(path)
    items = _para_texts(doc)
    lines = [t for t, _ in items]

    # 标题：优先取第一个非空段落（若不像标题则用文件名）
    title = ""
    if lines:
        first = lines[0].strip()
        if 4 <= len(first) <= 120 and not first.endswith(("。", ".")):
            title = first
    title = title or re.sub(r"\.docx?$", "", os.path.basename(title_hint or path), flags=re.I)

    # 按标题定位章节起点
    marks = {}
    for idx, (t, is_head) in enumerate(items):
        for name, pat in SECTION_PATTERNS:
            if name in marks:
                continue
            # 标题样式 或 文本完全匹配模式
            if (is_head or len(t) <= 40) and re.match(pat, t, re.I):
                marks[name] = idx

    def slice_between(name: str, max_items: int = 60) -> str:
        if name not in marks:
            return ""
        start = marks[name] + 1
        later = [v for k, v in marks.items() if v > marks[name]]
        end = min(later) if later else len(lines)
        return "\n".join(lines[start:min(end, start + max_items)])

    full_text = "\n".join(lines)
    abstract = textutil.clean_text(slice_between("abstract", 12))
    if len(abstract) < 60:
        # 没有摘要标题时：取正文前若干段作为摘要候选
        abstract = "\n".join(lines[1:8])[:1200]

    method_text = slice_between("method", 80)
    experiment_text = slice_between("experiment", 60)
    conclusion_text = slice_between("conclusion", 40)
    related_text = slice_between("related", 40)

    # 参考文献区与条目数
    refs_text = ""
    ref_count = 0
    if "references" in marks:
        refs_text = "\n".join(lines[marks["references"] + 1:])
        ref_count = sum(1 for l in refs_text.splitlines() if REF_LINE.match(l))
    else:
        # 没有"参考文献"标题时：从尾部找连续编号行
        tail = lines[-80:] if len(lines) > 80 else lines
        cnt = sum(1 for l in tail if REF_LINE.match(l))
        if cnt >= 3:
            ref_count = cnt
            refs_text = "\n".join([l for l in tail if REF_LINE.match(l)])

    # 局限句与贡献句
    limitations = _pick(lines, textutil.LIMITATION_CUES, textutil.LIMITATION_STRONG)
    contributions = _pick(lines, textutil.CONTRIBUTION_CUES)

    # 年份：从全文找 19xx/20xx
    ym = re.findall(r"\b(20\d{2}|19\d{2})\b", full_text)
    year = int(sorted(ym)[-1]) if ym else None

    return {
        "doc_type": "docx",
        "title": title,
        "year": year,
        "venue": "",
        "authors": [],
        "language": "zh" if len(re.findall(r"[\u4e00-\u9fff]", full_text)) > len(full_text) * 0.2 else "en",
        "cited_by_count": 0,
        "doi": None,
        "abstract": abstract,
        "abstract_pdf": abstract,
        "method_text": method_text,
        "experiment_text": experiment_text,
        "conclusion_text": conclusion_text,
        "related_text": related_text,
        "full_text": full_text,
        "pages": None,
        "chars_total": len(full_text),
        "sections_found": sorted(marks.keys()),
        "ref_count": ref_count,
        "refs_text": refs_text,
        "limitation_sentences": limitations,
        "contribution_sentences": contributions,
        "conclusion_sentences": textutil.split_sentences(abstract)[-2:] if abstract else [],
        "keywords": textutil.keywords(abstract + " " + method_text, 12),
        "source": "用户上传（Word）",
    }


def _pick(lines: list, cues: list, strong: list = None, top_k: int = 3,
          max_len: int = 260) -> list:
    out = []
    for t in lines:
        for s in textutil.split_sentences(t):
            s = s.strip()
            if len(s) > max_len or len(s) < 15:
                continue
            if textutil.is_boilerplate(s):
                continue
            if strong is not None and not any(re.search(c, s) for c in strong):
                continue
            if any(re.search(c, s) for c in cues):
                out.append(textutil.strip_section_head(s))
                break
        if len(out) >= top_k:
            break
    return out


def is_docx(filename: str) -> bool:
    return filename.lower().endswith((".docx", ".doc"))


def save_as_text(docx_path: str, text_dir: str) -> str:
    """解析并落盘为 .txt，供后续按普通文本处理（同时返回路径）。"""
    rec = parse_docx(docx_path)
    os.makedirs(text_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(docx_path))[0]
    out = os.path.join(text_dir, base + ".txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(rec.get("full_text") or "")
    return out
