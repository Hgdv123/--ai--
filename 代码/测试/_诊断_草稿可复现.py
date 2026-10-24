# -*- coding: utf-8 -*-
"""隔离测试：同一份 topics/cards 输入，连调两次 _llm_draft，看输出是否一致。

用于判断"稿子每次都变"到底来自：
  (1) 生成环节本身不可复现，还是
  (2) 上游输入（卡片/主题）每次都不同
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as E  # noqa: E402

E.activate()
os.environ["WM_OFFLINE"] = "1"
os.environ.pop("WM_DRAFT_TEMPERATURE", None)

from 源程序 import draft as D  # noqa: E402
from 源程序.cards import load_cards  # noqa: E402
from 源程序.llm import LLMClient  # noqa: E402


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


# ── 取一份固定的输入 ──
cards_data = load_cards()
cards = cards_data.get("cards", []) if isinstance(cards_data, dict) else cards_data
print("卡片数 =", len(cards))

# 按 draft.generate 的方式准备 topics / num_of
import re  # noqa: E402
topics = D.cluster_topics(cards) if hasattr(D, "cluster_topics") else []
print("主题   =", topics)

num_of = {}
for i, c in enumerate(cards, 1):
    num_of[id(c)] = i

client = LLMClient(verbose=False)

print("=" * 72)
print("同一输入连调两次 _llm_draft")
print("=" * 72)
outs = []
for i in (1, 2):
    text, meta = D._llm_draft(topics, cards, num_of, client, verbose=False)
    text = text or ""
    outs.append(text)
    print("第 %d 次：%5d 字符  sha=%s" % (i, len(text), sha(text)))
    print("        前 70 字：%s" % text.replace("\n", " ")[:70])

same = outs[0] == outs[1] and outs[0] != ""
print("-" * 72)
print("两次是否逐字节相同 :", "是 ★ 生成可复现" if outs[0] == outs[1] else "**否**")
if outs[0] != outs[1]:
    import difflib
    print("相似度             : %.1f%%" % (
        difflib.SequenceMatcher(None, outs[0], outs[1]).ratio() * 100))

# 顺带打印实际生效的采样参数
print("-" * 72)
print("生效参数：温度=%s  seed=%s" % (
    os.environ.get("WM_DRAFT_TEMPERATURE", "0"),
    os.environ.get("WM_DRAFT_SEED", "20260918")))
