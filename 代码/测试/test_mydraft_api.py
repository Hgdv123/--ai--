# -*- coding: utf-8 -*-
"""端到端测试"检查我的稿子"接口。"""

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
import shutil
import sys
# ── 自带服务：用当前代码起一个，跑完自动关（避免连到残留旧进程）──
import server as _server

_server_handle = _server.ensure(force_own=False)
BASE = _server.BASE
B = _server.BASE
import urllib.parse
import urllib.request
import uuid

SRC_DRAFT = _fixtures.docx()

PASS, FAIL = [], []

def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:70]) if detail else ""))

def get(p, timeout=300):
    with urllib.request.urlopen(B + p, timeout=timeout) as r:
        return json.load(r)

def post(p, fields=None, timeout=1200):
    data = urllib.parse.urlencode(fields or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(B + p, data=data, method="POST"),
                                    timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # 接口在 500 时会把真实错误放在响应体里。不捕获就会只看到一个
        # "HTTP Error 500"，最有用的信息全丢了（实测为此多花了很多时间）。
        body = e.read().decode("utf-8", "ignore")
        print("     !! %s 返回 %d：%s" % (p, e.code, body[:400]))
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": body[:300]}

def post_file(p, path, timeout=600):
    data = open(path, "rb").read()
    b = uuid.uuid4().hex
    body = ("--%s\r\n" % b).encode()
    body += b'Content-Disposition: form-data; name="files"; filename="my-draft.docx"\r\n'
    body += b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
    body += data + b"\r\n" + ("--%s--\r\n" % b).encode()
    req = urllib.request.Request(B + p, data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=%s" % b},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

print("=" * 70)
print("1. 初始状态（应先清理）")
print("=" * 70)
post("/api/mydraft/delete")
st = get("/api/mydraft")
ck("无稿子时 has_draft=False", st.get("has_draft") is False, st)

papers_before = get("/api/papers")["count"]
print("  文献库当前 %d 篇" % papers_before)

print()
print("=" * 70)
print("2. 上传稿子")
print("=" * 70)
r = post_file("/api/mydraft/upload", SRC_DRAFT)
ck("上传成功", r.get("ok") is True, r)
print("     标题=%s" % r.get("title"))
print("     字数=%s | 参考文献区=%s 条 | 章节=%s" % (r.get("chars"), r.get("ref_count"), r.get("sections")))
ck("识别出参考文献区", (r.get("ref_count") or 0) >= 4, r.get("ref_count"))
ck("识别出多个章节", len(r.get("sections") or []) >= 3, r.get("sections"))

print()
print("=" * 70)
print("3. 关键约束：稿子不得进入文献库")
print("=" * 70)
papers_after = get("/api/papers")["count"]
ck("文献库篇数不变（%d → %d）" % (papers_before, papers_after),
   papers_after == papers_before, papers_after)
titles = [p.get("title") or "" for p in get("/api/papers")["papers"]]
ck("文献库中没有出现稿子标题",
   not any("小样本图像分类方法研究综述" in t for t in titles), titles[:3])

print()
print("=" * 70)
print("4. 执行检查")
print("=" * 70)
rep = post("/api/mydraft/check")
if not rep.get("ok"):
    # 接口把真实错误放在响应体里（500 时）。测试脚本原先直接抛 HTTPError，
    # 把最有用的错误信息丢掉了——这里显式打印，避免"只看到一个 500"。
    print("     !! 检查失败，服务端返回：%s" % str(rep)[:400])
ck("检查成功", rep.get("ok") is True, str(rep)[:80] if not rep.get("ok") else "")
s = rep.get("summary") or {}
print("     问题 %s 处（高%s/中%s/低%s）" % (s.get("total"), s.get("high"), s.get("medium"), s.get("low")))
print("     正文引用 %s 处 | 参考文献 %s 条 | 库 %s 篇" % (
    s.get("citations_in_text"), s.get("refs_entries"), s.get("library_total")))
print("     可核实性: 命中 %s 条 / 未命中 %s 条" % (s.get("matched_library"), s.get("unmatched")))

kinds = {i["kind"] for i in (rep.get("issues") or [])}
EXPECT = ["引用真实性", "文末未引用", "格式合规", "重复著录", "论断有据", "该引未引"]
for k in EXPECT:
    ck("检出「%s」" % k, k in kinds)

# 「引用可核实性」只有在稿子里存在"库里查不到的参考文献"时才可能出现。
# 上面这份干净稿的参考文献全都在库中（未命中 0 条），这一项在它身上无从触发，
# 不是漏报。所以换一份含未命中引用的稿子再检一次，把这一项真正跑到。
if (s.get("unmatched") or 0) > 0:
    ck("检出「引用可核实性」", "引用可核实性" in kinds)
else:
    print("     这份稿子未命中 0 条 → 换含未收录引用的稿子再检一次")
    post_file("/api/mydraft/upload", _fixtures.docx_with_unmatched_ref())
    rep_u = post("/api/mydraft/check")
    su = rep_u.get("summary") or {}
    kinds_u = {i["kind"] for i in (rep_u.get("issues") or [])}
    ck("检出「引用可核实性」（换含未收录引用的稿子）",
       "引用可核实性" in kinds_u,
       "命中 %s / 未命中 %s" % (su.get("matched_library"), su.get("unmatched")))

print()
print("=" * 70)
print("5. 报告可持久化读取")
print("=" * 70)
st2 = get("/api/mydraft")
ck("再次读取仍带报告", "report" in st2, list(st2.keys()))
ck("报告含 issues 列表", isinstance((st2.get("report") or {}).get("issues"), list))

print()
print("=" * 70)
print("6. 必填与错误路径")
print("=" * 70)
# 上传 PDF 应被拒绝并给出提示
try:
    r2 = post_file("/api/mydraft/upload", os.path.join(PROJ, "data", "pdf", "001.pdf"))
    ck("上传 PDF 被拒绝", r2.get("ok") is False, r2.get("error", "")[:40])
except urllib.error.HTTPError as e:
    ck("上传 PDF 被拒绝（HTTP %d）" % e.code, e.code == 400)
# 上传非 docx 文本
tmp = os.path.join(E.tmp_dir(), "_bad.txt")
open(tmp, "w", encoding="utf-8").write("not a docx")
try:
    r3 = post_file("/api/mydraft/upload", tmp)
    ck("上传 txt 被拒绝", r3.get("ok") is False, r3.get("error", "")[:40])
except urllib.error.HTTPError as e:
    ck("上传 txt 被拒绝（HTTP %d）" % e.code, e.code == 400)

print()
print("=" * 70)
print("7. 删除稿子")
print("=" * 70)
d = post("/api/mydraft/delete")
ck("删除成功", d.get("ok") is True, d.get("removed"))
st3 = get("/api/mydraft")
ck("删除后 has_draft=False", st3.get("has_draft") is False)
ck("删除后文献库仍 %d 篇" % get("/api/papers")["count"],
   get("/api/papers")["count"] == papers_before)

print()
print("=" * 70)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 70)
if FAIL:
    for x in FAIL:
        print("  ✗ %s" % x)

# ── 收尾：关掉本次启动的服务 ──
try:
    _server_handle.stop()
except Exception:
    pass
