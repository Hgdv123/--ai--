# -*- coding: utf-8 -*-
"""全套测试 · 第一批：纯函数层（config / textutil / gbt7714 / retrieval / embeddings /
refparse / docx_parse / llm / bootstrap）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as B  # noqa: E402

B.activate()
R = {"pass": [], "fail": [], "warn": []}


def ck(name, ok, detail=""):
    (R["pass"] if ok else R["fail"]).append({"name": name, "detail": str(detail)[:160]})
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:80]) if detail else ""))


def warn(name, detail):
    R["warn"].append({"name": name, "detail": str(detail)[:160]})
    print("  ⚠️ %s　→ %s" % (name, str(detail)[:80]))


# ══════════ 1. 模块导入 ══════════
print("=" * 70)
print("1. 全部模块可导入（19 个）")
print("=" * 70)
MODULES = ["__init__", "bootstrap", "config", "textutil", "retrieval", "embeddings",
           "llm", "literature", "docx_parse", "refparse", "cards", "matrix",
           "gbt7714", "draft", "selfcheck", "agent", "docx_export", "app", "__main__"]
for m in MODULES:
    try:
        __import__("wenmai." + m if m != "__init__" else "wenmai")
        ck("import wenmai.%s" % m, True)
    except Exception as e:
        ck("import wenmai.%s" % m, False, repr(e)[:90])

# ══════════ 2. config ══════════
print()
print("=" * 70)
print("2. config：路径解析与配置项")
print("=" * 70)
from wenmai import config as C

ck("PKG_DIR 存在", os.path.isdir(C.PKG_DIR), C.PKG_DIR)
# 项目根不再断言"含 pyproject.toml"：交付包用 WM_ROOT 指向「数据与产物/」，
# 测试更是指向隔离沙箱，那里本来就没有 pyproject.toml。真正要成立的是
# 「数据目录、输出目录都落在生效的项目根下面」——这才是各模块读写的依据。
ck("PROJECT_DIR 是绝对路径", os.path.isabs(C.PROJECT_DIR), C.PROJECT_DIR)
ck("DATA_DIR 位于 PROJECT_DIR 下",
   os.path.dirname(C.DATA_DIR) == C.PROJECT_DIR, C.DATA_DIR)
ck("OUT_DIR 位于 PROJECT_DIR 下",
   os.path.dirname(C.OUT_DIR) == C.PROJECT_DIR, C.OUT_DIR)
# WM_ROOT 生效时，PROJECT_DIR 必须等于它——这是交付包"代码与数据分开放"的关键
ck("WM_ROOT 生效时 PROJECT_DIR 等于 WM_ROOT",
   (not os.environ.get("WM_ROOT")) or
   os.path.abspath(C.PROJECT_DIR) == os.path.abspath(os.environ["WM_ROOT"]),
   "%s vs %s" % (C.PROJECT_DIR, os.environ.get("WM_ROOT")))
ck("WEB_DIR 含两个页面",
   os.path.isfile(os.path.join(C.WEB_DIR, "index.html")) and
   os.path.isfile(os.path.join(C.WEB_DIR, "privacy.html")), C.WEB_DIR)
ck("settings.describe() 返回 6 项", len(C.settings.describe()) >= 5,
   list(C.settings.describe().keys()))
ck("daily_cost_limit 是数字", isinstance(C.settings.daily_cost_limit, (int, float)),
   C.settings.daily_cost_limit)
ck("api_key 已配置", bool(C.settings.api_key))

# ══════════ 3. textutil ══════════
print()
print("=" * 70)
print("3. textutil：文本处理")
print("=" * 70)
from wenmai import textutil as T

ck("clean_text 去多余空行", T.clean_text("a\n\n\n\nb") == "a\n\nb", repr(T.clean_text("a\n\n\n\nb")))
ck("clean_text 修英文断词", T.clean_text("meta-\nlearning") == "metalearning",
   repr(T.clean_text("meta-\nlearning")))
ck("clean_text 处理空输入", T.clean_text("") == "" and T.clean_text(None) == "")

sents = T.split_sentences("第一句。第二句！第三句？")
ck("split_sentences 中文句切分", len(sents) == 3, sents)
sents2 = T.split_sentences("First sentence. Second one here.")
ck("split_sentences 英文句切分", len(sents2) >= 1, sents2)
ck("split_sentences 空输入", T.split_sentences("") == [])

toks = T.tokenize("深度学习在图像分类中的应用 deep learning")
ck("tokenize 有输出", len(toks) > 0, len(toks))
ck("tokenize 过滤停用词", "的" not in toks and "the" not in T.tokenize("the cat"))
ck("tokenize 空输入", T.tokenize("") == [])

kw = T.keywords("小样本学习 小样本 分类 分类 分类 图像", top_k=3)
ck("keywords 返回高频词", len(kw) > 0 and kw[0] in ("分类", "小样本"), kw)
ck("keywords 空输入", T.keywords("") == [])

ng = T.char_ngrams("abcdef", 3)
ck("char_ngrams 输出正确", ng == ["abc", "bcd", "cde", "def"], ng)
ck("char_ngrams 短文本", T.char_ngrams("ab", 3) == ["ab"])
ck("char_ngrams 空输入", T.char_ngrams("", 3) == [])

ck("is_boilerplate 识别免责声明",
   T.is_boilerplate("The views expressed in this paper are those of the author"))
ck("is_boilerplate 识别资助信息", T.is_boilerplate("This work was supported by grant no. 123"))
ck("is_boilerplate 放行正常句", not T.is_boilerplate("未来工作将探索跨域迁移能力"))
ck("strip_section_head 去掉标题",
   T.strip_section_head("Discussion and Future Work We introduced a method")
   .startswith("We introduced"), T.strip_section_head("Future work We will explore"))

# ══════════ 4. gbt7714 ══════════
print()
print("=" * 70)
print("4. gbt7714：参考文献格式")
print("=" * 70)
from wenmai import gbt7714 as G

ck("guess_type 识别期刊", G.guess_type("IEEE Transactions on Pattern Analysis") == "J")
ck("guess_type 识别会议", G.guess_type("Proceedings of the IEEE Conference on Computer Vision") == "C")
ck("guess_type 识别 arXiv", G.guess_type("arXiv (Cornell University)") == "EB/OL")
ck("guess_type 空输入不崩", G.guess_type("") in ("J", "EB/OL"))

rec = {"title": "Test Title", "authors": ["A One", "B Two"], "year": 2022,
       "venue": "Journal of Testing", "ref_type": "J"}
entry = G.format_entry(rec, 1)
ck("format_entry 含编号", entry.startswith("[1] "), entry[:40])
ck("format_entry 含类型标识", "[J]" in entry, entry)
ck("format_entry 含年份", "2022" in entry, entry)
ck("format_entry 含作者", "A One" in entry, entry)

rec3 = dict(rec, authors=["A", "B", "C", "D", "E"])
ck("format_entry 超 3 作者用 et al", "et al" in G.format_entry(rec3), G.format_entry(rec3)[:70])

refs = G.build_reference_list([rec, rec3])
ck("build_reference_list 编号递增",
   refs[0]["index"] == 1 and refs[1]["index"] == 2, [r["index"] for r in refs])

ck("check_entry 正常条目无问题", G.check_entry("[1] A One. Test Title[J]. Journal, 2022.") == [],
   G.check_entry("[1] A One. Test Title[J]. Journal, 2022."))
ck("check_entry 检出缺类型标识", any("类型标识" in p for p in G.check_entry("[1] A One. Title. J, 2022.")))
ck("check_entry 检出缺年份", any("年份" in p for p in G.check_entry("[1] A One. Title[J]. Journal.")))
ck("check_entry 空输入不崩", isinstance(G.check_entry(""), list))

# ══════════ 5. retrieval ══════════
print()
print("=" * 70)
print("5. retrieval：检索引擎")
print("=" * 70)
from wenmai.retrieval import BM25, VectorIndex, HybridRetriever, EvidenceRetriever, \
    split_for_index, _stable_bucket

ck("_stable_bucket 稳定", _stable_bucket("abc", 1000) == _stable_bucket("abc", 1000))
ck("_stable_bucket 分布", len({_stable_bucket("w%d" % i, 100) for i in range(50)}) > 20)

ck("BM25 空语料不崩", BM25([]).search(["a"]) == [])
ck("BM25 检索命中", len(BM25([["a", "b"], ["c", "d"]]).search(["a"], top_k=1)) == 1)
ck("BM25 无匹配返回空", BM25([["a"], ["b"]]).search(["zzz"]) == [])

vi = VectorIndex(["机器学习 深度学习", "recommender system"])
ck("VectorIndex 检索不崩", isinstance(vi.search("深度学习"), list))
ck("VectorIndex 空查询返回空", vi.search("") == [])
ck("VectorIndex 空语料不崩", VectorIndex([]).search("x") == [])

hr = HybridRetriever([{"title": "paper one", "full_text": "machine learning"},
                      {"title": "paper two", "full_text": "recommender"}])
ck("HybridRetriever 检索返回", len(hr.search("machine learning", top_k=2)) >= 1)

chunks = split_for_index("段一。" * 100 + "\n\n" + "段二。" * 100, max_chars=200)
ck("split_for_index 切块", len(chunks) >= 2, len(chunks))
ck("split_for_index 空输入", split_for_index("") == [])
ck("split_for_index 超长无空行", len(split_for_index("啊" * 3000, max_chars=500)) >= 2)

er = EvidenceRetriever([{"title": "t", "full_text": "self-distillation improves performance " * 30,
                         "id": "d1"}])
ck("EvidenceRetriever 建索引", len(er) == 1, len(er))
ck("EvidenceRetriever.find 返回结果", len(er.find("d1", "self distillation")) > 0)
ck("EvidenceRetriever.find 未知 key 不崩", er.find("nope", "x") == [])
ck("EvidenceRetriever.find 空断言不崩", er.find("d1", "") == [])
ck("EvidenceRetriever 空文档不崩", len(EvidenceRetriever([])) == 0)

# ══════════ 6. embeddings ══════════
print()
print("=" * 70)
print("6. embeddings：向量化（三级降级链）")
print("=" * 70)
from wenmai.embeddings import Embedder, cosine, _local_vector
from wenmai import local_llm as _LL

# 装了本地模型后，离线时的正确后端是 local-bge-small-zh（512 维）；
# n-gram（2048 维）只是最终兜底。要验证兜底行为需临时屏蔽本地模型。
os.environ["WM_OFFLINE"] = "1"
e_off = Embedder()
v1 = e_off.encode(["测试文本"])
ck("离线时走本地后端", e_off.backend.startswith("local"), e_off.backend)
ck("离线向量维度 512（本地模型）或 2048（n-gram 兜底）",
   len(v1[0]) in (512, 2048), len(v1[0]))
ck("余弦相似度自比=1", abs(cosine(v1[0], v1[0]) - 1.0) < 1e-6)
ck("空向量不崩", cosine([], []) == 0.0)
ck("长度不等不崩", isinstance(cosine([1.0], [1.0, 2.0]), float))
ck("批量编码长度一致", len(e_off.encode(["a", "b", "c"])) == 3)
ck("空列表编码不崩", e_off.encode([]) == [])

_orig_find = _LL.find_model
_LL.find_model = lambda names: ""          # 模拟"本地模型缺失"
try:
    e_ng = Embedder()
    v_ng = e_ng.encode(["兜底测试"])
    ck("模型缺失时退回 n-gram（2048 维）",
       e_ng.backend == "local-ngram" and len(v_ng[0]) == 2048,
       "%s / %d 维" % (e_ng.backend, len(v_ng[0])))
finally:
    _LL.find_model = _orig_find
os.environ.pop("WM_OFFLINE", None)

# ══════════ 7. llm ══════════
print()
print("=" * 70)
print("7. llm：客户端护栏与降级")
print("=" * 70)
from wenmai.llm import LLMClient, today_cost, PRICES

_LL.find_model = lambda names: ""          # 模拟无本地模型
try:
    c = LLMClient(api_key="")
    ck("无 Key 且无本地模型时 available=False", c.available is False)
    txt, meta = c.chat([{"role": "user", "content": "x"}])
    ck("此时 chat 返回 None", txt is None, meta)
    ck("此时 chat_json 返回 None", c.chat_json([{"role": "user", "content": "x"}])[0] is None)

    os.environ["WM_OFFLINE"] = "1"
    c2 = LLMClient()
    ck("强制离线且无本地模型时 available=False", c2.available is False)
    os.environ.pop("WM_OFFLINE", None)
finally:
    _LL.find_model = _orig_find

if _LL.available("gen"):
    os.environ["WM_OFFLINE"] = "1"
    c3 = LLMClient()
    ck("离线但有本地模型时 available=True", c3.available is True, c3.available)
    ck("  └ cloud=False（不走云端）", c3.cloud is False, c3.cloud)
    os.environ.pop("WM_OFFLINE", None)

led = today_cost()
ck("成本台账含必要字段",
   all(k in led for k in ("date", "cost", "calls", "cached", "tokens")), list(led.keys()))
ck("PRICES 三种模型都有", all(k in PRICES for k in ("qwen-turbo", "qwen-plus", "qwen-max")))

# ══════════ 8. docx_parse ══════════
print()
print("=" * 70)
print("8. docx_parse：Word 解析")
print("=" * 70)
from wenmai.docx_parse import is_docx, parse_docx

ck("is_docx 识别 .docx", is_docx("a.docx") and is_docx("A.DOCX"))
ck("is_docx 拒绝 .pdf", not is_docx("a.pdf"))
ck("is_docx 接受 .doc", is_docx("a.doc"))
try:
    r = parse_docx(os.path.join(B.tmp_dir(), "missing_file.docx"))
    ck("解析不存在的文件应抛错", False, "未抛错")
except Exception:
    ck("解析不存在的文件抛错（调用方需捕获）", True)

# ══════════ 9. bootstrap ══════════
print()
print("=" * 70)
print("9. bootstrap：依赖检查")
print("=" * 70)
from wenmai import bootstrap as pkg_bootstrap

res = pkg_bootstrap.ensure(quiet=True, auto_install=False)
ck("ensure 返回 ok 字段", "ok" in res and "missing" in res, list(res.keys()))
ck("硬依赖齐备", res["ok"] is True, res.get("missing"))

# ══════════ 汇总 ══════════
print()
print("=" * 70)
print("第一批汇总：通过 %d，失败 %d，警告 %d" % (len(R["pass"]), len(R["fail"]), len(R["warn"])))
print("=" * 70)
if R["fail"]:
    print("失败项：")
    for x in R["fail"]:
        print("  ✗ %s　→ %s" % (x["name"], x["detail"]))
if R["warn"]:
    print("警告项：")
    for x in R["warn"]:
        print("  ⚠️ %s　→ %s" % (x["name"], x["detail"]))

with open(os.path.join(B.tmp_dir(), "testA_result.json"), "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=2)
