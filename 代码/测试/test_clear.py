# -*- coding: utf-8 -*-
"""清空专项：/api/clear 必须让"清空后回到干净状态"这句话成立。

关键坑（实测踩到）：只删 corpus/docs/cards/matrix 时，
  · outputs/ 的综述稿还在 → 界面刷新后"研究现状"照旧显示
  · data/pdf 的原文还在 → 下次上传同名文件被判为"已存在"而跳过，内容其实是旧的
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
import json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

PORT = 8811
BASE = "http://127.0.0.1:%d" % PORT
PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n)
    print("  %s %s%s" % ("OK " if ok else "!! ", n, ("  -> " + str(d)[:110]) if d else ""))

env = dict(os.environ); env["PYTHONPATH"] = PROJ; env["PYTHONIOENCODING"] = "utf-8"
env["WM_OFFLINE"] = "0"
p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", E.PKG, "serve", "--port", str(PORT)],
                     cwd=PROJ, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))
def post(path, fields=None, timeout=900):
    body = urllib.parse.urlencode(fields or {}).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
def upload(path, fp):
    data = open(fp, "rb").read()
    b = "----wmclear"
    body = (("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
             "Content-Type: application/pdf\r\n\r\n" % (b, os.path.basename(fp))).encode()
            + data + ("\r\n--%s--\r\n" % b).encode())
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % b)
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))

D = os.path.join(PROJ, "data")
O = os.path.join(PROJ, "outputs")
try:
    for _ in range(60):
        time.sleep(1)
        try:
            get("/api/state"); break
        except Exception: pass

    print("1) 先造出「有结果」的状态")
    st, r = post("/api/run", {"stage": "all"}, timeout=1800)
    ck("全流程跑通", st == 200, st)
    stt = get("/api/state")
    ck("已有综述稿", bool(stt["draft"]["markdown"]), len(stt["draft"].get("refs") or []))
    ck("本体文件存在（draft.json）", os.path.isfile(os.path.join(O, "draft.json")))
    ck("Word 产物存在", os.path.isfile(os.path.join(O, "文脉_综述初稿.docx")))
    pdfs_before = [f for f in os.listdir(os.path.join(D, "pdf"))]
    ck("有论文原文文件", len(pdfs_before) > 0, pdfs_before)

    print()
    print("2) 调用清空")
    st, r = post("/api/clear")
    ck("清空返回 200", st == 200, st)
    ck("响应报告了删除的文件数", (r.get("removed_files") or 0) > 0, r.get("removed_files"))

    print()
    print("3) 清空后必须回到干净状态")
    stt = get("/api/state")
    ck("文献库归零", stt["corpus"]["count"] == 0, stt["corpus"]["count"])
    ck("综述稿已清空（这是用户最直接的预期）",
       not (stt["draft"].get("markdown")), "仍有 %d 字" % len(stt["draft"].get("markdown") or ""))
    ck("参考文献归零", not (stt["draft"].get("refs") or []))
    for name in ("draft.json", "draft.md", "selfcheck.json", "evidence_check.json",
                 "文脉_综述初稿.docx", "文献对比矩阵.xlsx"):
        ck("已删除 " + name, not os.path.isfile(os.path.join(O, name)))
    ck("data/pdf 已清空", os.listdir(os.path.join(D, "pdf")) == [] if os.path.isdir(os.path.join(D, "pdf")) else True)
    ck("docs/cards/matrix 已删除",
       not any(os.path.isfile(os.path.join(D, n)) for n in ("docs.json", "cards.json", "matrix.json")))
    ck("新鲜度不再说「结果可下载」", stt["freshness"]["draft"] == "missing", stt["freshness"]["draft"])

    print()
    print("4) 保留项（不该被误删）")
    ck("处理方式设置保留（privacy.json）", os.path.isfile(os.path.join(D, "privacy.json")))

    print()
    print("5) 清空后再上传同名文件，内容必须是新的（不能静默复用旧文件）")
    probe = _fixtures.pdf()
    r2 = upload("/api/upload", probe)
    ck("清空后可再次上传", (r2.get("total") or 0) >= 1, r2)
    ck("新上传的文件确实写入了 data/pdf", len(os.listdir(os.path.join(D, "pdf"))) >= 1,
       os.listdir(os.path.join(D, "pdf"))[:3])
finally:
    p.terminate()
    try: p.wait(timeout=15)
    except Exception: p.kill()

# 必须还原现场：这个套件会**真的删光** data/pdf 与 outputs 里的产物，
# 不还原会让后面的套件拿不到 001.pdf 之类的文件（实测导致全流程套件
# 在 2 秒内因 FileNotFoundError 直接退出）。
# 注意只 restore、不 snapshot：后者会把"已清空"的现场覆盖成基线快照。
try:
    import snapshot
    snapshot.restore()
    print("  [快照] 已还原测试现场")
except Exception as e:
    print("  [快照] 还原失败：%s" % str(e)[:100])

print()
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)