# -*- coding: utf-8 -*-
"""四态判定专项：有依据 / 找不到依据 / 原文明确矛盾 / 无法核对。

为什么单独测这件事：把「没找到依据」和「原文说的正好相反」混成一个标签，
用户就分不清"你去核对一下"和"你写错了"。
前者可能只是论文换了措辞（检索没命中），后者是硬结论。
分错的代价是**误导用户去改本来正确的引用**，而这类错误在报告里看不出来——
两种情况都只是"一行红字"。所以必须钉住：

  1. 确定性矛盾检测：同量纲数值不一致、论断数值在原文中不存在、方向相反 → 必须报；
  2. 它同时必须**不误报**：数值一致、没给数值、方向一致 → 不许报；
  3. 原文不在库里 → 只能判「无法核对」，绝不许升级成"引用错误"；
  4. 矛盾判定在检索命中后**先于模型调用**完成，不依赖模型是否可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as ENV  # noqa: E402

ENV.activate()
from wenmai.selfcheck import (  # noqa: E402
    find_contradiction, check_claim_support, build_docs_index,
    ST_SUPPORTED, ST_NOT_EVIDENCE, ST_CONTRADICTED, ST_UNVERIFIABLE, _norm_status)
from wenmai.literature import load_docs  # noqa: E402

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name,
                         ("　→ " + str(detail)[:110]) if detail else ""))


print("=" * 70)
print("四态判定专项")
print("=" * 70)

# ── 1. 确定性矛盾：必须报 ────────────────────────────────
print("\n1. 确定性矛盾检测：这些必须判「原文明确矛盾」")
MUST_HIT = [
    ("该方法把准确率提升了 10 倍", "Our method improves the accuracy by 2 times.",
     "同量纲（倍）数值不一致（跨语言也能比，量纲归一后数值直接比）"),
    ("准确率提升了 15%", "The accuracy of our method decreases by 15%.", "方向相反"),
    ("训练速度提高了 3 倍", "training speed is reduced by 3x", "方向相反（中英混合）"),
    ("精度优于基线 20%", "the method is 20% worse than the baseline", "方向相反（worse）"),
    ("该方法把准确率提升了 37%", "本文方法的准确率提升了 12%。", "同语言：数值在原文中不存在"),
]
for claim, ev, why in MUST_HIT:
    r = find_contradiction(claim, ev)
    ck("检出：%s" % why, bool(r), r or "未检出")

# ── 2. 确定性矛盾：不许误报 ──────────────────────────────
print("\n2. 这些必须**不报**（误报会让人去改正确的引用）")
MUST_MISS = [
    ("该方法把准确率提升了 10 倍", "improves the accuracy by 10 times", "数值一致"),
    ("准确率提升了 15%", "accuracy improves by 15%", "数值与方向都一致"),
    ("在 Omniglot 上验证了有效性", "We evaluate on Omniglot in 2017.", "无具体数值"),
    ("模型参数量更少", "using fewer parameters", "没有可比数值"),
    ("覆盖了 3 个数据集", "we use 3 datasets", "数值一致（裸数字不应参与比对）"),
    ("", "some evidence text", "空论断"),
    ("论断文本", "", "空证据"),
    # 跨语言时数字形态天然对不上（"2 倍" vs "two times"），
    # 拿"数值不存在"去判矛盾会大量冤枉用户，必须放过。
    ("准确率提升了 2 倍", "our accuracy doubles on the benchmark", "跨语言：数字形态不同"),
    ("准确率提升了 2 倍", "accuracy improves by two times", "跨语言：数字写作英文单词"),
]
for claim, ev, why in MUST_MISS:
    r = find_contradiction(claim, ev)
    ck("不报：%s" % why, not r, r or "（无冲突）")

# ── 3. 无法核对：不得升级成引用错误 ──────────────────────
print("\n3. 原文不在库里 → 只能判「无法核对」")
SEP = "\n\n"
# 句子必须 >= 25 字：decompose_claims 的 min_len 是 25，短句根本不会被当成论断。
draft = {
    "markdown": "# 研究现状" + SEP +
                "MAML 通过优化模型的初始参数，使模型在新任务上仅需少量梯度更新即可快速适应[1]。" + SEP +
                "这个研究方向目前已经被彻底解决，不存在任何遗留问题与争议[99]。" + SEP +
                "## 参考文献" + SEP +
                "[1] Finn C. Model-Agnostic Meta-Learning for Fast Adaptation[J]. 2017." + SEP +
                "[99] 某不存在的文献[J]. 2099.",
    # 引用条目必须带 title：条目→库内论文的定位就是靠 title 匹配的，
    # 只有 index 的 refs 会让所有引用都退化成"无法核对"（这正是本测试要防的反面）。
    "refs": [{"index": 1, "title": "Model-Agnostic Meta-Learning for Fast Adaptation",
              "text": "Finn C. Model-Agnostic Meta-Learning for Fast Adaptation[J]. 2017."},
             {"index": 99, "title": "某不存在的文献",
              "text": "某不存在的文献[J]. 2099."}],
}
docs = [d for d in load_docs().get("docs", []) if (d.get("full_text") or "").strip()]
retr, by_key = build_docs_index(docs)
ck("测试前置：文献库可用", retr is not None and len(by_key) > 0, "%d 篇" % len(by_key))

res = check_claim_support(draft["markdown"], draft, retr, by_key,
                          use_llm=False, verbose=False)
by_cite = {}
for r in res["results"]:
    by_cite.setdefault(str(r.get("citation")), []).append(r)
# 引用 [99] 在库里不存在 → 必须是 unverifiable
st99 = {r["status"] for r in by_cite.get("99", [])}
ck("库里没有的引用 [99] 判「无法核对」", st99 == {ST_UNVERIFIABLE}, st99 or "（未产生结果）")
ck("「无法核对」的理由里不含「写错/错误」这类定性",
   all("错误" not in (r.get("reason") or "") and "写错" not in (r.get("reason") or "")
       for r in by_cite.get("99", [])),
   [r.get("reason") for r in by_cite.get("99", [])][:1])

# ── 4. 四态取值合法 + 汇总字段自洽 ──────────────────────
print("\n4. 四态取值与汇总字段自洽")
valid = {ST_SUPPORTED, ST_NOT_EVIDENCE, ST_CONTRADICTED, ST_UNVERIFIABLE}
s = res["summary"]
ck("所有条目的 status 都是四态之一",
   all(r["status"] in valid for r in res["results"]),
   sorted({r["status"] for r in res["results"]}))
ck("四态计数之和 = 判定条数",
   s["supported"] + s["contradicted"] + s["not_evidence"] + s["unverifiable"]
   == s["claims_judged"],
   "%d vs %d" % (s["supported"] + s["contradicted"] + s["not_evidence"] + s["unverifiable"],
                 s["claims_judged"]))
ck("unsupported 兼容字段 = 矛盾 + 找不到依据",
   s["unsupported"] == s["contradicted"] + s["not_evidence"],
   s["unsupported"])
ck("unresolved 兼容字段 = 无法核对",
   s["unresolved"] == s["unverifiable"], s["unresolved"])
verifiable = s["supported"] + s["contradicted"] + s["not_evidence"]
expect_rate = round(s["supported"] / verifiable, 4) if verifiable else 0.0
ck("支持率只在「可核对」范围内计算（无法核对不充数）",
   abs(s["support_rate"] - expect_rate) < 1e-9, s["support_rate"])

# ── 5. 旧状态名归一 ─────────────────────────────────────
print("\n5. 旧报告里的状态名要归一到四态")
ck("unsupported → 找不到依据", _norm_status("unsupported") == ST_NOT_EVIDENCE)
ck("unresolved → 无法核对", _norm_status("unresolved") == ST_UNVERIFIABLE)
ck("weak → 找不到依据", _norm_status("weak") == ST_NOT_EVIDENCE)
ck("未知值 → 无法核对（不猜成「有依据」）", _norm_status("whatever") == ST_UNVERIFIABLE)

print("\n" + "=" * 70)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ❌ %s" % f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
