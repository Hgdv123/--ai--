"""Word 导出：把综述草稿导出为可直接使用的 .docx。

包含：标题、研究现状正文（带引用角标）、参考文献（GB/T 7714 格式）、自检报告附录。
中文字体显式设置 eastAsia，避免 Word 里显示为方框。
"""
from __future__ import annotations

import os
import re
import time

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, Cm, RGBColor

from .config import DOCX_OUT, OUT_DIR, ensure_dirs

FONT = "微软雅黑"
INK = RGBColor(0x1A, 0x1D, 0x24)
BRAND = RGBColor(0x1F, 0x5F, 0xD0)
GREY = RGBColor(0x6B, 0x72, 0x80)


def _set(run, size=10.5, bold=False, color=INK):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)


def _setup(doc):
    st = doc.styles["Normal"]
    st.font.name = FONT
    st.font.size = Pt(10.5)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin = sec.bottom_margin = Cm(2.3)
    sec.left_margin = sec.right_margin = Cm(2.4)
    hdr = sec.header.paragraphs[0]
    hdr.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _set(hdr.add_run("文脉 WenMai 生成 · 综述初稿"), size=8, color=GREY)


def export(draft: dict, check: dict = None, verbose: bool = True) -> str:
    """draft 来自 draft.generate()；check 来自 selfcheck.self_check()。"""
    ensure_dirs()
    doc = Document()
    _setup(doc)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(4)
    _set(p.add_run("研究现状"), size=20, bold=True, color=BRAND)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.paragraph_format.space_after = Pt(14)
    _set(sub.add_run("文脉 WenMai 自动生成初稿 · %s" % time.strftime("%Y-%m-%d %H:%M")),
         size=9, color=GREY)

    tip = doc.add_paragraph()
    tip.paragraph_format.space_after = Pt(12)
    _set(tip.add_run("说明：本稿由文脉基于所导入文献自动起草，引用编号与参考文献表由程序生成。"
                     "请作者逐句核对后再使用；点击引用角标可回溯原文。"), size=9, color=GREY)

    # 正文
    body = draft.get("markdown", "")
    m = re.search(r"(?m)^#{1,3}\s*(?:参考文献|References)\s*$", body)
    body_text = body[:m.start()] if m else body
    body_text = re.sub(r"(?m)^#{1,3}.*$", "", body_text).strip()
    for para in [x.strip() for x in body_text.split("\n") if x.strip()]:
        pp = doc.add_paragraph()
        pf = pp.paragraph_format
        pf.space_after = Pt(8)
        pf.line_spacing = 1.5
        pf.first_line_indent = Cm(0.74)
        _set(pp.add_run(para), size=10.5)

    # 参考文献
    h = doc.add_paragraph()
    h.paragraph_format.space_before = Pt(14)
    h.paragraph_format.space_after = Pt(6)
    _set(h.add_run("参考文献"), size=13, bold=True, color=BRAND)
    for r in draft.get("refs", []):
        pp = doc.add_paragraph()
        pf = pp.paragraph_format
        pf.space_after = Pt(3)
        pf.line_spacing = 1.4
        pf.left_indent = Cm(0.85)
        pf.first_line_indent = Cm(-0.85)
        _set(pp.add_run(r["text"]), size=9.5)

    # 自检附录
    if check and check.get("issues"):
        h2 = doc.add_paragraph()
        h2.paragraph_format.space_before = Pt(16)
        h2.paragraph_format.space_after = Pt(6)
        _set(h2.add_run("附：引用自检报告"), size=13, bold=True, color=BRAND)
        s = check["summary"]
        pp = doc.add_paragraph()
        pp.paragraph_format.space_after = Pt(8)
        _set(pp.add_run("共发现 %d 处问题（高 %d / 中 %d / 低 %d）。" % (
            s["total"], s["high"], s["medium"], s["low"])), size=10)
        for i in check["issues"][:40]:
            ip = doc.add_paragraph()
            ipf = ip.paragraph_format
            ipf.space_after = Pt(2)
            ipf.left_indent = Cm(0.6)
            ipf.line_spacing = 1.35
            _set(ip.add_run("［%s］%s —— %s（建议：%s）" % (
                i["severity"], i["kind"], i["detail"], i["suggestion"])), size=9)

    doc.save(DOCX_OUT)
    if verbose:
        print("[导出] Word 已生成：%s" % DOCX_OUT)
    return DOCX_OUT


def export_matrix_excel(verbose: bool = True) -> str:
    from .matrix import to_excel
    ensure_dirs()
    path = os.path.join(OUT_DIR, "文献对比矩阵.xlsx")
    real = to_excel(path)
    if verbose:
        print("[导出] 矩阵已生成：%s" % real)
    return real
