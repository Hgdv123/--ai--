# -*- coding: utf-8 -*-
"""验收：离线模式下"生成综述稿"要产出真正的综述段落，而不是论文清单。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as E  # noqa: E402

E.activate()
os.environ["WM_OFFLINE"] = "1"
from 源程序 import draft as D  # noqa: E402
from 源程序.config import OUT_DIR  # noqa: E402

print("=" * 70)
print("离线生成综述稿（use_llm=True）")
print("=" * 70, flush=True)
t0 = time.time()
d = D.generate(use_llm=True, verbose=True)
dt = time.time() - t0
print("-" * 70)
print("耗时      : %.1f 秒" % dt)
print("返回类型  :", type(d).__name__)
if isinstance(d, dict):
    body = d.get("body") or d.get("draft") or ""
    print("正文字数  :", len(body))
    print("llm_note  :", d.get("llm_note") or "（无，说明走的是模型通道）")
    print("通道      :", d.get("channel") or d.get("source") or "—")
    print("引用条数  :", len(d.get("refs") or []))
    print("-" * 70)
    print("正文前 400 字：")
    print(body[:400])
    print("-" * 70)

md = os.path.join(OUT_DIR, "draft.md")
if os.path.isfile(md):
    txt = open(md, encoding="utf-8").read()
    print("draft.md  : %d 字符" % len(txt))
    # 「论文清单」模式的典型特征：正文只是标题+年份的罗列
    looks_list = txt.count("（") > 3 and "研究现状" not in txt
    has_prose = any(k in txt for k in ("本文", "该", "通过", "表明", "综上", "然而"))
    print("有综述性行文 :", "是" if has_prose else "否")
    print("判定      :", "❌ 像论文清单" if looks_list and not has_prose else "OK 是综述段落")
