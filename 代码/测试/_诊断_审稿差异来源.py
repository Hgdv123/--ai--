# -*- coding: utf-8 -*-
"""量化"两次审稿结果不一样"的来源。

拆成两半分别测：
  A. 同一份稿子连测两次 → 差异来自"模型判断不稳定"
  B. 重新生成两次稿子   → 差异来自"稿子本身不一样"
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as E  # noqa: E402

E.activate()
os.environ["WM_OFFLINE"] = "1"
from 源程序 import selfcheck as S  # noqa: E402
from 源程序.config import OUT_DIR  # noqa: E402

MD = os.path.join(OUT_DIR, "draft.md")


def snapshot():
    md = open(MD, encoding="utf-8").read()
    r = S.run_evidence_check(use_llm=True, verbose=False)
    st = r.get("summary") or r
    ev = st.get("evidence_check") or st
    return {
        "md_hash": hash(md),
        "md_len": len(md),
        "claims": ev.get("claims_total"),
        "supported": ev.get("supported"),
        "unsupported": ev.get("unsupported"),
        "rate": ev.get("support_rate"),
    }


print("=" * 72)
print("A. 同一份稿子，连测两次（隔离模型判断稳定性）")
print("=" * 72)
md0 = open(MD, encoding="utf-8").read()
print("稿子长度 =", len(md0), "字符")
a1 = snapshot()
print("第 1 次：", a1)
a2 = snapshot()
print("第 2 次：", a2)
print("稿子是否被改动 :", "否（审稿只读）" if open(MD, encoding='utf-8').read() == md0 else "是")
same_judge = (a1["supported"] == a2["supported"]
              and a1["unsupported"] == a2["unsupported"])
print("判断是否一致   :", "一致" if same_judge else "**不一致 → 模型判断本身不稳定**")

print()
print("=" * 72)
print("B. 重新生成两次稿子（隔离稿子本身的变化）")
print("=" * 72)
from 源程序 import draft as D  # noqa: E402

texts = []
for i in (1, 2):
    d = D.generate(use_llm=True, verbose=False)
    md = open(MD, encoding="utf-8").read()
    texts.append(md)
    print("第 %d 次生成：%d 字符  前 60 字：%s" % (i, len(md), md.replace("\n", " ")[:60]))
print("两次稿子完全相同 :", "是" if texts[0] == texts[1] else "**否 → 稿子每次都不一样**")
if texts[0] != texts[1]:
    import difflib
    sm = difflib.SequenceMatcher(None, texts[0], texts[1])
    print("相似度           : %.1f%%" % (sm.ratio() * 100))

print()
print("=" * 72)
print("结论")
print("=" * 72)
print("A（同一稿子判断是否稳定）:", "稳定" if same_judge else "不稳定")
print("B（稿子本身是否固定）    :", "固定" if texts[0] == texts[1] else "每次都变")
