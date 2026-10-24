# -*- coding: utf-8 -*-
"""测试题录解析：三种常见格式。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as E  # noqa: E402

E.activate()
from wenmai.refparse import parse_refs, detect_format  # noqa: E402

CASES = {
    "RefWorks 标签式（知网默认导出）": """A1: 张三, 李四
T1: 基于对比学习的小样本图像分类方法
JF: 计算机学报
YR: 2022
VO: 45
IS: 3
SP: 512-530
K1: 小样本学习; 对比学习; 图像分类
AB: 针对小样本图像分类中标注数据稀缺的问题, 本文提出一种基于对比学习的改进方法。

A1: Wang W, Li M
T1: Few-shot learning with contrastive embedding
JF: IEEE Transactions on Pattern Analysis and Machine Intelligence
YR: 2023
K1: few-shot learning; contrastive learning
""",
    "明文键值式（知网页面复制）": """【作者】王五;赵六
【题名】基于元学习的小样本目标检测
【刊名】自动化学报
【年】2021
【关键词】元学习; 目标检测; 小样本
【摘要】本文研究了小样本条件下的目标检测问题。

【作者】孙七
【题名】图神经网络综述
【刊名】软件学报
【年】2020
""",
    "带冒号的键值式": """作者: 陈八, 周九
题名: 面向教育数据挖掘的学习行为分析
期刊: 电化教育研究
年份: 2019
关键词: 教育数据挖掘; 学习行为
""",
}

for name, text in CASES.items():
    print("=" * 66)
    print("格式：%s" % name)
    print("  识别为：%s" % detect_format(text))
    recs = parse_refs(text)
    print("  解析出 %d 条：" % len(recs))
    for i, r in enumerate(recs, 1):
        print("   [%d] %s" % (i, r["title"][:50]))
        print("       作者: %s" % ("、".join(r["authors"]) or "—"))
        print("       来源: %s | 年份: %s | 类型: [%s]" % (
            r["venue"] or "—", r["year"] or "—", r["ref_type"]))
        print("       关键词: %s" % ("、".join(r["keywords"][:4]) or "—"))
        if r["abstract"]:
            print("       摘要: %s" % r["abstract"][:50])
        print("       卷期页: %s / %s / %s" % (r["volume"] or "—", r["issue"] or "—", r["pages"] or "—"))
    print()

print("=" * 66)
print("边界情况测试")
for name, text in {
    "空文本": "",
    "只有一行作者": "A1: 张三",
    "格式混乱": "随便一段没有结构的文字，看看会不会崩。\n第二行也是。",
}.items():
    try:
        r = parse_refs(text)
        print("  %-12s → 解析出 %d 条（未崩溃）" % (name, len(r)))
    except Exception as e:
        print("  %-12s → ❌ 抛错: %s" % (name, e))
