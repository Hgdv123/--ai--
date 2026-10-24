# -*- coding: utf-8 -*-
"""空库启动专项：清空之后必须仍然能起来（原先会直接退出、网页不弹）。"""

# ── 路径与样本文件：一律由仓库自带的测试工具推导，勿写死 ──
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
import _testenv as E
import _fixtures
HERE = _HERE          # 供 os.path.join(HERE, "..", "源程序", ...) 使用
PROJ = E.activate()
import io, json, os, shutil, subprocess, sys, time, urllib.request

BK = os.path.join(E.tmp_dir(), "_emptybk")
PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n)
    print("  %s %s%s" % ("OK " if ok else "!! ", n, ("  -> " + str(d)[:110]) if d else ""))

# 备份真实数据
shutil.rmtree(BK, ignore_errors=True)
shutil.copytree(os.path.join(PROJ, "data"), os.path.join(BK, "data"))
shutil.copytree(os.path.join(PROJ, "outputs"), os.path.join(BK, "outputs"))
try:
    # 造出"用户点了清空之后"的状态
    with open(os.path.join(PROJ, "data", "corpus.json"), "w", encoding="utf-8") as f:
        json.dump({"query": "", "count": 0, "records": []}, f, ensure_ascii=False)
    for n in ("docs.json", "cards.json", "matrix.json"):
        p = os.path.join(PROJ, "data", n)
        if os.path.isfile(p): os.remove(p)
    for sub in ("pdf", "docx"):
        d = os.path.join(PROJ, "data", sub)
        if os.path.isdir(d):
            for fn in os.listdir(d): os.remove(os.path.join(d, fn))
    for n in ("draft.json", "draft.md", "selfcheck.json", "evidence_check.json",
              "文脉_综述初稿.docx", "文献对比矩阵.xlsx"):
        p = os.path.join(PROJ, "outputs", n)
        if os.path.isfile(p): os.remove(p)
    print("已造出空库状态（模拟点了清空之后）")
    print()

    print("跑 python -m wenmai demo（限时 90 秒，看它是否起服务）")
    env = dict(os.environ); env["PYTHONPATH"] = PROJ; env["PYTHONIOENCODING"] = "utf-8"
    env["WM_OFFLINE"] = "0"
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-u", "-m", E.PKG, "demo",
                             "--port", "8831", "--no-browser"],
                            cwd=PROJ, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    BASE = "http://127.0.0.1:8831"
    up = False
    for _ in range(45):
        time.sleep(1)
        try:
            urllib.request.urlopen(BASE + "/api/state", timeout=3).read()
            up = True; break
        except Exception:
            if proc.poll() is not None:
                break
    ck("空库时服务照常启动（页面能打开）", up, "进程存活=%s" % (proc.poll() is None))

    if up:
        st = json.loads(urllib.request.urlopen(BASE + "/api/state", timeout=10).read())
        ck("服务报文献库为 0", st["corpus"]["count"] == 0, st["corpus"]["count"])
        html = urllib.request.urlopen(BASE + "/", timeout=20).read().decode("utf-8", "ignore")
        ck("页面可访问", len(html) > 1000, "%d 字节" % len(html))
        for kw in ("用自带的 6 篇示例论文", "粘贴知网题录"):
            ck("页面提供空库引导：%s" % kw, kw in html)
        pj = json.loads(urllib.request.urlopen(BASE + "/api/papers", timeout=10).read())
        ck("论文清单为空但不报错", pj["count"] == 0, pj["count"])

    proc.terminate()
    try: proc.wait(timeout=15)
    except Exception: proc.kill()
    out = proc.stdout.read().decode("utf-8", "ignore") if proc.stdout else ""
    ck("终端提示了「直接打开页面」", "直接打开页面" in out,
       [l for l in out.split("\n") if "空" in l][:1])
finally:
    # 还原真实数据
    shutil.rmtree(os.path.join(PROJ, "data"), ignore_errors=True)
    shutil.rmtree(os.path.join(PROJ, "outputs"), ignore_errors=True)
    shutil.copytree(os.path.join(BK, "data"), os.path.join(PROJ, "data"))
    shutil.copytree(os.path.join(BK, "outputs"), os.path.join(PROJ, "outputs"))
    shutil.rmtree(BK, ignore_errors=True)
    print("\n已还原真实数据")

print()
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)