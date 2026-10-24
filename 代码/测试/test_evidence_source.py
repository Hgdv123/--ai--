# -*- coding: utf-8 -*-
"""证据溯源专项：页码锚点、页眉/文献区不进证据索引。

为什么单独测这三件事：证据核对要能说"依据在第 3 页"，
用户才会照着页码翻过去自己看。这一步错得**看不出来**——
页码指错页时报告依然"正常"，只是把人引到错的地方；
页眉混进证据时支持率虚高，判定全被污染。所以必须有回归测试钉住。

钉住的不变量：
  1. chunk_spans 的块序列与切分本体 _chunk_text 逐块一致（一块不丢、一块不多）；
  2. 定位不到的块照样返回，只是 start/end 为 None（缺页码可以，丢证据不行）;
  3. page_spans 单调不回跳，且落在正文范围内；
  4. 页眉/页脚/水印不进证据块，论文标题不成为"证据"；
  5. 证据检索返回的命中带页码，且参考文献区不被当作证据。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as ENV  # noqa: E402

ENV.activate()
from wenmai.literature import (load_docs, _filter_boundary,  # noqa: E402
                               _map_pos)
from wenmai.retrieval import (_chunk_text, chunk_spans, PageMap,  # noqa: E402
                              EvidenceRetriever)
from wenmai.selfcheck import build_docs_index  # noqa: E402

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name,
                         ("　→ " + str(detail)[:110]) if detail else ""))


def sq(s):
    return re.sub(r"\s+", " ", s or "").strip()


DOCS = [d for d in load_docs().get("docs", []) if (d.get("full_text") or "").strip()]
print("=" * 70)
print("证据溯源专项（%d 篇论文）" % len(DOCS))
print("=" * 70)

# ── 1. 块序列不变 ────────────────────────────────────────
print("\n1. chunk_spans 的块序列必须与切分本体一致")
seq_ok, span_kind_ok, no_page, total = True, True, 0, 0
first_bad = ""
for d in DOCS:
    full = d.get("full_text") or ""
    for mc, mn in ((1200, 60), (800, 40)):
        ref = _chunk_text(full, max_chars=mc, min_chars=mn)
        got = chunk_spans(full, max_chars=mc, min_chars=mn)
        if [c["text"] for c in got] != ref:
            seq_ok = False
            first_bad = first_bad or "%s(%d,%d)" % ((d.get("title") or "")[:20], mc, mn)
        for c in got:
            total += 1
            if c["start"] is None:
                no_page += 1
                if c["end"] is not None:
                    span_kind_ok = False
            elif not isinstance(c["start"], int) or c["end"] <= c["start"]:
                span_kind_ok = False
ck("块序列与 _chunk_text 逐块一致（一块不丢、一块不多）", seq_ok, first_bad or "两组参数 × %d 篇" % len(DOCS))
ck("有页码的块 start/end 都是有效区间；无页码的块 start/end 同为 None", span_kind_ok)
ck("拿不到页码的块占比 < 5%（缺页码可以，丢证据不行）",
   no_page <= total * 0.05, "%d/%d" % (no_page, total))

# ── 2. page_spans 单调且落在正文范围内 ────────────────────
print("\n2. page_spans 必须单调递增、落在正文长度内、页数与 pages 对得上")
mono_ok, range_ok, count_ok = True, True, True
detail = ""
for d in DOCS:
    spans = d.get("page_spans") or []
    n = len(d.get("full_text") or "")
    if len(spans) != (d.get("pages") or 0):
        count_ok = False
        detail = detail or "%s: 区间 %d / pages %s" % ((d.get("title") or "")[:20], len(spans), d.get("pages"))
    prev = -1
    for s in spans:
        if s["start"] < prev:
            mono_ok = False
        prev = s["start"]
        if not (0 <= s["start"] <= n and s["start"] < s["end"] <= n + 1):
            range_ok = False
ck("页码区间单调不回跳（回跳会让后面所有证据页码全错）", mono_ok, detail)
ck("页码区间落在正文范围内", range_ok)
ck("页码区间数量 = 论文页数", count_ok, detail)

# ── 3. 页码换算正确 ─────────────────────────────────────
print("\n3. 页码换算：页内位置必须落到该页区间")
conv_ok = True
for d in DOCS:
    spans = d.get("page_spans") or []
    if len(spans) < 2:
        continue
    pm = PageMap(spans, 0)
    for s in spans:
        mid = (s["start"] + s["end"]) // 2
        got = pm.page_of(mid)
        expect = str(s["page"])
        # 允许落在相邻页的重叠区上（段落跨页时的正常现象）
        if got not in (expect, str(max(1, s["page"] - 1)), str(s["page"] + 1)):
            conv_ok = False
ck("页内位置换算出的页码是所在页或相邻页（重叠区允许）", conv_ok)

# ── 4. 页眉/标题不进证据 ────────────────────────────────
print("\n4. 页眉、水印、论文标题不得成为证据块")
retr, by_key = build_docs_index(DOCS)
head_ok, title_ok, ref_ok = True, True, True
for key, doc in by_key.items():
    chunks = retr.by_doc.get(key, {}).get("chunks") or []
    title_flat = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (doc.get("title") or "").lower())
    for c in chunks:
        if re.search(r"(?mi)^\s*arXiv:\d{4}\.\d{4,5}", c):
            head_ok = False
        flat = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", c.lower())
        if title_flat and flat == title_flat:
            title_ok = False
    kinds = {i.get("kind") for i in (doc.get("ignored_spans") or [])}
    if "参考文献区" not in kinds:
        ref_ok = False
ck("页眉/arXiv 水印不在证据块里（否则任何句子都能被它'支撑'）", head_ok)
ck("论文标题本身不作为证据块（标题与任何谈论它的句子都高度相似）", title_ok)
ck("参考文献区已从证据索引排除（文献表讲的是别人的论文）", ref_ok)

# ── 5. 坐标平移（页码在过滤前后必须对齐）───────────────
print("\n5. 坐标平移：删掉字符后，后面的位置要整体前移，不能冻住")
# 这一条防的是一类"看不出来"的错：映射方向搞反或差一格时，
# 整篇页码会系统性偏移、末页会塌成空区间，而报告看起来完全正常。
# 本文件里这几个断言是实打实抓到过 bug 的（off-by-one 与查表取错端点）。
_cases = [
    ("删中段 [3,6)", [{"start": 3, "end": 6}], 10, 7,
     [0, 2, 3, 6, 9, 10], [0, 2, 3, 3, 6, 7]),
    ("删两端", [{"start": 0, "end": 2}, {"start": 8, "end": 10}], 10, 6,
     [0, 2, 5, 8, 10], [0, 0, 3, 6, 6]),
    ("不删", [], 10, 10, [0, 5, 10], [0, 5, 10]),
]
for label, removed, total, after, probes, expect in _cases:
    _keys = _filter_boundary(removed, total, after)
    got = [_map_pos(p, _keys, after) for p in probes]
    ck("坐标平移正确：%s" % label, got == expect, "实测 %s / 期望 %s" % (got, expect))
# 真实数据上：每页区间必须非空、单调、不越界，且末页覆盖到正文末尾
_bad = []
for d in DOCS:
    sp = d.get("page_spans") or []
    n = len(d.get("full_text") or "")
    prev = -1
    for s in sp:
        if not (0 <= s["start"] < s["end"] <= n) or s["start"] < prev:
            _bad.append("%s 第%s页" % ((d.get("title") or "")[:16], s["page"]))
            break
        prev = s["start"]
    if sp and sp[-1]["end"] != n:
        _bad.append("%s 末页未覆盖到正文末尾" % (d.get("title") or "")[:16])
ck("每页区间非空、单调、不越界，且末页覆盖到正文末尾", not _bad, _bad[:3])

# ── 6. 检索命中带页码 ──────────────────────────────────
print("\n6. 证据检索：命中要带页码，且不能来自参考文献区")
CLAIM = "MAML通过优化初始参数，使模型仅需少量梯度更新即可在新任务上快速泛化[2]。"
hits_n, with_page = 0, 0
for key in by_key:
    for h in retr.find(key, CLAIM, top_k=2):
        hits_n += 1
        if h.get("page"):
            with_page += 1
ck("证据命中带页码的比例 >= 90%",
   hits_n == 0 or with_page >= hits_n * 0.9, "%d/%d" % (with_page, hits_n))
ck("无页码的命中 page 为空串（不得当成第 1 页）",
   all(h.get("page") != "0" for key in by_key for h in retr.find(key, CLAIM, top_k=2)))

print("\n" + "=" * 70)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ❌ %s" % f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
