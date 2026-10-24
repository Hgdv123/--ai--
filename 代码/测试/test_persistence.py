# -*- coding: utf-8 -*-
"""持久化测试：数据会不会因为关掉程序而消失。

做法：起服务 → 做真实改动（上传一篇 PDF、跑卡片、切数据模式）
→ **完全关闭服务进程** → 重新起服务 → 逐项核对改动是否还在。
两组独立重启，排除"进程没真死"造成的假通过。

注意点（都是踩过的坑）：
  · 起服务前先确保端口空闲——残留进程会让新进程起不来，
    于是测试连到的是**旧进程**，看不出任何问题，结论完全失真。
  · 基线必须是已知干净的 6 篇；上一轮崩溃留下的探针会污染快照，
    导致"初始就 7 篇"这种假失败。这里先断言基线。
"""

# ── 路径与样本文件：一律由仓库自带的测试工具推导，勿写死 ──
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
import _testenv as E
import _fixtures
HERE = _HERE          # 供 os.path.join(HERE, "..", "源程序", ...) 使用
PROJ = E.activate()
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PORT = 8753
BASE = "http://127.0.0.1:%d" % PORT
EXTRA = os.path.join(E.tmp_dir(), "_persist_probe.pdf")
PASS, FAIL = [], []
BASELINE = 6

def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:110]) if detail else ""))

def reachable(timeout=2):
    try:
        urllib.request.urlopen(BASE + "/api/state", timeout=timeout).read()
        return True
    except Exception:
        return False

def kill_stale():
    """杀掉占用测试端口的进程，并等到端口空闲。

    这里原先自带一份用 `wmic` 的实现——而 wmic 已被 Windows 移除（实测
    CommandNotFound），于是它静默杀掉 0 个进程，"服务已完全停止"这条断言
    就永远失败。改用 tests/server.py 里已经修好的实现（基于 netstat）。
    """
    try:
        # server.py 的端口是模块级常量，导入前设好环境变量才能让它认对端口
        os.environ["WM_TEST_PORT"] = str(PORT)
        import server as _srv
        return _srv.kill_stale(verbose=False)
    except Exception:
        return 0

def start():
    kill_stale()
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJ
    env["PYTHONIOENCODING"] = "utf-8"
    env["WM_OFFLINE"] = "0"
    p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", E.PKG, "serve",
                          "--port", str(PORT)],
                         cwd=PROJ, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(90):
        time.sleep(1)
        if reachable(3):
            return p
    raise RuntimeError("服务未能启动")

def stop(p):
    """彻底关闭，并确认端口真的不通。"""
    p.terminate()
    try:
        p.wait(timeout=20)
    except Exception:
        p.kill()
        p.wait(timeout=10)
    kill_stale()
    return not reachable(2)

def jget(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read().decode("utf-8"))

def jpost(path, data=None, timeout=1800):
    body = urllib.parse.urlencode(data or {}).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

def upload_pdf(filename, content):
    b = "----wm%d" % int(time.time() * 1000)
    body = (("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
             "Content-Type: application/pdf\r\n\r\n" % (b, filename)).encode()
            + content + ("\r\n--%s--\r\n" % b).encode())
    req = urllib.request.Request(BASE + "/api/upload", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % b)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}

def disk_mode():
    p = os.path.join(PROJ, "data", "privacy.json")
    if not os.path.isfile(p):
        return None
    return json.load(io.open(p, encoding="utf-8")).get("mode")

SRC_PDF = os.path.join(PROJ, "data", "pdf", "001.pdf")
print("=" * 72)
print("持久化测试（两组独立重启）")
print("=" * 72)

print("\n步骤 0：清理残留进程与探针")
print("  清理进程 %d 个" % kill_stale())
for junk in (EXTRA, os.path.join(PROJ, "data", "pdf", "007.pdf")):
    if os.path.isfile(junk):
        os.remove(junk)
print("  磁盘模式 = %s" % disk_mode())

