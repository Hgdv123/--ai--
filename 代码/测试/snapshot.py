# -*- coding: utf-8 -*-
"""测试用的数据快照：开始拍、结束还原。

为什么要有这个共享模块（而不是每个脚本各写一份）：

两个脚本（fulltest_A、fuzz_edge）各自实现过一份 backup/restore，都写成了
「备份已存在就直接返回」，也就是**只备份一次**。于是第一次运行之后快照
再也不更新，而 restore() 每轮都把"第一次测试之前"的旧数据盖回工作区——
测试跑得越多数据越旧。表现是"PDF 与元数据张冠李戴""语料记录字段缺失"
这类极难定位的问题，排查了很久才发现根因在测试脚手架里。

约定：
  · snapshot()  在脚本**开头**调用，永远从当前工作区重新拍一份（覆盖旧的）
  · restore()   在脚本**结尾**调用；用 try/finally 保证异常时也会还原
  · 还原前做完整性体检，快照明显损坏时**拒绝还原**并报警，
    宁可不还原也不要用坏数据覆盖工作区
"""
from __future__ import annotations

import io
import json
import os
import shutil

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BK = os.path.join(PROJ, ".dsh-tmp", "snapshot")
# 备份/还原必须是**整个目录树**（含 data/pdf、data/docx、data/mydraft 等子目录）。
# 曾经只备份了顶层文件列表，docx 子目录没进快照，还原后 Word 记录就成了
# "有记录、无文件"的孤儿——而这种半截状态会被下一次备份继承，一路污染下去
# （实测把工作区和全部备份都拖坏了）。这里明确用整树复制。
DIRS = ("data", "outputs")


def _sane(sub: str) -> bool:
    """快照体检：只查"一定成立"的结构性不变量，不做内容判断。

    刻意**不**要求语料库非空：空语料库是完全合法的状态（用户可能刚清空），
    若把它当损坏就会拒绝还原（实测踩到过——fuzz 脚本清空后无法还原）。
    """
    if sub != "data":
        return True
    cp = os.path.join(BK, sub, "corpus.json")
    if not os.path.isfile(cp):
        return True                       # 快照里本就没有语料库，不算损坏
    try:
        d = json.load(io.open(cp, encoding="utf-8"))
    except Exception:
        return False
    return isinstance(d.get("records"), list) and isinstance(d.get("count"), int)


def snapshot(verbose: bool = True) -> str:
    for sub in DIRS:
        src = os.path.join(PROJ, sub)
        dst = os.path.join(BK, sub)
        shutil.rmtree(dst, ignore_errors=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
    if verbose:
        n = "?"
        cp = os.path.join(BK, "data", "corpus.json")
        if os.path.isfile(cp):
            try:
                n = json.load(io.open(cp, encoding="utf-8")).get("count")
            except Exception:
                n = "?"
        print("  [快照] 已保存当前数据（语料库 %s 篇）" % n)
    return BK


def restore(verbose: bool = True) -> bool:
    ok = True
    for sub in DIRS:
        src = os.path.join(BK, sub)
        dst = os.path.join(PROJ, sub)
        if not os.path.isdir(src):
            continue
        if not _sane(sub):
            if verbose:
                print("  [快照] !! %s 快照不完整，放弃还原以免覆盖工作区" % sub)
            ok = False
            continue
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    if verbose:
        print("  [快照] 已还原" if ok else "  [快照] 部分未还原")
    return ok
