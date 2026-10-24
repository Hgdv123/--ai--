# -*- coding: utf-8 -*-
"""验证：过程叙述不再被当成审查结论，结论改为以实测数字为准。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _testenv as E  # noqa: E402

E.activate()
from 源程序.agent import sanitize_agent_summary, factual_summary, _compose_conclusion  # noqa: E402

# 截图里那段原文
NARRATION = ("根据初稿内容，我将首先使用 `check_citation_existence` 和 `check_ordering` "
             "工具来检查引用的正确性和顺序性，然后再使用 `check_claim_support` 工具"
             "来验证每个引用是否真的有原文支撑。")

REAL = "初稿中的引用基本符合原文内容，但个别句子的引用细节需要进一步明确。"

REPORT = {"issues_total": 4, "high": 4, "medium": 0, "low": 0,
          "by_kind": {"论断有据（回原文核对）": 4},
          "evidence_check": {"mode": "RAG-evidence", "claims_total": 7,
                             "citations_located": 15, "claims_judged": 7,
                             "supported": 3, "weak": 0, "unsupported": 4,
                             "unresolved": 0, "llm_used": True,
                             "support_rate": 0.4286}}

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("OK " if ok else "FAIL", name,
                         ("  → " + str(detail)[:120]) if detail else ""))


print("=" * 70)
print("1. sanitize：过程叙述不应被当成结论")
print("=" * 70)
s = sanitize_agent_summary(NARRATION)
ck("截图里那段计划被判定为无有效结论", "没有产出可读的文字结论" in s, s[:60])

print()
print("=" * 70)
print("2. sanitize：真正的结论要保留")
print("=" * 70)
s2 = sanitize_agent_summary(REAL)
ck("真实结论原样保留", s2 == REAL, s2[:60])
s3 = sanitize_agent_summary("引用[1]在参考文献表中不存在，属于假引用，需补全或删除。")
ck("含结论内容的句子不被误杀", s3 == "引用[1]在参考文献表中不存在，属于假引用，需补全或删除。", s3[:60])
s4 = sanitize_agent_summary("<tools>{\"type\":\"function\"}</tools>审查完成，发现两处问题。")
ck("工具定义泄漏仍被清除", "tools" not in s4 and "两处问题" in s4, s4[:60])

print()
print("=" * 70)
print("3. factual_summary：结论由实测数字构成")
print("=" * 70)
f = factual_summary(REPORT)
print("   →", f)
ck("报告了问题总数与分级", "4 处问题" in f and "高 4" in f)
ck("报告了回原文核对结果", "7 条论断" in f and "4 条无支撑" in f)
ck("给出了支持率", "43%" in f)
ck("列出了问题分布", "论断有据（回原文核对） 4 处" in f)

print()
print("=" * 70)
print("4. _compose_conclusion：模型文字 + 实测数字")
print("=" * 70)
c1 = _compose_conclusion(NARRATION, REPORT)
print("   叙述型输入 →", c1.replace("\n", " / ")[:230])
ck("用数字替代了过程叙述", "我将首先使用" not in c1 and "4 处问题" in c1)
c2 = _compose_conclusion(REAL, REPORT)
print("   结论型输入 →", c2.replace("\n", " / ")[:250])
ck("保留模型结论并附上数字", REAL in c2 and "4 处问题" in c2)
ck("实测数字排在模型文字之前", c2.index("4 处问题") < c2.index(REAL))
ck("模型文字被标明仅供参考", "仅供参考" in c2)
c3 = _compose_conclusion("", {})
ck("无报告无文字时返回空串", c3 == "", repr(c3))
c4 = _compose_conclusion("", REPORT)
ck("只有报告时仍给结论", "4 处问题" in c4, c4[:70])

print()
print("=" * 70)
print("5. 运行状态类文字不应冒充 AI 的概括")
print("=" * 70)
FAIL_TEXT = "模型调用失败（本地模型调用失败（llama-server 未就绪或返回异常）），已按确定性顺序完成全部检查，结论与报告不受影响。"
c5 = _compose_conclusion(FAIL_TEXT, REPORT)
print("   →", c5.replace("\n", " / ")[:200])
ck("失败说明不再当成 AI 概括", "模型调用失败" not in c5 and "仅供参考" not in c5)
ck("数字仍然保留", "4 处问题" in c5)
c6 = _compose_conclusion("云端与本地模型均不可用（未配置有效的百炼 Key），已按确定性顺序完成全部检查。", REPORT)
ck("模型皆不可用的说明同样不冒充概括", "百炼 Key" not in c6 and "4 处问题" in c6)

print()
print("=" * 70)
print("汇总：通过 %d，失败 %d" % (len(PASS), len(FAIL)))
print("=" * 70)
sys.exit(1 if FAIL else 0)