print("\n步骤 1：启动服务，确认基线干净")
p = start()
st0 = jget("/api/state")
n0 = st0["corpus"]["count"]
print("  初始：语料库 %d 篇 / docs %d / 卡片 %s" % (n0, st0["docs"]["count"], st0["cards"]["count"]))
ck("基线为 %d 篇（快照干净）" % BASELINE, n0 == BASELINE, n0)

print("\n步骤 2：做真实改动（上传 1 篇 + 跑卡片 + 切仅本地）")
shutil.copy2(SRC_PDF, EXTRA)
st, r = upload_pdf("持久化探针.pdf", open(EXTRA, "rb").read())
ck("上传返回 200", st == 200, r)
papers = jget("/api/papers")
ck("语料库变成 %d 篇" % (BASELINE + 1), papers["count"] == BASELINE + 1, papers["count"])
ck("新上传的论文在清单里",
   any("持久化探针" in (x.get("title") or "") for x in papers["papers"]))
st, r = jpost("/api/run", {"stage": "cards"})
ck("跑卡片阶段成功", st == 200, str(r)[:60])
s1 = jget("/api/state")
cards1 = s1["cards"]["count"]
ck("卡片数随之更新", cards1 >= BASELINE, cards1)
jpost("/api/privacy?mode=local")
ck("数据模式已写入磁盘为 local", disk_mode() == "local", disk_mode())
# 界面读的是 settings.describe() 里的「模型服务」——不是「数据模式」这个键
# （曾经按错键名断言，得到 None 而误判为产品缺陷）。
ck("界面读到的模型服务已切换到本地",
   "本地" in (jget("/api/state")["settings"].get("模型服务") or ""),
   jget("/api/state")["settings"].get("模型服务"))

print("\n步骤 3：关闭程序，确认端口真的不通")
ck("服务已完全停止", stop(p))

print("\n步骤 4：重新启动，核对改动是否还在")
p = start()
st2 = jget("/api/state")
papers2 = jget("/api/papers")
ck("语料库仍是 %d 篇（未回退）" % (BASELINE + 1), st2["corpus"]["count"] == BASELINE + 1,
   st2["corpus"]["count"])
ck("新上传的论文仍在清单",
   any("持久化探针" in (x.get("title") or "") for x in papers2["papers"]))
ck("卡片数未丢失", st2["cards"]["count"] == cards1,
   "%s vs %s" % (st2["cards"]["count"], cards1))
ck("数据模式仍是 local", disk_mode() == "local", disk_mode())
ck("docs 未丢失", st2["docs"]["count"] >= BASELINE, st2["docs"]["count"])

print("\n步骤 5：删除探针，确认删除也能持久")
tgt = [x for x in papers2["papers"] if "持久化探针" in (x.get("title") or "")]
ck("找到探针条目", bool(tgt))
if tgt:
    st, r = jpost("/api/delete", {"local_pdf": tgt[0]["local_pdf"]})
    ck("删除探针成功", r.get("ok") is True, r)
jpost("/api/privacy?mode=cloud")
ck("模式已改回 cloud", disk_mode() == "cloud", disk_mode())
ck("服务已完全停止", stop(p))
p = start()
st3 = jget("/api/state")
ck("重启后仍是 %d 篇（删除已持久）" % BASELINE, st3["corpus"]["count"] == BASELINE,
   st3["corpus"]["count"])
ck("重启后模式仍是 cloud", disk_mode() == "cloud", disk_mode())
stop(p)

print("\n步骤 6：直接查磁盘文件（不经过服务）")
for rel in ("data/corpus.json", "data/docs.json", "data/cards.json",
            "data/matrix.json", "data/privacy.json", "outputs/draft.json"):
    pth = os.path.join(PROJ, rel)
    ok = os.path.isfile(pth) and os.path.getsize(pth) > 10
    ck("%s 在磁盘上且非空" % rel, ok,
       ("%d 字节" % os.path.getsize(pth)) if os.path.isfile(pth) else "缺失")

if os.path.isfile(EXTRA):
    os.remove(EXTRA)

print()
print("=" * 72)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 72)
for n in FAIL:
    print("  ✗ %s" % n)
sys.exit(1 if FAIL else 0)
