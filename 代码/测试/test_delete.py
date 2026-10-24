# -*- coding: utf-8 -*-
"""删除专项：三种传参方式都必须能删，PDF 与 Word 都要能删。"""

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

PORT = 8851
BASE = "http://127.0.0.1:%d" % PORT
PASS, FAIL = [], []
def ck(n, ok, d=""):
    (PASS if ok else FAIL).append(n)
    print("  %s %s%s" % ("OK " if ok else "!! ", n, ("  -> " + str(d)[:110]) if d else ""))

env = dict(os.environ); env["PYTHONPATH"] = PROJ; env["PYTHONIOENCODING"] = "utf-8"
env["WM_OFFLINE"] = "0"
p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", E.PKG, "serve", "--port", str(PORT)],
                     cwd=PROJ, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def jget(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=60).read().decode("utf-8"))
def post(path, data=None, ctype=None):
    body = urllib.parse.urlencode(data).encode() if data else b""
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", ctype or "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode("utf-8"))
        except Exception: return e.code, {}
def upload(fp, name=None):
    data = open(fp, "rb").read()
    b = "----wmdel"
    body = (("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (b, name or os.path.basename(fp))).encode()
            + data + ("\r\n--%s--\r\n" % b).encode())
    req = urllib.request.Request(BASE + "/api/upload", data=body, method="POST")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % b)
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))

DOCX = _fixtures.docx()
try:
    for _ in range(60):
        time.sleep(1)
        try:
            jget("/api/state"); break
        except Exception: pass

    print("1) 上传一个 Word 文件（复刻用户遇到的情况）")
    r = upload(DOCX, "测试用稿子.docx")
    ck("上传成功", r.get("ok") is True, r)
    papers = jget("/api/papers")["papers"]
    docx_rec = [x for x in papers if (x.get("kind") == "docx") or str(x.get("local_pdf","")).endswith(".docx")]
    ck("清单里出现该 Word 记录", bool(docx_rec), [x.get("local_pdf") for x in papers])
    ident = docx_rec[0]["local_pdf"] if docx_rec else ""
    print("     标识 =", repr(ident))

    print()
    print("2) 用【查询参数】删除（这就是界面 500 时报错的调用方式）")
    st, r = post("/api/delete?local_pdf=" + urllib.parse.quote(ident))
    ck("查询参数删除成功（原来这里报 400）", st == 200 and r.get("ok") is True, "%s %s" % (st, r))
    ck("列表中已移除", not any(str(x.get("local_pdf","")).endswith(".docx")
                              for x in jget("/api/papers")["papers"]))
    ck("Word 物理文件已删除",
       not os.path.isfile(os.path.join(PROJ, "data", "docx", os.path.basename(ident))))

    print()
    print("3) 用【表单字段】删除")
    r = upload(DOCX, "再来一个.docx")
    ident2 = [x["local_pdf"] for x in jget("/api/papers")["papers"]
              if str(x.get("local_pdf","")).endswith(".docx")][0]
    st, rr = post("/api/delete", {"local_pdf": ident2})
    ck("表单字段删除成功", st == 200 and rr.get("ok") is True, "%s %s" % (st, rr))

    print()
    print("4) 用【JSON 体】删除")
    r = upload(DOCX, "第三个.docx")
    ident3 = [x["local_pdf"] for x in jget("/api/papers")["papers"]
              if str(x.get("local_pdf","")).endswith(".docx")][0]
    st, rr = post("/api/delete", None, ctype="application/json")
    req = urllib.request.Request(BASE + "/api/delete",
                                 data=json.dumps({"local_pdf": ident3}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        rr = json.loads(resp.read().decode("utf-8"))
    ck("JSON 体删除成功", rr.get("ok") is True, rr)

    print()
    print("5) 反斜杠路径也要能删（Windows 上常见）")
    r = upload(DOCX, "第四个.docx")
    ident4 = [x["local_pdf"] for x in jget("/api/papers")["papers"]
              if str(x.get("local_pdf","")).endswith(".docx")][0]
    st, rr = post("/api/delete?local_pdf=" + urllib.parse.quote(ident4.replace("/", "\\")))
    ck("反斜杠标识能删除", st == 200 and rr.get("ok") is True, "%s %s" % (st, rr))

    print()
    print("6) 错误路径仍然正确")
    st, rr = post("/api/delete")
    ck("空标识 → 400", st == 400, st)
    st, rr = post("/api/delete?local_pdf=pdf/nope.pdf")
    ck("不存在的 → 404", st == 404, st)
finally:
    p.terminate()
    try: p.wait(timeout=15)
    except Exception: p.kill()
    import shutil

print()
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)

# 统一走快照机制还原（不要用桌面副本当恢复源，那会把污染来回传染）
try:
    import snapshot
    snapshot.restore()
    print("  [快照] 已还原测试现场")
except Exception as e:
    print("  [快照] 还原失败：%s" % str(e)[:100])
