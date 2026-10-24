# -*- coding: utf-8 -*-
"""新功能系统测试：题录解析（F1） + 该引未引检测。

覆盖：
  A. 题录解析 —— 三种格式 × 正常/边界/异常
  B. 题录导入 —— 接口、落库、与新鲜度的衔接、删除
  C. 该引未引 —— 正例、负例（自述/过渡句）、空输入、无文献库
  D. 回归 —— 原有能力是否被破坏
"""
import json
import os
import sys

# ── 自带服务：没有就起一个（复用已在 8848 上跑的，避免重复启动）──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 路径与包名引导：包目录已改名「源程序」，本文件里的 `from wenmai...` 靠这行
# 安装的别名才能解析（否则 ModuleNotFoundError）。顺带把数据指向隔离沙箱。
import _testenv as ENV  # noqa: E402
PROJ = ENV.activate()
import server as _server

_server_handle = _server.ensure(force_own=False)
B = _server.BASE
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:70]) if detail else ""))


def get(p, timeout=60):
    return json.load(urllib.request.urlopen(B + p, timeout=timeout))


def post_form(p, fields, timeout=120):
    data = urllib.parse.urlencode(fields).encode()
    return json.load(urllib.request.urlopen(
        urllib.request.Request(B + p, data=data, method="POST"), timeout=timeout))


# ══════════════ A. 题录解析 ══════════════
print("=" * 68)
print("A. 题录解析（直接调解析器）")
print("=" * 68)
from wenmai.refparse import parse_refs, detect_format

A_CASES = [
    ("RefWorks 标签式（知网默认）",
     "A1: 张三, 李四\nT1: 基于对比学习的小样本图像分类\nJF: 计算机学报\nYR: 2022\nVO: 45\nIS: 3\nSP: 512-530\nK1: 小样本; 对比学习\n",
     "refworks", 1, "张三", "计算机学报", 2022, "512-530"),
    ("RefWorks 多条（空行分隔）",
     "A1: 王五\nT1: 论文一\nJF: 期刊A\nYR: 2021\n\nA1: 赵六\nT1: 论文二\nJF: 期刊B\nYR: 2020\n",
     "refworks", 2, "王五", "期刊A", 2021, None),
    ("明文键值式（【】）",
     "【作者】孙七;周八\n【题名】图神经网络综述\n【刊名】软件学报\n【年】2020\n",
     "bracket", 1, "孙七", "软件学报", 2020, None),
    ("明文键值式多条",
     "【作者】甲\n【题名】论文甲\n【刊名】刊甲\n【年】2019\n\n【作者】乙\n【题名】论文乙\n【刊名】刊乙\n【年】2018\n",
     "bracket", 2, "甲", "刊甲", 2019, None),
    ("冒号键值式",
     "作者: 陈八, 周九\n题名: 面向教育数据挖掘的学习行为分析\n期刊: 电化教育研究\n年份: 2019\n",
     "kv", 1, "陈八", "电化教育研究", 2019, None),
    ("百分号标签",
     "%A 张三\n%T 百分号格式测试\n%J 测试期刊\n%D 2023\n",
     "refworks", 1, "张三", "测试期刊", 2023, None),
]

for name, text, exp_fmt, exp_n, exp_author, exp_venue, exp_year, exp_pages in A_CASES:
    fmt = detect_format(text)
    recs = parse_refs(text)
    ok = (fmt == exp_fmt and len(recs) == exp_n)
    check("%s：格式识别=%s、条数=%d" % (name, fmt, len(recs)), ok,
          "" if ok else "期望 %s/%d" % (exp_fmt, exp_n))
    if recs:
        r = recs[0]
        check("  └ 作者=%s" % exp_author, exp_author in (r.get("authors") or []),
              r.get("authors"))
        check("  └ 刊名=%s" % exp_venue, r.get("venue") == exp_venue, r.get("venue"))
        check("  └ 年份=%s" % exp_year, r.get("year") == exp_year, r.get("year"))
        if exp_pages:
            check("  └ 页码=%s" % exp_pages, r.get("pages") == exp_pages, r.get("pages"))
        check("  └ 文献类型标识", r.get("ref_type") in ("J", "C", "D", "M", "N", "EB/OL"),
              r.get("ref_type"))

print()
print("A2. 边界与异常输入")
for name, text, exp_ok in [
    ("空文本", "", True),
    ("只有空白", "   \n\n  ", True),
    ("只有一行作者", "A1: 张三", True),
    ("无结构自由文本", "这是一段随便写的文字，没有任何题录结构。\n第二行。", True),
    ("超长单行", "T1: " + "很长的标题" * 200, True),
    ("特殊字符", "A1: 张<三> & \nT1: 标题含<b>标签</b>\nYR: 2022", True),
    ("非法年份", "T1: 测试\nYR: 不是年份", True),
]:
    try:
        r = parse_refs(text)
        check("%s（不崩溃，返回 %d 条）" % (name, len(r)), True)
    except Exception as e:
        check("%s（不崩溃）" % name, False, "抛错: %s" % str(e)[:50])

# ══════════════ B. 题录导入接口 ══════════════
print()
print("=" * 68)
print("B. 题录导入接口与衔接")
print("=" * 68)
# 先让产物与文献库对齐，再断言"题录不会触发需要重新生成"。
# 这条断言只有在 docs/cards/draft 都已跟上语料库时才有意义；否则它测的是
# "上一个脚本有没有刷过产物"，数据一还原就红，掩盖真正的问题（实测踩到过）。
_st0 = get("/api/state")
if _st0["freshness"].get("needs_run") is not False:
    print("     （产物与文献库不一致，先跑一次全流程打底）")
    post_form("/api/run", {"stage": "all"}, timeout=1800)
