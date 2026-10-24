# -*- coding: utf-8 -*-
"""验收：用交付包原样启动，测用户报障的那个功能（离线 AI 审稿）。"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/代码
ROOT = os.path.join(os.path.dirname(CODE), "数据与产物")
B = "http://127.0.0.1:8848"

env = dict(os.environ)
env["WM_ROOT"] = ROOT
env["WM_ENV_FILE"] = os.path.join(CODE, ".env")
env["PYTHONIOENCODING"] = "utf-8"

t0 = time.time()
p = subprocess.Popen([sys.executable, "-X", "utf8", "_launch.py"], cwd=CODE, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(90):
        time.sleep(1)
        try:
            urllib.request.urlopen(B + "/api/state", timeout=3).read()
            break
        except Exception:
            pass
    else:
        raise SystemExit("服务未启动")
    print("启动耗时      : %.1f 秒" % (time.time() - t0))

    st = json.loads(urllib.request.urlopen(B + "/api/state", timeout=5).read().decode("utf-8"))
    print("模型服务      :", st["settings"]["模型服务"])
    print("文档/文献/卡片:", st["docs"]["count"], st.get("corpus", {}).get("count"),
          st.get("cards", {}).get("count"))
    print("needs_run     :", st["freshness"]["needs_run"])

    print("-" * 62)
    t1 = time.time()
    req = urllib.request.Request(B + "/api/agent", data=b"", method="POST")
    d = json.loads(urllib.request.urlopen(req, timeout=1200).read().decode("utf-8"))
    print("AI 审稿耗时   : %.1f 秒" % (time.time() - t1))
    print("status        :", d.get("status"))
    print("工具          :", d.get("tools_used"))
    print("结论          :", (d.get("agent_summary") or "")[:200])
    rep = d.get("report") or {}
    print("问题数        :", rep.get("issues_total"),
          "| 高", rep.get("high"), "中", rep.get("medium"), "低", rep.get("low"))
finally:
    p.terminate()
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()
    print("-" * 62)
    print("已关闭服务")
