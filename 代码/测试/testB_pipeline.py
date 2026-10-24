# -*- coding: utf-8 -*-
"""全套测试 · 第二批：数据处理层 + 全流程 + 导出。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as ENV  # noqa: E402

ENV.activate()
os.environ["PYTHONIOENCODING"] = "utf-8"
R = {"pass": [], "fail": [], "warn": []}


def ck(name, ok, detail=""):
    (R["pass"] if ok else R["fail"]).append({"name": name, "detail": str(detail)[:200]})
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:90]) if detail else ""))


# ══════════ 1. literature（解析层） ══════════
print("=" * 70)
print("1. literature：文献加载与解析")
print("=" * 70)
from wenmai.literature import (load_corpus, load_docs, parse_one, parse_all,
                               is_word_record, _record_path, parse_one_word)

corpus = load_corpus()
ck("load_corpus 有记录", corpus.get("count", 0) == 6, corpus.get("count"))
ck("语料记录字段完整",
   all(k in corpus["records"][0] for k in ("title", "local_pdf", "doc_type")),
   list(corpus["records"][0].keys())[:8])
# 逐条校验：少数字段不能让"整条记录"蒙混过关——曾经因为语料库是旧版本生成的，
# fetch 写入的 local_file/doc_type 缺失，而只查第一条时刚好漏过。
_bad = [r.get("local_pdf") for r in corpus["records"]
        if not (r.get("title") or "").strip()
        or not (r.get("local_pdf") or "").strip()
        or (r.get("doc_type") or "pdf") not in ("pdf", "docx", "ref")]
ck("每条记录都带 title/local_pdf/合法 doc_type", not _bad, _bad[:5])

docs = load_docs()
ck("load_docs 有结果", docs.get("count", 0) == 6, docs.get("count"))
d0 = docs["docs"][0]
ck("解析含全文", len(d0.get("full_text") or "") > 1000, len(d0.get("full_text") or ""))
ck("解析含章节", bool(d0.get("sections_found")), d0.get("sections_found"))
ck("解析无错误", not d0.get("parse_error"), d0.get("parse_error"))

# 题录不参与解析
ref_rec = {"doc_type": "ref", "title": "题录测试", "local_pdf": "ref001.json", "local_file": ""}
ck("is_word_record 对题录返回 False", is_word_record(ref_rec) is False)
ck("_record_path 对题录返回空", _record_path(ref_rec) == "", repr(_record_path(ref_rec)))
out = parse_one(ref_rec, verbose=False)
ck("题录解析不报错且标记 no_fulltext", out.get("no_fulltext") is True and not out.get("parse_error"))

# 不存在的文件
bad = {"doc_type": "pdf", "title": "不存在", "local_pdf": "pdf/zzz.pdf", "local_file": "pdf/zzz.pdf"}
ob = parse_one(bad, verbose=False)
ck("不存在的文件返回 parse_error", bool(ob.get("parse_error")), ob.get("parse_error"))

# parse_all 可重复运行
r2 = parse_all(verbose=False)
ck("parse_all 幂等（仍 6 篇成功）",
   sum(1 for x in r2["docs"] if not x.get("parse_error")) == 6,
   sum(1 for x in r2["docs"] if not x.get("parse_error")))

# ══════════ 2. cards（卡片抽取） ══════════
print()
print("=" * 70)
print("2. cards：文献卡片抽取（离线规则通道）")
print("=" * 70)
from wenmai.cards import extract_local, enhance_with_llm, load_cards, build_cards
from wenmai.llm import LLMClient

card = extract_local(d0)
ck("extract_local 返回 fields", "fields" in card and "confidence" in card, list(card.keys()))
ck("extract_local source=local", card["source"] == "local")
ck("extract_local 6 个字段齐全",
   all(k in card["fields"] for k in ("problem", "method", "datasets", "metrics",
                                     "limitation", "takeaway")), list(card["fields"].keys()))
ck("置信度取值 0~1",
   all(0.0 <= v <= 1.0 for v in card["confidence"].values()), card["confidence"])

from wenmai import local_llm as _LLB
_orig_fm2 = _LLB.find_model
_LLB.find_model = lambda names: ""          # 模拟无本地模型
try:
    c_off = LLMClient(api_key="")
    ck("无模型时不调用增强（返回原卡）",
       enhance_with_llm(dict(card), d0, c_off).get("source") == "local",
       enhance_with_llm(dict(card), d0, c_off).get("llm_note"))
finally:
    _LLB.find_model = _orig_fm2

ck("空文档不崩", "fields" in extract_local({}))
ck("只有标题不崩", "fields" in extract_local({"title": "x"}))

cards = load_cards()
if cards.get("count", 0) != 6:
    # 自备前置数据：cards.json 由上一阶段产出，可能是被清空或还没生成过。
    # 原来这里直接断言"已有卡片"，于是这条用例其实在测"上一次有没有跑过"，
    # 而不是在测卡片抽取——一旦数据被清就红，掩盖了真正的问题。
    print("     （cards.json 缺失，先构建一次）")
    build_cards(use_llm=False, verbose=False)
    cards = load_cards()
ck("load_cards 得到 6 张卡片", cards.get("count", 0) == 6, cards.get("count"))

# ══════════ 3. matrix（对比矩阵） ══════════
print()
print("=" * 70)
print("3. matrix：对比矩阵与聚类")
print("=" * 70)
from wenmai.matrix import build_matrix, cluster_topics, to_excel, load_matrix

m = build_matrix(verbose=False)
ck("矩阵行数=文献数", m["count"] == 6, m["count"])
ck("矩阵含 9 列", len(m["columns"]) == 9, len(m["columns"]))
ck("矩阵每行字段齐全",
   all(k in m["rows"][0] for k in ("title", "problem", "method", "datasets", "metrics", "limitation")))
ck("主题聚类有结果", len(m["topics"]) > 0, [t["name"] for t in m["topics"]])
ck("聚类过滤了分词残片",
   all(t["name"] not in ("classi", "recognitio", "imag") for t in m["topics"]),
   [t["name"] for t in m["topics"]])
ck("cluster_topics 空输入返回空", cluster_topics([]) == [])

# 写进调试区而不是 outputs/：这是测试中间件，混在交付产物里会让人分不清
# 哪些是"给用户的东西"、哪些是测试留下的（实测清理时就要专门辨认它）。
xl = to_excel(os.path.join(ENV.tmp_dir(), "_test_matrix.xlsx"))
ck("导出 Excel/CSV 成功", os.path.isfile(xl), os.path.basename(xl))
if xl.endswith(".xlsx"):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(xl)
        ws = wb.active
        ck("Excel 内容正确（%d 行）" % ws.max_row, ws.max_row == 7, ws.max_row)
    except Exception as e:
        ck("Excel 可读", False, repr(e)[:70])

# ══════════ 4. draft（草稿生成） ══════════
print()
print("=" * 70)
print("4. draft：草稿生成（离线模板通道）")
print("=" * 70)
from wenmai.draft import generate, load_draft, renumber_by_citation_order, _clean_field

ck("_clean_field 拒绝英文原文",
   _clean_field("We propose an algorithm for meta-learning that is model-agnostic") == "",
   repr(_clean_field("We propose an algorithm for meta-learning")))
ck("_clean_field 保留中文概括",
   _clean_field("提出模型无关的元学习算法") == "提出模型无关的元学习算法")
ck("_clean_field 拒绝超长", _clean_field("中" * 200) == "")
ck("_clean_field 去章节标题",
   _clean_field("Methodology 本文提出改进方法") == "本文提出改进方法",
   repr(_clean_field("Methodology 本文提出改进方法")))
ck("_clean_field 空输入", _clean_field("") == "" and _clean_field(None) == "")

# 重编号
body, refs, mp = renumber_by_citation_order(
    "先说[3]。再说[1]。最后[3]。",
    [{"index": 1, "cite": "[1]", "text": "[1] A", "title": "a"},
     {"index": 3, "cite": "[3]", "text": "[3] B", "title": "b"}])
ck("重编号按首次出现顺序", "[1]" in body and "[2]" in body and mp == {3: 1, 1: 2}, (body, mp))
ck("未引用条目被剔除", len(refs) == 2, len(refs))
ck("重编号空输入不崩", renumber_by_citation_order("", [])[1] == [])

d_off = generate(use_llm=False, verbose=False)
ck("离线生成成功", d_off.get("llm_used") is False and len(d_off["refs"]) > 0,
   (d_off.get("llm_note"), len(d_off["refs"])))
ck("离线正文标明是模板", "模板" in (d_off.get("llm_note") or ""), d_off.get("llm_note"))
ck("草稿含 source_papers", isinstance(d_off.get("source_papers"), int), d_off.get("source_papers"))
ck("草稿含参考文献章节", "参考文献" in d_off["markdown"])
ck("离线正文无英文原文混入",
   not any(s in d_off["markdown"] for s in ("We propose an algorithm", "Related Work The")),
   [s for s in ("We propose an algorithm", "Related Work The") if s in d_off["markdown"]])

# ══════════ 5. selfcheck（自检 10 项） ══════════
print()
print("=" * 70)
print("5. selfcheck：自检与埋错测试")
print("=" * 70)
from wenmai.selfcheck import (run_checks, make_error_dataset, decompose_claims,
                              _split_draft, _parse_refs, _cited_order, _norm_title_ref)

md = load_draft()["markdown"]
parts = _split_draft(md)
ck("_split_draft 分离正文与文献", len(parts["body"]) > 0 and len(parts["refs_block"]) > 0,
   (len(parts["body"]), len(parts["refs_block"])))
refs_p = _parse_refs(parts["refs_block"])
ck("_parse_refs 解析条目", len(refs_p) > 0, len(refs_p))
ck("_cited_order 展开合并编号",
   _cited_order("引用[1-3]和[5]") == [1, 2, 3, 5], _cited_order("引用[1-3]和[5]"))
ck("_norm_title_ref 可比较", len(_norm_title_ref("[1] A. Some Title[J]. J, 2020.")) > 0)

claims = decompose_claims(parts["body"])
ck("decompose_claims 拆出论断", len(claims) > 0, len(claims))
ck("decompose_claims 展开编号",
   all(isinstance(c["citations"], list) for c in claims))
ck("decompose_claims 空输入", decompose_claims("") == [])

res = run_checks(md)
ck("run_checks 返回 issues+summary", "issues" in res and "summary" in res)
ck("summary 含 10 项检查统计", "by_kind" in res["summary"] and "total" in res["summary"])

# 埋错测试
bench = make_error_dataset(md)
ck("埋错测试 4 类", bench["total"] == 4, bench["total"])
ck("埋错测试全检出", bench["recall"] == 1.0, bench["recall"])

# 人工构造各类型问题，验证检出
FAKE = """# 研究现状
这是一段足够长的陈述句，用来测试引用真实性的检查逻辑[1]。
这里引用了不存在的编号[99]。
顺序测试先说[3]再说[2]，编号不是递增的。
没有任何引用支撑的论断句，长度也足够长以便被检测到。
## 参考文献
[1] A One. Test Title[J]. Journal, 2022.
[1] A One. Test Title[J]. Journal, 2022.
[3] B Two. Missing Type. Journal, 2022.
"""
r = run_checks(FAKE)
kinds = {i["kind"] for i in r["issues"]}
for k in ("引用真实性", "引用越界", "编号顺序", "重复著录", "格式合规", "论断有据"):
    ck("检出「%s」" % k, k in kinds, sorted(kinds))

# ══════════ 6. agent（工具层） ══════════
print()
print("=" * 70)
print("6. agent：工具与降级")
print("=" * 70)
from wenmai.agent import TOOL_SCHEMAS, DISPATCH, ReviewAgent
import wenmai.selfcheck as SC

ck("工具 schema 9 个", len(TOOL_SCHEMAS) == 9, len(TOOL_SCHEMAS))
ck("每个工具都有实现", all(t["function"]["name"] in DISPATCH for t in TOOL_SCHEMAS),
   [t["function"]["name"] for t in TOOL_SCHEMAS if t["function"]["name"] not in DISPATCH])
for name in DISPATCH:
    try:
        DISPATCH[name]({"no": 1, "idx": 1, "citation": 1, "summary": "测试"})
        ck("工具 %s 可调用" % name, True)
    except Exception as e:
        ck("工具 %s 可调用" % name, False, repr(e)[:80])

os.environ["WM_OFFLINE"] = "1"
ag = ReviewAgent(max_steps=3, verbose=False)
res_ag = ag.run()
# Agent 的正常收尾状态：
#   finished          — 模型自己调了 generate_report 结束
#   finished_no_tool  — 模型不再调用工具，由程序补做必要核查后出报告
#   max_steps_reached — 步数用尽，自动收尾
#   fast_mode         — 测试快速模式（WM_AGENT_FAST=1，不做模型推理）
# 只有模型（云端与本地都）不可用时才是 deterministic_fallback。
# 断网环境里本地模型随包提供，所以通常**不该**落到降级——这条断言以前只认
# deterministic_fallback，等于把"本地模型能跑"误判成失败。
ck("Agent 正常结束（模型真跑、快速模式、或确实降级）",
   res_ag["status"] in ("finished", "finished_no_tool", "max_steps_reached",
                        "fast_mode", "deterministic_fallback"),
   res_ag["status"])
ck("降级仍产出报告", bool(res_ag.get("report")), list((res_ag.get("report") or {}).keys())[:5])
ck("降级记录工具轨迹", len(res_ag["trace"]) > 0, len(res_ag["trace"]))
os.environ.pop("WM_OFFLINE", None)

# ══════════ 7. docx_export（导出） ══════════
print()
print("=" * 70)
print("7. docx_export：Word / Excel 导出")
print("=" * 70)
from wenmai.docx_export import export, export_matrix_excel

d_now = load_draft()
chk_now = json.load(open(os.path.join(ENV.outputs_dir(), "selfcheck.json"),
                         encoding="utf-8"))
p_docx = export(d_now, chk_now, verbose=False)
ck("导出 Word 成功", os.path.isfile(p_docx), os.path.basename(p_docx))
try:
    from docx import Document
    doc = Document(p_docx)
    texts = "\n".join(p.text for p in doc.paragraphs)
    ck("Word 含研究现状", "研究现状" in texts)
    ck("Word 含参考文献", "参考文献" in texts)
    ck("Word 段落数合理（%d）" % len(doc.paragraphs), len(doc.paragraphs) > 5)
    ck("Word 无占位符残留", "TODO" not in texts and "{{" not in texts)
except Exception as e:
    ck("Word 可读", False, repr(e)[:70])

ck("导出 Excel 成功", os.path.isfile(export_matrix_excel(verbose=False)))

# ══════════ 汇总 ══════════
print()
print("=" * 70)
print("第二批汇总：通过 %d，失败 %d" % (len(R["pass"]), len(R["fail"])))
print("=" * 70)
if R["fail"]:
    print("失败项：")
    for x in R["fail"]:
        print("  ✗ %s　→ %s" % (x["name"], x["detail"]))
with open(os.path.join(ENV.tmp_dir(), "testB_result.json"), "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=2)