before = get("/api/state")["corpus"]["count"]
check("导入前文献库 = %d 篇" % before, True)

r = post_form("/api/refs", {"text": "A1: 接口测试作者\nT1: 系统测试题录A\nJF: 接口期刊\nYR: 2024\n",
                            "name": "系统测试"})
check("POST /api/refs 成功（parsed=%s added=%s）" % (r.get("parsed"), r.get("added")),
      r.get("ok") and r.get("added") == 1)

st = get("/api/state")
f = st["freshness"]
check("文献库增至 %d 篇" % st["corpus"]["count"], st["corpus"]["count"] == before + 1)
check("新鲜度区分题录（有全文 %s / 仅题录 %s）" % (f.get("papers_with_fulltext"), f.get("refs_only")),
      f.get("refs_only") == 1)
check("题录不触发「需要重新生成」（needs_run=%s）" % f.get("needs_run"),
      f.get("needs_run") is False)

# 空题录应报错
try:
    post_form("/api/refs", {"text": "随便一段没有结构的文字", "name": "空测试"})
    check("空题录被拒绝", False, "未报错")
except urllib.error.HTTPError as e:
    check("空题录被拒绝（HTTP %d）" % e.code, e.code == 400)

# 删除题录（只删本次测试创建的，绝不动示例论文）
papers = get("/api/papers")["papers"]
tgt = [p for p in papers if "系统测试题录A" in (p.get("title") or "")]
check("找到本次测试创建的题录", len(tgt) == 1,
      [p.get("title") for p in papers][:8] if not tgt else "")
if tgt:
    d = post_form("/api/delete", {"local_pdf": tgt[0]["local_pdf"]})
    check("删除题录记录", d.get("ok") is True, d)
after = get("/api/state")["corpus"]["count"]
check("删除后文献库回到 %d 篇（当前 %d）" % (before, after), after == before)

# ══════════════ C. 该引未引 ══════════════
print()
print("=" * 68)
print("C. 该引未引检测")
print("=" * 68)
from wenmai.selfcheck import suggest_missing_citations, build_docs_index
from wenmai.literature import load_docs

docs = load_docs().get("docs", [])
retr, by_key = build_docs_index(docs)

pos = ("小样本图像分类在近年取得显著进展，元学习方法通过优化初始参数实现了快速任务适应。"
       "有研究表明，高质量嵌入空间比复杂算法更重要，准确率达到81.4%。")
neg_self = "本文提出了一种改进的原型网络，并在CIFAR-FS上验证了有效性。"
neg_trans = "总体来看，现有方法在跨域场景下仍存在明显不足。"
import re as _re
neg_cited = "已有工作表明元学习可以提升泛化能力[1]。"

res_pos = suggest_missing_citations(pos, retr, by_key)
check("正例：检出 %d 处需引用" % res_pos["total"], res_pos["total"] >= 1)
check("正例：给出建议文献",
      any(c.get("candidates") for c in res_pos["suggestions"]),
      (res_pos["suggestions"][0]["candidates"][:1] if res_pos["suggestions"] else "无"))

res_self = suggest_missing_citations(neg_self, retr, by_key)
check("负例1：自述句「本文提出…」被跳过", res_self["total"] == 0, res_self["total"])

res_trans = suggest_missing_citations(neg_trans, retr, by_key)
check("负例2：过渡句「总体来看…」被跳过", res_trans["total"] == 0, res_trans["total"])

res_cited = suggest_missing_citations(neg_cited, retr, by_key)
check("负例3：已有引用角标的句子被跳过", res_cited["total"] == 0, res_cited["total"])

res_empty = suggest_missing_citations("", retr, by_key)
check("空输入不崩溃", res_empty["total"] == 0)

res_noretr = suggest_missing_citations(pos, None, {})
check("无检索器时仍能提示（不崩溃）", res_noretr["total"] >= 1, res_noretr["total"])

check("GET /api/missing 可用", "total" in get("/api/missing", timeout=180))

# ══════════════ D. 回归测试 ══════════════
print()
print("=" * 68)
print("D. 回归：原有能力未被破坏")
print("=" * 68)
st = get("/api/state")
check("文献库 %d 篇" % st["corpus"]["count"], st["corpus"]["count"] == 6)
check("草稿存在（%d 条参考文献）" % len(st["draft"]["refs"]), len(st["draft"]["refs"]) > 0)
ev = st["check"].get("evidence_check", {})
check("RAG 证据核对仍工作（核对 %s 处，支持率 %s）" % (ev.get("citations_located"),
                                                   ev.get("support_rate")),
      ev.get("citations_located", 0) > 0)
check("自检结果存在（%s 处问题）" % st["check"].get("total"), "total" in st["check"])
check("该引未引计数已入报告（%s）" % st["check"].get("missing_citations"),
      "missing_citations" in st["check"])
check("Word 可下载", st.get("has_docx") is True)
check("论文清单接口可用（%d 篇）" % get("/api/papers")["count"], get("/api/papers")["count"] == 6)
check("隐私接口可用", "acknowledged" in get("/api/privacy"))

# ══════════════ 汇总 ══════════════
print()
print("=" * 68)
print("测试汇总：通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
print("=" * 68)
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  ✗ " + x)
else:
    print("全部通过 ✅")

# ── 收尾：关掉本次启动的服务 ──
try:
    _server_handle.stop()
except Exception:
    pass
