# -*- coding: utf-8 -*-
"""阶段三 · 第二波：边界数据状态 + 模式协同 + 一致性核对。"""

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
import snapshot as _snap
import urllib.parse
import urllib.request
import uuid
BK = os.path.join(E.tmp_dir(), "bk3")
PASS, FAIL = [], []

def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:74]) if detail else ""))

def jget(path, timeout=600):
    try:
        with urllib.request.urlopen(B + path, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {"_err": e.read().decode("utf-8", "ignore")[:150]}
    except Exception as e:
        return 0, {"_err": repr(e)[:100]}

def jpost(path, fields=None, timeout=1800):
    data = urllib.parse.urlencode(fields or {}).encode() if fields is not None else b""
    try:
        req = urllib.request.Request(B + path, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {"_err": e.read().decode("utf-8", "ignore")[:150]}
    except Exception as e:
        return 0, {"_err": repr(e)[:100]}

_snap.snapshot()
print("=" * 72)
print("1. 数据一致性：界面显示 vs 实际产物")
print("=" * 72)
st, s = jget("/api/state")

draft = json.load(open(os.path.join(PROJ, "outputs", "draft.json"), encoding="utf-8"))
chk = json.load(open(os.path.join(PROJ, "outputs", "selfcheck.json"), encoding="utf-8"))
cards = json.load(open(os.path.join(PROJ, "data", "cards.json"), encoding="utf-8"))
docs = json.load(open(os.path.join(PROJ, "data", "docs.json"), encoding="utf-8"))

ck("state.corpus.count = 语料库实际",
   s["corpus"]["count"] == json.load(open(os.path.join(PROJ, "data", "corpus.json"),
                                          encoding="utf-8"))["count"],
   s["corpus"]["count"])
ck("state.docs.count = docs.json 实际",
   s["docs"]["count"] == docs["count"], "%s vs %s" % (s["docs"]["count"], docs["count"]))
ck("state.cards.count = cards.json 实际",
   s["cards"]["count"] == cards["count"], "%s vs %s" % (s["cards"]["count"], cards["count"]))
ck("state.draft.refs = draft.json refs",
   len(s["draft"]["refs"]) == len(draft["refs"]),
   "%d vs %d" % (len(s["draft"]["refs"]), len(draft["refs"])))
ck("state.check.total = selfcheck.json total",
   s["check"]["total"] == chk["summary"]["total"],
   "%s vs %s" % (s["check"]["total"], chk["summary"]["total"]))
ck("state.check_issues 条数 = check.total",
   len(s.get("check_issues") or []) == chk["summary"]["total"],
   "%d vs %d" % (len(s.get("check_issues") or []), chk["summary"]["total"]))

# 正文里的引用编号必须都在参考文献表里
md = draft["markdown"]
body = md.split("## 参考文献")[0]
import re
cited = set()
for m in re.finditer(r"\[(\d{1,3})(?:\s*[-,–]\s*(\d{1,3}))*\]", body):
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    cited.update(range(min(a, b), max(a, b) + 1))
ref_idx = set()
for r in draft["refs"]:
    mm = re.match(r"\[(\d+)\]", r.get("cite") or "")
    if mm:
        ref_idx.add(int(mm.group(1)))
ck("正文引用编号全部存在于参考文献表", cited <= ref_idx,
   "正文有而表中无: %s" % sorted(cited - ref_idx)[:6])
ck("参考文献表编号连续从 1 开始",
   sorted(ref_idx) == list(range(1, len(ref_idx) + 1)), sorted(ref_idx)[:8])

print()
print("=" * 72)
print("2. 边界状态：只有 1 篇论文")
print("=" * 72)
cj = os.path.join(PROJ, "data", "corpus.json")
c0 = json.load(open(cj, encoding="utf-8"))
one = dict(c0)
one["records"] = c0["records"][:1]
one["count"] = 1
json.dump(one, open(cj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
st, r = jpost("/api/run", {"stage": "all"})
ck("1 篇论文跑全流程不崩", st == 200, str(r)[:70])
st, s1 = jget("/api/state")
ck("1 篇时草稿仍生成", bool((s1.get("draft") or {}).get("markdown")))
ck("1 篇时自检有结果", "total" in (s1.get("check") or {}))

# 空文献库
zero = dict(c0)
zero["records"] = []
zero["count"] = 0
json.dump(zero, open(cj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
st, r = jpost("/api/run", {"stage": "cards"})
ck("空库跑卡片不崩（非 500）", st != 500, st)
st, s0 = jget("/api/state")
ck("空库时 state 正常返回", st == 200 and s0["corpus"]["count"] == 0, st)

print()
print("=" * 72)
print("3. 模式协同：稿子与文献库互不干扰")
print("=" * 72)
_snap.restore()
st, _ = jpost("/api/mydraft/delete")
# 上传稿子
dx = open(_fixtures.docx(), "rb").read()
b = uuid.uuid4().hex
body = ("--%s\r\n" % b).encode()
body += b'Content-Disposition: form-data; name="files"; filename="d.docx"\r\n'
body += b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
body += dx + b"\r\n" + ("--%s--\r\n" % b).encode()
req = urllib.request.Request(B + "/api/mydraft/upload", data=body,
                             headers={"Content-Type": "multipart/form-data; boundary=%s" % b},
                             method="POST")
urllib.request.urlopen(req, timeout=300).read()
st, s = jget("/api/state")
ck("上传稿子后文献库篇数不变", s["corpus"]["count"] == 6, s["corpus"]["count"])
st, md = jget("/api/mydraft")
ck("稿子可读", md.get("has_draft") is True)
# 跑写综述流程，确认稿子未混入
st, r = jpost("/api/run", {"stage": "all"})
st, s2 = jget("/api/state")
titles = [x.get("title") or "" for x in (s2.get("cards") or {}).get("cards", [])] \
    if isinstance(s2.get("cards"), dict) else []
st, pj = jget("/api/papers")
ck("跑完写综述后文献库仍 6 篇", pj["count"] == 6, pj["count"])
ck("文献库中没有稿子标题",
   not any("小样本图像分类方法研究综述" in (p.get("title") or "") for p in pj["papers"]))
st, md2 = jget("/api/mydraft")
ck("稿子仍在（未被写综述流程覆盖）", md2.get("has_draft") is True)

print()
print("=" * 72)
print("4. 重复操作与幂等性")
print("=" * 72)
st1, r1 = jpost("/api/mydraft/check")
st2, r2 = jpost("/api/mydraft/check")
ck("连续两次稿子检查都成功", st1 == 200 and st2 == 200)
ck("两次检查问题数一致（幂等）",
   (r1.get("summary") or {}).get("total") == (r2.get("summary") or {}).get("total"),
   "%s vs %s" % ((r1.get("summary") or {}).get("total"), (r2.get("summary") or {}).get("total")))
st, r = jpost("/api/mydraft/delete")
ck("重复删除不报错", st == 200, st)
st, r = jpost("/api/mydraft/delete")
ck("再次删除仍 200", st == 200, st)

st, r1 = jpost("/api/run", {"stage": "matrix"})
st, r2 = jpost("/api/run", {"stage": "matrix"})
ck("重复跑矩阵阶段正常", st == 200, st)

print()
print("=" * 72)
print("5. stage 参数边界")
print("=" * 72)
# 非法阶段必须**明确拒绝**（400）。原来未知值不匹配任何分支，等于"什么都没做"
# 却返回 200；若默认值哪天改成 all，写错参数就会静默跑一整套并消耗云端额度。
VALID = {"all", "cards", "matrix", "draft", "check", "export"}
for stage in ["bogus", "CARDS", "all; rm -rf", "../x", " cards ", "all;"]:
    st, r = jpost("/api/run", {"stage": stage}, timeout=1200)
    # 先 strip 再判定：首尾空格不算写错（" cards " 会被当成 cards 正常执行）
    should_ok = stage.strip() in VALID
    if should_ok:
        ck("stage=%-14s 合法值正常执行" % repr(stage)[:14], st == 200, st)
    else:
        ck("非法 stage=%-14s 被拒绝（非 200/500）" % repr(stage)[:14],
           st == 400, st)
        ck("  拒绝时给可读提示", "未知阶段" in str(r), str(r)[:70])

print()
print("=" * 72)
print("6. Agent 接口（真实调用）")
print("=" * 72)
st, r = jpost("/api/agent", {"max_steps": "2"}, timeout=2400)
ck("Agent 接口返回 200", st == 200, str(r)[:70])
ck("Agent 有结论或报告",
   bool(r.get("report") or r.get("summary") or r.get("status")), list(r.keys())[:6])

_snap.restore()
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
