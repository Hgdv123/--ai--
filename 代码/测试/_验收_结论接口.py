# -*- coding: utf-8 -*-
"""验收：走界面真正调用的 /api/agent，检查「审查结论」这一栏的内容。"""
import json
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

p = subprocess.Popen([sys.executable, "-X", "utf8", "_launch.py"], cwd=CODE, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(120):
        time.sleep(1)
        try:
            urllib.request.urlopen(B + "/api/state", timeout=3).read()
            break
        except Exception:
            pass
    else:
        raise SystemExit("服务未启动")

    t0 = time.time()
    req = urllib.request.Request(B + "/api/agent?max_steps=18", data=b"", method="POST")
    d = json.loads(urllib.request.urlopen(req, timeout=1800).read().decode("utf-8"))
    print("AI 审稿耗时 : %.1f 秒" % (time.time() - t0))
    print("status      :", d.get("status"))
    print("工具        :", d.get("tools_used"))
    print("=" * 70)
    print("界面上「AI 助手的审查结论」这一栏会显示：")
    print("-" * 70)
    print(d.get("agent_summary") or "（空）")
    print("-" * 70)
    rep = d.get("report") or {}
    print("report      : 问题 %s（高 %s / 中 %s / 低 %s）"
          % (rep.get("issues_total"), rep.get("high"), rep.get("medium"), rep.get("low")))
    ev = rep.get("evidence_check") or {}
    print("evidence    : 论断 %s，有支撑 %s，无支撑 %s，支持率 %s"
          % (ev.get("claims_total"), ev.get("supported"), ev.get("unsupported"),
             ev.get("support_rate")))
    # 判定：结论里不应出现"我准备做什么"的过程叙述，且应有实测数字
    s = d.get("agent_summary") or ""
    bad = ("我将首先" in s) or ("我打算" in s) or ("接下来我" in s)
    has_facts = ("本轮共发现" in s) or ("本轮未发现" in s)
    print("=" * 70)
    print("结论中无过程叙述 :", "OK" if not bad else "❌ 仍在显示计划")
    print("结论含实测数字   :", "OK" if has_facts else "❌ 缺少数字")
finally:
    p.terminate()
    try:
        p.wait(timeout=8)
    except Exception:
        p.kill()
