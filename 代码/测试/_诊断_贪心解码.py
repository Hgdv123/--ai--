# -*- coding: utf-8 -*-
"""干净的贪心解码对照实验：同一提示词连发两次，看输出是否逐字节相同。

背景：把草稿温度从 0.3 改成 0 后，两次生成仍然不同（1453 vs 1776 字符）。
需要先确认是"temperature=0 在本地服务上没做到确定性"，还是"提示词本身两次不同"。
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as E  # noqa: E402

E.activate()
from 源程序 import local_llm as L  # noqa: E402

port = L._start("gen")
print("端口 =", port)
if not port:
    raise SystemExit("服务未启动")

PROMPT = "请用一句话概括元学习与小样本分类的关系。"


def call(temperature, n_predict=80, seed=None, retries=24):
    body = {"prompt": PROMPT, "n_predict": n_predict, "temperature": temperature,
            "stream": False, "repeat_penalty": 1.12}
    if seed is not None:
        body["seed"] = seed
    last = None
    for i in range(retries):
        req = urllib.request.Request("http://127.0.0.1:%d/completion" % port,
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r).get("content") or ""
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


print("=" * 72)
for temp in (0, 0.3):
    outs = []
    for i in (1, 2):
        t0 = time.time()
        o = call(temp)
        outs.append(o)
        print("温度 %-4s 第 %d 次（%.1f 秒）：%r" % (temp, i, time.time() - t0, o[:90]))
    same = outs[0] == outs[1]
    print("  → 两次是否逐字节相同：", "是 ★ 确定性" if same else "**否 → 该温度下非确定性采样**")
    print()

print("=" * 72)
print("再测：温度 0 + 固定 seed，连发三次")
outs = [call(0, seed=1234) for _ in range(3)]
for i, o in enumerate(outs, 1):
    print("  第 %d 次：%r" % (i, o[:80]))
print("  → 是否全部相同：", "是" if len(set(outs)) == 1 else "**否**")

print()
print("=" * 72)
print("服务启动参数里的采样相关项")
print("  生成槽位:", L.GEN_SLOTS, "| 上下文:", L.GEN_CTX, "| 每槽:", L.GEN_CTX // max(L.GEN_SLOTS, 1))
print("  注意：多槽位（-np 4）批处理时，同一批次内不同槽位的计算可能互相影响，")
print("        即使贪心解码也可能因批内张量布局不同而出现差异。")
