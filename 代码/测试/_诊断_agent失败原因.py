# -*- coding: utf-8 -*-
"""抓 Agent 离线跑时的完整日志，定位模型调用到底失败在哪一步、什么原因。"""
import os
import subprocess
import sys
import time
import urllib.request

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/代码
ROOT = os.path.join(os.path.dirname(CODE), "数据与产物")
B = "http://127.0.0.1:8848"

env = dict(os.environ)
env["WM_ROOT"] = ROOT
env["WM_ENV_FILE"] = os.path.join(CODE, ".env")
env["PYTHONIOENCODING"] = "utf-8"

proc = subprocess.Popen([sys.executable, "-X", "utf8", "_launch.py"], cwd=CODE, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(120):
        time.sleep(1)
        try:
            urllib.request.urlopen(B + "/api/state", timeout=3).read()
            break
        except Exception:
            pass
    print("服务就绪", flush=True)
    t0 = time.time()
    req = urllib.request.Request(B + "/api/agent?max_steps=18", data=b"", method="POST")
    d = urllib.request.urlopen(req, timeout=1800).read().decode("utf-8")
    print("AI 审稿 %.1f 秒" % (time.time() - t0))
    import json
    r = json.loads(d)
    print("status =", r.get("status"))
finally:
    proc.terminate()
    try:
        out = proc.communicate(timeout=10)[0].decode("utf-8", "ignore")
    except Exception:
        proc.kill()
        out = proc.communicate()[0].decode("utf-8", "ignore")

print("=" * 70)
print("服务端完整日志（只挑关键行）")
print("=" * 70)
keys = ("[Agent]", "[本地模型]", "[模型]", "失败", "错误", "Error", "Traceback", "503", "重试")
for ln in out.splitlines():
    if any(k in ln for k in keys):
        print(ln[:170])
