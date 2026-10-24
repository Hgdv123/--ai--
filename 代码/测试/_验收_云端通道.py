# -*- coding: utf-8 -*-
"""用 .env 里的真实 Key 实测云端通道能否跑通（含失败的如实报告）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from 源程序.config import settings, key_usable, key_problem  # noqa: E402

print("=" * 66)
print("配置")
print("  .env            :", settings.__class__.__module__)
print("  api_key 长度     :", len(settings.api_key))
print("  key 是否可用     :", key_usable(settings.api_key))
if key_problem(settings.api_key):
    print("  问题            :", key_problem(settings.api_key)[:120])
print("  base_url        :", settings.base_url)
print("  model_default   :", settings.model_default)
print("  llm_available   :", settings.llm_available)
print("=" * 66, flush=True)

if not settings.llm_available:
    print("云端不可用，后面的实测没有意义。")
    raise SystemExit(1)

from 源程序.llm import LLMClient  # noqa: E402

c = LLMClient(verbose=True)
print("client.cloud    :", c.cloud)
print("client.available:", c.available)
print("-" * 66)

t0 = time.time()
text, meta = c.chat(
    [{"role": "system", "content": "你是一个学术写作助手，回答尽量简短。"},
     {"role": "user", "content": "用一句话说明什么是小样本学习。"}],
    max_tokens=120, temperature=0.2, verbose=True)
dt = time.time() - t0
print("-" * 66)
print("耗时   : %.1f 秒" % dt)
if text:
    print("通道   :", "云端（百炼）" if not (meta or {}).get("local") else "本地")
    print("模型   :", (meta or {}).get("model"))
    print("返回   :", text.strip()[:160])
    print("结论   : 云端通道可用 ★")
else:
    print("失败原因:", meta)
    print("结论   : ❌ 云端调用失败（原因见上）")

# 成本台账
try:
    from 源程序.llm import today_cost
    print("-" * 66)
    print("今日花费:", today_cost())
except Exception as e:
    print("成本台账读取失败:", str(e)[:80])
