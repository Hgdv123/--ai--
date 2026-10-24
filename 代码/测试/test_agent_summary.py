# -*- coding: utf-8 -*-
"""Agent 结论净化专项：模型把工具 JSON 当结论吐出来时必须堵住。

为什么单独测这件事：这类泄漏**看起来像正常输出**——界面上就是一行灰色小字，
不报错、不崩，只有懂 JSON 的人才知道那不是给人看的。
实测出现过的形式（旧判据全部漏过）：
  · `{"no":2}`            —— 工具入参（句子编号）
  · `{"citation":2,"status":"supported",…}` —— 工具返回值
  · `check_claim_support({"no":2})` —— 伪调用写法
它们都没有 `"function"`/`"parameters"`，所以只认工具 schema 的旧判据抓不到。

同时必须**不误伤**：用户稿子里的 JSON、正文里的代码示例都要原样保留。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as ENV  # noqa: E402

ENV.activate()
from wenmai.agent import sanitize_agent_summary, _compose_conclusion  # noqa: E402

PASS, FAIL = [], []
FALLBACK = "AI 助手已完成本轮核查"


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name,
                         ("　→ " + str(detail)[:100]) if detail else ""))


print("=" * 70)
print("Agent 结论净化专项")
print("=" * 70)

print("\n1. 工具 JSON 不得出现在结论里")
BLOCK = [
    ('{"no":2}', "工具入参（短）"),
    ('{"no": 2}', "工具入参（带空格）"),
    ('{"no":2,"sentence":""}', "工具入参（多字段）"),
    ('【{"no":2}】', "被中文括号包住"),
    ('[{"no":2}]', "包在数组里"),
    ('check_claim_support({"no":2})', "伪调用写法"),
    ('{"index":3}', "按编号取条目"),
    ('{"no":11}。', "带句号"),
    ('{"citation":2,"status":"supported","reason":"片段有支撑"}', "工具返回值"),
    ('{"status":"supported","reason":"原文提到 Meta-SGD 的学习率优化"}', "工具返回值（中文理由）"),
    ('我将调用 check_claim_support 工具，参数是 {"no":2}', "过程叙述+参数"),
]
for text, why in BLOCK:
    out = sanitize_agent_summary(text)
    ck("堵住：%s" % why, out.startswith(FALLBACK), "%r → %r" % (text[:34], out[:44]))

print("\n2. 混在正常句子里的伪调用：JSON 部分要被清掉")
SCRUB = ['它调用了 get_sentence({"no":2}) 取句子。']
for text in SCRUB:
    out = sanitize_agent_summary(text)
    ck("清掉 JSON 片段，保留可读中文",
       ('"no"' not in out) and ('{' not in out) and ('}' not in out), out[:60])

print("\n3. 不得误伤：这些必须原样保留")
KEEP = [
    "见函数 f({a}) 与 g({b}) 的用法。",
    "参考文献 [1] 指出该结论成立，支持率为 0.73。",
    "句子 3 的引用与原文不符，原文讨论的是训练速度。",
    '用户稿子里有 JSON 配置 {"timeout": 30}，这不影响引用检查。',
    "三处引用的原文片段均已核对，其中一处为否定式误报。",
]
for text in KEEP:
    out = sanitize_agent_summary(text)
    ck("保留：%s" % text[:22], out == text, out[:60])

print("\n4. 端到端：合成后的界面结论不含 JSON")
REP = {"total": 0, "high": 0, "medium": 0, "low": 0, "by_kind": {}, "evidence_check": {}}
for text in ('{"no":2}', '{"citation":2,"status":"supported"}'):
    out = _compose_conclusion(text, REP)
    ck("合成结论无 JSON：%s" % text[:26],
       ('"no"' not in out) and ('"status"' not in out), out[:70])

print("\n" + "=" * 70)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ❌ %s" % f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
