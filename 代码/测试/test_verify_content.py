# -*- coding: utf-8 -*-
"""内容校验专项：_pdf_matches_title / _refs_region / _count_refs。

为什么单独测这三件事：它们防的都是**看不见的错配**——
PDF 与元数据不是同一篇论文、文献表被正文里的同名小节顶替、
作者-年份制文献表数不出条目。这类问题不会让程序报错，
只会让报告看起来"正常"却全是错的，所以必须有回归测试钉住。
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as ENV  # noqa: E402

PROJ = ENV.activate()
from wenmai.literature import (_pdf_matches_title, _refs_region, _count_refs,
                               _title_norm, _title_sim, _MAX_PARSE_PAGES)
from wenmai.textutil import clean_text

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:100]) if detail else ""))


def _looks_like_tool_json(s):
    """判断文本是否残留"工具定义"特征——这是不该出现在用户界面的东西。"""
    t = (s or "")
    if "<tools>" in t or "</tools>" in t or "tool_call" in t:
        return True
    return ('"function"' in t and ('"parameters"' in t or '"description"' in t))


print("=" * 70)
print("内容校验专项")
print("=" * 70)

# ── 1. 归一化与相似度 ────────────────────────────────────
print("\n1. 标题归一化与相似度")
ck("归一化去大小写/连字符/空格",
   _title_norm("Model-Agnostic Meta-Learning!") == "modelagnosticmetalearning",
   _title_norm("Model-Agnostic Meta-Learning!"))
ck("完全相同的标题相似度 = 1", _title_sim(_title_norm("A B C D E"), _title_norm("A B C D E")) == 1.0)
ck("不相干的标题相似度低",
   _title_sim(_title_norm("Deep Residual Learning for Image Recognition"),
              _title_norm("A survey on Image Data Augmentation")) < 0.6)
ck("一字之差仍高相似",
   _title_sim(_title_norm("Few-Shot Learning via Meta Learning"),
              _title_norm("Few Shot Learning via Meta-Learning")) > 0.85)

# ── 2. 真文件校验 ────────────────────────────────────────
print("\n2. _pdf_matches_title 在真实文件上的判定")
corpus_p = os.path.join(PROJ, "data", "corpus.json")
if os.path.isfile(corpus_p):
    recs = json.load(io.open(corpus_p, encoding="utf-8"))["records"]
    pdfs = [r for r in recs if (r.get("doc_type") or "pdf") == "pdf"
            and os.path.isfile(os.path.join(PROJ, "data", (r.get("local_pdf") or "").replace("/", os.sep)))]
    ck("语料库里有可校验的 PDF", len(pdfs) >= 1, len(pdfs))
    right = [r for r in pdfs
             if _pdf_matches_title(os.path.join(PROJ, "data", r["local_pdf"].replace("/", os.sep)),
                                   r.get("title") or "")]
    ck("每篇的 PDF 内容都与标题一致（无错配残留）", len(right) == len(pdfs),
       "%d/%d 一致" % (len(right), len(pdfs)))
    # 交叉验证：拿 A 的标题去校验 B 的文件，必须判否
    crossed = 0
    for i, r in enumerate(pdfs):
        other = pdfs[(i + 1) % len(pdfs)]
        if other["local_pdf"] == r["local_pdf"]:
            continue
        if _pdf_matches_title(os.path.join(PROJ, "data", other["local_pdf"].replace("/", os.sep)),
                              r.get("title") or ""):
            crossed += 1
            print("       误判：用 %s(%s) 的标题校验 %s(%s)"
                  % (r["local_pdf"], (r.get("title") or "")[:28],
                     other["local_pdf"], (other.get("title") or "")[:28]))
    ck("用别人的标题校验本文件 → 一律判否", crossed == 0, "误判 %d 例" % crossed)
else:
    ck("语料库存在（跳过真实文件校验）", False, corpus_p)

# ── 3. 不存在的文件 / 空标题 ─────────────────────────────
print("\n3. 边界")
ck("不存在的文件 → False", _pdf_matches_title(os.path.join(PROJ, "data", "nope.pdf"), "任意标题") is False)
ck("标题太短不比对 → True（不否决）",
   _pdf_matches_title(os.path.join(PROJ, "data", "pdf", "001.pdf"), "AB") is True)

# ── 4. 文献表定位 ────────────────────────────────────────
print("\n4. _refs_region：取文末文献表而非正文中的同名小节")
body = ("Introduction\n" * 200 +
        "References\n[1] A. Author. Title one. 2019.\n[2] B. Author. Title two. 2020.\n" +
        "Related Work\n" * 200)
seg = _refs_region(body)
ck("正文中先出现的 References 不被误当文献表", "[1] A. Author" in seg, seg[:40])
ck("不误取更早的小节内容", "Related Work" not in seg or seg.index("Related Work") > 0)
early = "References\n[1] early one. 2019.\n" + "body\n" * 400
ck("标题落在前半段 → 判为正文小节，返回空",
   _refs_region(early) == "", _refs_region(early)[:40])
ck("没有文献表标题 → 返回空", _refs_region("正文" * 500) == "")
ck("空文本不炸", _refs_region("") == "")

# ── 5. 条目计数 ─────────────────────────────────────────
print("\n5. _count_refs：编号制与作者-年份制都要数得出来")
num_refs = "".join("[%d] Author %d. Some title. Journal, 20%02d.\n" % (i, i, i % 20)
                   for i in range(1, 31))
ck("编号制 [1] → 30 条", _count_refs(num_refs) == 30, _count_refs(num_refs))
dot_refs = "".join("%d. Author, A. Some title. 20%02d.\n" % (i, i % 20) for i in range(1, 41))
ck("编号制 1. → 40 条", _count_refs(dot_refs) == 40, _count_refs(dot_refs))
authoryear = "".join(
    "Author%02d, A., Other, B., and Third, C. Some paper title here. In CVPR.\n" % i
    for i in range(1, 26))
ck("作者-年份制 → 25 条", _count_refs(authoryear) == 25, _count_refs(authoryear))
ck("没有条目 → 0", _count_refs("just some prose without entries\n") == 0)
ck("空文本 → 0", _count_refs("") == 0)
# 少量编号（<8）不足以确信，应转用作者-年份启发式而不是只报 3
mixed = "[1] A, B. Title.\n[2] C, D. Title.\n[3] E, F. Title.\n" + authoryear
ck("编号太少时改用作者-年份启发式", _count_refs(mixed) >= 25, _count_refs(mixed))

# ── 6. 页数上限不会被调回小值 ────────────────────────────
print("\n6. 解析页数上限")
ck("页数上限 ≥ 100（保证不切掉文末文献表）", _MAX_PARSE_PAGES >= 100, _MAX_PARSE_PAGES)
import inspect
from wenmai import literature as _lit
sig = inspect.signature(_lit.parse_one)
ck("parse_one 默认页数 = 上限", sig.parameters["max_pages"].default == _MAX_PARSE_PAGES,
   sig.parameters["max_pages"].default)
sig2 = inspect.signature(_lit.parse_all)
ck("parse_all 默认页数 = 上限", sig2.parameters["max_pages"].default == _MAX_PARSE_PAGES,
   sig2.parameters["max_pages"].default)

# ── 7. 真实语料库的文献表都能数出条目 ────────────────────
print("\n7. 真实论文的文献表")
docs_p = os.path.join(PROJ, "data", "docs.json")
if os.path.isfile(docs_p):
    docs = json.load(io.open(docs_p, encoding="utf-8"))["docs"]
    zero = [d.get("title", "")[:40] for d in docs
            if (d.get("chars_total") or 0) > 30000 and not (d.get("ref_count") or 0)]
    # 语料库刚重建、还没重新解析时这条会失败（docs.json 仍是上一批 PDF 的结果），
    # 跑一次 `wenmai parse` 即可恢复。
    ck("有正文的论文都能数出文献表条目", not zero, zero)
    ck("文献表条目数在合理范围",
       all(0 <= (d.get("ref_count") or 0) <= 1000 for d in docs),
       [d.get("ref_count") for d in docs])
else:
    ck("docs.json 存在（跳过）", False, docs_p)

# ── 8. 编号重排的不变量：正文编号必须都在参考文献表里 ──────
print("\n8. renumber_by_citation_order：正文编号 ⊆ 参考文献表")
from wenmai.draft import renumber_by_citation_order

_refs = [{"index": i, "cite": "[%d]" % i, "text": "[%d] A. T%d." % (i, i),
          "title": "T%d" % i} for i in (1, 2, 4)]
for body, name in [
    ("正文引用 [1] 和 [2]，还有不存在的 [4][5]。", "含越界号"),
    ("全是越界号 [7][8][9]。", "全越界"),
    ("相邻连排 [1][2][3]。", "相邻编号"),
    ("[1] 行首引用。", "行首"),
    ("没有引用的句子。", "无引用"),
]:
    nb, nr, _ = renumber_by_citation_order(body, _refs)
    _cited = {int(x) for x in re.findall(r"\[(\d+)\]", nb)}
    _tab = {r["index"] for r in nr}
    ck("编号重排「%s」后正文 ⊆ 表" % name, _cited <= _tab,
       "正文 %s / 表 %s" % (sorted(_cited), sorted(_tab)))
    ck("编号重排「%s」后表内编号连续" % name,
       sorted(_tab) == list(range(1, len(_tab) + 1)), sorted(_tab))

# ── 9. Agent 结论不得泄漏内部工具定义 ──────────────────────
# 实测出现过：模型把 tools schema 当正文吐出来，界面直接显示
# `<tools>{"type":"function",…}</tools>`，用户完全看不懂。
print("\n9. sanitize_agent_summary：结论里不能出现工具定义")
from wenmai.agent import sanitize_agent_summary

_LEAK = ('<tools>\n{"type": "function", "function": {"name": "reference_by_index", '
         '"description": "按编号取参考文献条目原文。", "parameters": {"properties": '
         '{"idx": {"description": "参考文献编号", "type": "integer"}}, "required": '
         '["idx"], "type": "object"}}\n</tools>')
_NORMAL = "本次核查共发现 4 处引用问题，建议逐条核对原文。"

ck("截图中的原始泄漏被清理干净",
   not _looks_like_tool_json(sanitize_agent_summary(_LEAK)),
   sanitize_agent_summary(_LEAK)[:60])
ck("泄漏后面跟着的正常结论被保留",
   "4 处引用问题" in sanitize_agent_summary(_LEAK + "\n\n" + _NORMAL),
   sanitize_agent_summary(_LEAK + "\n\n" + _NORMAL)[:60])
ck("裸 JSON（无 tools 标签）也被清理",
   not _looks_like_tool_json(sanitize_agent_summary(
       '{"type": "function", "function": {"name": "find_evidence", '
       '"parameters": {"type": "object"}}}')))
ck("正常结论原样保留", sanitize_agent_summary(_NORMAL) == _NORMAL)
ck("空输入给出可照做的兜底文案",
   len(sanitize_agent_summary("")) > 10 and "检查过程" in sanitize_agent_summary(""))
ck("纯标签输入也给兜底文案", len(sanitize_agent_summary("<tools></tools>")) > 10)

# ── 10. 「非论文」判定：传错文件要看得出来 ─────────────────
# 实测案例：用户把"宣传视频策划案"传进文献库，界面只显示"就绪"，
# 看不出任何问题，还会被当成综述素材。判据：既无章节又无参考文献。
print("\n10. looks_like_non_paper：非论文文档要被识别出来")
from wenmai.literature import looks_like_non_paper

ck("非论文文档被识别（无章节、无参考文献）",
   looks_like_non_paper({"full_text": "视频策划案正文…" * 40,
                         "sections_found": [], "ref_count": 0}) is True)
ck("有章节的真论文不误判",
   looks_like_non_paper({"full_text": "x" * 500,
                         "sections_found": ["abstract", "method"], "ref_count": 0}) is False)
ck("有参考文献表的不误判",
   looks_like_non_paper({"full_text": "x" * 500,
                         "sections_found": [], "ref_count": 22}) is False)
ck("空正文交给「无正文」状态处理，不重复报",
   looks_like_non_paper({"full_text": "", "sections_found": [], "ref_count": 0}) is False)
ck("空 dict 不崩", looks_like_non_paper({}) is False)

# ── 11. 引用纠正不得改坏正确的编号 ──────────────────────────
# 实测事故：离线模式下 correct_citations 把**模型原本正确**的编号改错，
# 5 句里改错 4 句，用户看到的就是"研究现状与原文不符"（支持率仅 0.14）。
# 两条误判来源已堵住：① 段首角标（"[1] 提出了…"是合法的中文综述写法）；
# ② 一句话里有多篇论文的线索时，"命中最多"不等于"角标所指"。
print("\n11. correct_citations：不能把正确的引用编号改坏")
from wenmai.draft import correct_citations

_cards = [
    {"title": "Model-Agnostic Meta-Learning for Fast Adaptation",
     "fields": {"method": "提出模型无关的元学习算法，显式优化模型参数以支持高效微调",
                "problem": "深度网络仅用少量样本快速适应",
                "datasets": ["Omniglot", "mini-ImageNet"], "metrics": [], "limitation": ""}},
    {"title": "Unsupervised Meta-Learning For Few-Shot Image Classification",
     "fields": {"method": "提出UMTRA算法，在未标注数据上构建合成任务进行元学习",
                "problem": "无监督条件下少样本图像分类", "datasets": ["Omniglot"],
                "metrics": [], "limitation": "性能低于监督方法"}},
    {"title": "Meta-SGD Learning to Learn Quickly",
     "fields": {"method": "提出Meta-SGD元学习器，端到端优化初始化、更新方向与学习率",
                "problem": "少样本快速学习", "datasets": [], "metrics": [], "limitation": ""}},
]
_num = {id(c): i for i, c in enumerate(_cards, 1)}

_body = ("[1] 提出了模型无关的元学习算法，通过显式优化模型参数来支持高效微调。\n"
         "[2] 则提出了UMTRA算法，在未标注数据上构建合成任务进行元学习。\n"
         "[3] 提出了Meta-SGD元学习器，端到端优化初始化、更新方向与学习率。")
_out, _n = correct_citations(_body, _cards, _num)
ck("段首角标 + 内容正确时不做任何改动", _n == 0, "改了 %d 处" % _n)
ck("段首角标被原样保留（内容一字未动）", _out == _body, _out[:80])

_multi = ("UMTRA 在未标注数据上构建合成任务，而其性能低于监督方法，"
          "这一点与 Meta-SGD 的端到端优化形成对比[2]。")
_out2, _n2 = correct_citations(_multi, _cards, _num)
ck("一句里有多篇论文线索时不猜（保留原编号）", _n2 == 0, "改了 %d 处" % _n2)

_wrong = "这句话讲的是UMTRA算法在未标注数据上构建合成任务进行元学习[3]。"
_out3, _n3 = correct_citations(_wrong, _cards, _num)
ck("线索唯一且与角标冲突时才纠正", _n3 >= 1, "改了 %d 处" % _n3)

print()
print("=" * 70)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 70)
for n in FAIL:
    print("  ✗ %s" % n)
sys.exit(1 if FAIL else 0)
