# -*- coding: utf-8 -*-
"""阶段三 · 动态接口模糊测试：所有端点 × 正常/病态入参。"""

# ── 路径与样本文件：一律由仓库自带的测试工具推导，勿写死 ──
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
import _testenv as E
import _fixtures
HERE = _HERE          # 供 os.path.join(HERE, "..", "源程序", ...) 使用
PROJ = E.activate()
import json
import os
import sys
import time
# ── 自带服务：用当前代码起一个，跑完自动关（避免连到残留旧进程）──
import server as _server

_server_handle = _server.ensure(force_own=False)
BASE = _server.BASE
B = _server.BASE
import urllib.error
import urllib.parse
import urllib.request
import uuid
PASS, FAIL = [], []

def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:74]) if detail else ""))

def req(path, method="GET", data=None, ctype=None, timeout=900, raw=False):
    """返回 (状态码, 内容)。不抛异常，便于测病态入参。"""
    headers = {}
    if ctype:
        headers["Content-Type"] = ctype
    r = urllib.request.Request(B + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (body if raw else body.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, repr(e)[:100]

def jget(path, timeout=300):
    st, body = req(path, timeout=timeout)
    try:
        return st, json.loads(body)
    except Exception:
        return st, {"_raw": body[:200]}

def jpost(path, fields=None, timeout=900):
    data = urllib.parse.urlencode(fields or {}).encode() if fields is not None else b""
    st, body = req(path, "POST", data, "application/x-www-form-urlencoded", timeout)
    try:
        return st, json.loads(body)
    except Exception:
        return st, {"_raw": body[:200]}

print("=" * 72)
print("1. 只读接口：正常 + 病态入参")
print("=" * 72)
READ = [
    ("/api/state", None),
    ("/api/papers", None),
    ("/api/privacy", None),
    ("/api/missing", None),
    ("/api/mydraft", None),
    ("/api/evidence/detail", None),
    ("/api/evidence/detail?citation=0", None),
    ("/api/evidence/detail?citation=-1", None),
    ("/api/evidence/detail?citation=9999", None),
    ("/api/evidence/detail?citation=abc", None),
]
for path, _ in READ:
    st, body = jget(path)
    ok = st == 200
    ck("GET %s → %d" % (path, st), ok,
       "" if ok else str(body)[:70])

print()
print("=" * 72)
print("2. 隐私切换：合法与非法入参")
print("=" * 72)
for mode, expect in [("cloud", 200), ("local", 200), ("CLOUD", 400),
                     ("", 400), ("null", 400), ("../etc", 400),
                     ("cloud'; DROP TABLE", 400), ("x" * 500, 400)]:
    st, body = jpost("/api/privacy?mode=" + urllib.parse.quote(mode))
    ck("POST mode=%-18s → %d（期望 %d）" % (repr(mode)[:18], st, expect), st == expect,
       str(body)[:60] if st != expect else "")
# 复位
jpost("/api/privacy?mode=cloud")

print()
print("=" * 72)
print("3. 上传接口：各类文件与畸形请求")
print("=" * 72)

def upload(path, filename, content, ctype="application/octet-stream"):
    b = uuid.uuid4().hex
    body = ("--%s\r\n" % b).encode()
    body += ('Content-Disposition: form-data; name="files"; filename="%s"\r\n' % filename).encode()
    body += ("Content-Type: %s\r\n\r\n" % ctype).encode()
    body += content + b"\r\n" + ("--%s--\r\n" % b).encode()
    return req(path, "POST", body, "multipart/form-data; boundary=%s" % b)

# 正常 PDF

pdf = open(os.path.join(PROJ, "data", "pdf", "006.pdf"), "rb").read()
st, body = upload("/api/upload", "ok.pdf", pdf)
j = json.loads(body) if st == 200 else {}
ck("上传正常 PDF", st == 200 and len(j.get("saved") or []) == 1, st)
# 正常 docx
dx = open(_fixtures.docx(), "rb").read()
st, body = upload("/api/upload", "ok.docx", dx)
j2 = json.loads(body) if st == 200 else {}
ck("上传正常 docx", st == 200 and len(j2.get("saved") or []) == 1, st)
# 空文件
st, body = upload("/api/upload", "empty.pdf", b"")
ck("上传空文件被拒", st == 200 and len(json.loads(body).get("skipped") or []) == 1, st)
# 无文件名
st, body = upload("/api/upload", "", b"data")
ck("无文件名被拒", st == 200 and len(json.loads(body).get("skipped") or []) == 1, st)
# 超长文件名
st, body = upload("/api/upload", "a" * 300 + ".pdf", pdf)
ck("超长文件名不崩", st == 200, st)
# 路径穿越文件名
st, body = upload("/api/upload", "../../../evil.pdf", pdf)
ck("路径穿越文件名不崩", st == 200, st)
# 空 multipart
st, body = req("/api/upload", "POST", b"", "multipart/form-data; boundary=xxx")
ck("空 multipart 不崩溃（4xx）", st in (400, 422), st)

# 清理刚上传的
_, pj = jget("/api/papers")
for p in pj.get("papers", []):
    t = p.get("title") or ""
    # 注意：上传后标题取自文件名（去掉扩展名），所以 "ok.pdf" → "ok"，
    # 只判断 startswith("ok") 会漏掉等号情况，实测留下过残留记录。
    if (t in ("ok", "empty", "data")
            or t.startswith(("a" * 50, "..", "evil"))
            or (p.get("source") or "") == "用户上传"):
        jpost("/api/delete", {"local_pdf": p.get("local_pdf")})

print()
print("=" * 72)
print("4. 删除接口：越权与畸形")
print("=" * 72)
for key, expect in [("", 400), ("pdf/nope.pdf", 404),
                    ("../../../windows/system32/cmd.exe", 404),
                    ("..\\..\\boot.ini", 404),
                    ("/etc/passwd", 404),
                    ("ref999.json", 404)]:
    st, body = jpost("/api/delete", {"local_pdf": key})
    ck("删除 %-38s → %d（期望 %d）" % (repr(key)[:38], st, expect), st == expect,
       str(body)[:50] if st != expect else "")

print()
print("=" * 72)
print("5. 题录导入：边界与注入")
print("=" * 72)
REF_CASES = [
    ("空文本", {"text": ""}, 400),
    ("纯空白", {"text": "   \n  "}, 400),
    ("超长文本", {"text": "A1: 张三\nT1: " + "长标题" * 3000}, 200),
    ("含 SQL 注入", {"text": "A1: ' OR 1=1--\nT1: test"}, 200),
    ("含 HTML 标签", {"text": "A1: <script>alert(1)</script>\nT1: <b>x</b>\nYR: 2020"}, 200),
    ("多条混排", {"text": "A1: A\nT1: t1\nJF: j\nYR: 2020\n\nA1: B\nT1: t2\nJF: j\nYR: 2021"}, 200),
    ("非法年份", {"text": "T1: t\nYR: 99999"}, 200),
]
for name, fields, expect in REF_CASES:
    st, body = jpost("/api/refs", fields)
    ck("%-14s → %d（期望 %d）" % (name, st, expect), st == expect, str(body)[:55] if st != expect else "")
# 清理导入的题录
_, pj2 = jget("/api/papers")
for p in pj2.get("papers", []):
    if (p.get("local_pdf") or "").startswith("ref"):
        jpost("/api/delete", {"local_pdf": p["local_pdf"]})

print()
print("=" * 72)
print("6. 我的稿子接口：病态入参")
print("=" * 72)
jpost("/api/mydraft/delete")
st, body = jpost("/api/mydraft/check")
ck("未上传就检查 → 400", st == 400, st)
st, body = upload("/api/mydraft/upload", "bad.txt", b"hello")
ck("上传 txt → 400", st == 400, st)
st, body = upload("/api/mydraft/upload", "bad.pdf", pdf)
ck("上传 PDF → 400", st == 400, st)
st, body = upload("/api/mydraft/upload", "empty.docx", b"")
ck("上传空 docx → 400", st == 400, st)

print()
print("=" * 72)
print("7. 并发请求（找竞态）")
print("=" * 72)
from concurrent.futures import ThreadPoolExecutor

def hit(path):
    st, _ = jget(path, timeout=120)
    return st

with ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(hit, ["/api/state"] * 8))
ck("8 并发 /api/state 全部 200", all(s == 200 for s in results), results)

with ThreadPoolExecutor(max_workers=6) as ex:
    results = list(ex.map(hit, ["/api/papers", "/api/privacy", "/api/mydraft"] * 2))
ck("6 并发混合只读接口全部 200", all(s == 200 for s in results), results)

print()
print("=" * 72)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 72)
for n, d in FAIL:
    print("  ✗ %s　%s" % (n, d))

# ── 收尾：关掉本次启动的服务 ──
try:
    _server_handle.stop()
except Exception:
    pass
