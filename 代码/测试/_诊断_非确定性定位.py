# -*- coding: utf-8 -*-
"""定位非确定性：对比"短提示词/短生成"与"长提示词/长生成"是否可复现。

已知：短提示词+固定 seed+n_predict=80 → 三次逐字节一致。
待测：草稿真实提示词 + n_predict=2400 → 是否仍一致。
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as E  # noqa: E402

E.activate()
os.environ["WM_OFFLINE"] = "1"
from 源程序 import local_llm as L  # noqa: E402
from 源程序 import draft as D  # noqa: E402
from 源程序.cards import load_cards  # noqa: E402
from 源程序.llm import LLMClient  # noqa: E402

port = L._start("gen")
print("端口 =", port, "| GEN_CTX =", L.GEN_CTX, "| 槽数 =", L.GEN_SLOTS,
      "| 每槽 =", L.GEN_CTX // max(L.GEN_SLOTS, 1))


def raw(prompt, n_predict, temperature=0.0, seed=20260918, retries=30):
    wrapped = ("<|im_start|>system\n你是一个学术写作助手，只输出要求的内容，"
               "不要重复题目，不要自问自答。<|im_end|>\n"
               "<|im_start|>user\n%s<|im_end|>\n<|im_start|>assistant\n" % prompt)
    body = {"prompt": wrapped, "n_predict": n_predict, "temperature": temperature,
            "stream": False, "stop": list(L.STOP_MARKERS), "repeat_penalty": 1.12,
            "seed": seed}
    raw_body = json.dumps(body).encode()
    last = None
    for _ in range(retries):
        req = urllib.request.Request("http://127.0.0.1:%d/completion" % port,
                                     data=raw_body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                return json.load(r).get("content") or ""
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


# ── 造出与生产一致的草稿提示词 ──
cards_data = load_cards()
cards = cards_data.get("cards", []) if isinstance(cards_data, dict) else cards_data
topics = D.cluster_topics(cards)
num_of = {id(c): i for i, c in enumerate(cards, 1)}
blocks = []
for c in cards:
    f = c.get("fields") or {}
    blocks.append("[%d] %s（%s 年）\n  问题：%s\n  方法：%s\n  数据集：%s\n  指标：%s\n  局限：%s"
                  % (num_of[id(c)], (c.get("title") or "")[:100], c.get("year") or "",
                     f.get("problem") or "—", f.get("method") or "—",
                     "、".join(f.get("datasets") or []) or "—",
                     "；".join(f.get("metrics") or []) or "—", f.get("limitation") or "—"))
prompt = D.LLM_USER.format(n_topics=max(len(topics), 2), cards_block="\n\n".join(blocks))
print("草稿提示词长度 =", len(prompt), "字符")
print("=" * 72)

print("A. 短提示词 + 短生成（n_predict=80）")
for i in (1, 2):
    o = raw("请用一句话概括元学习与小样本分类的关系。", 80)
    print("   第 %d 次 sha=%s len=%d" % (i, sha(o), len(o)))
print()

print("B. 长提示词（草稿真实提示词）+ 长生成（n_predict=600）")
outs = []
for i in (1, 2):
    t0 = time.time()
    o = raw(prompt, 600)
    outs.append(o)
    print("   第 %d 次 sha=%s len=%d  %.1f 秒" % (i, sha(o), len(o), time.time() - t0))
print("   → ", "一致 ★" if outs[0] == outs[1] else "**不一致**")
print()

print("C. 同 B 但槽数改为 1 的效果（仅提示：需重启服务才生效）")
print("   当前 -np =", L.GEN_SLOTS, "（多槽批处理时同一批次内张量布局不同，")
print("   llama.cpp 的批处理路径可能引入与槽位相关的数值差异）")
