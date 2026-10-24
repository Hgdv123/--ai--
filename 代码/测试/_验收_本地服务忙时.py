# -*- coding: utf-8 -*-
"""验收：本地模型服务"忙"时不应被误判为"没起来"。

复现原缺陷：模型正在推理时 /health 可能回 503 或超时，旧实现只认 200，
于是 _start() 判定服务没起来，又去另起一个抢同一端口的实例 → 报
"llama-server 未就绪或返回异常"，Agent 整轮退回确定性计划。

本脚本在**真实推理进行中**并发探测，验证：
  1. _healthy() 在服务忙时仍判定为活着
  2. _start() 不会重复拉起实例（端口上始终只有一个服务）
  3. chat_tools() 能拿到工具调用
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _testenv as E  # noqa: E402

E.activate()
from 源程序 import agent as A  # noqa: E402
from 源程序 import local_llm as L  # noqa: E402

print("GEN_PORT =", L.GEN_PORT, "| 生成本地可用 =", L.available("gen"))

port = L._start("gen")
print("_start 返回端口 =", port)
if not port:
    raise SystemExit("本地生成服务未能启动")

alive_during = []
stop = threading.Event()


def probe():
    """推理期间持续探测，记录判定结果。"""
    while not stop.is_set():
        alive_during.append((L._healthy(port), L._serving(port)))
        time.sleep(0.4)


t = threading.Thread(target=probe, daemon=True)
t.start()

t0 = time.time()
d = L.chat_tools(
    [{"role": "system", "content": A.SYSTEM_PROMPT},
     {"role": "user", "content": "请先看看这份综述有多少句话，再决定下一步。"}],
    A.TOOL_SCHEMAS, max_tokens=300, temperature=0.2, timeout=300)
dt = time.time() - t0
stop.set()
t.join(timeout=5)

print("-" * 66)
print("推理耗时        : %.1f 秒" % dt)
print("推理期间探测次数 : %d" % len(alive_during))
if alive_during:
    h_ok = sum(1 for h, _ in alive_during if h)
    s_ok = sum(1 for _, s in alive_during if s)
    print("  判定为活着    : %d/%d" % (h_ok, len(alive_during)))
    print("  端口在监听    : %d/%d" % (s_ok, len(alive_during)))
print("返回 content    : %r" % (d.get("content") or "")[:70])
print("返回 tool_calls : %d 个" % len(d.get("tool_calls") or []))
for c in (d.get("tool_calls") or []):
    print("   -", c.get("function", {}).get("name"),
          str(c.get("function", {}).get("arguments"))[:90])

# 端口上只应有一个实例：用 netstat 数监听该端口的行数
import subprocess
out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
listeners = set()
for line in out.splitlines():
    p = line.split()
    if len(p) >= 5 and p[0].upper() == "TCP" and p[3].upper() == "LISTENING" \
            and p[1].endswith(":%d" % port):
        listeners.add(p[4])
print("-" * 66)
print("监听 %d 的进程数 : %d  %s" % (port, len(listeners), sorted(listeners)))

ok = (alive_during and all(h for h, _ in alive_during) and len(listeners) == 1
      and d.get("tool_calls"))
print("结论：", "忙时不再被误判、单实例、工具调用正常 ★" if ok else "仍有问题")
sys.exit(0 if ok else 1)
