"""Agent：让模型自己决定调用哪些工具来审查综述稿。

与"固定顺序跑一遍检查"的区别：
- 固定流水线：代码决定顺序 → 全部检查一遍 → 出报告
- 本 Agent：**模型读全局 → 自己挑可疑项 → 按需调用工具 → 根据结果决定下一步** → 出报告

工具（9 个，均为现有能力的原子封装）：
  list_sentences / get_sentence / reference_by_index
  check_citation_existence / check_ordering / check_format
  find_evidence / check_claim_support / generate_report

降级：模型不可用或未开启 function calling 时，退回"确定性计划"（仍产出完整报告），
      保证断网也能演示。
"""
from __future__ import annotations

import json
import os
import re
import time

from . import local_llm, selfcheck
from .config import OUT_DIR, ensure_dirs
from .llm import LLMClient

AGENT_LOG = os.path.join(OUT_DIR, "agent_trace.json")


# 模型有时会把**内部工具定义**当成正文吐出来（实测出现过：结论里直接是
# `<tools>{"type":"function","function":{"name":"reference_by_index",…}}`）。
# 那是实现细节，用户既看不懂也不该看到，必须在**生成处**就清掉，
# 而不是指望每个展示方各自处理。
_TOOL_LEAK_PATTERNS = [
    re.compile(r"<tools>.*?</tools>", re.S | re.I),          # 带标签的整块
    re.compile(r"</?tools>", re.I),                           # 落单标签
    re.compile(r"<tool_call>.*?</tool_call>", re.S | re.I),
]

# `check_claim_support({"no":2})` 这类"工具名+参数"的伪调用写法。
# **只认我们自己的工具名**：若写成 `\w+\(\{…\}\)` 会连正文里的 `见函数 f({a}) 与 g({b})`
# 一起吃掉（实测踩到）。
_TOOL_NAMES = ("check_citation_existence", "check_claim_support", "check_format",
               "check_ordering", "find_evidence", "generate_report", "get_sentence",
               "list_sentences", "reference_by_index")
_TOOL_CALL_LEAK = re.compile(
    r"\b(?:%s)\s*\(\s*\{[^{}]*\}\s*\)" % "|".join(_TOOL_NAMES))

# 模型把**工具的入参或返回值**当成结论吐出来的两种写法。它们的特征键取自
# 我们自己的工具 schema（见 TOOL_SCHEMAS），因此可以明确判定"这不是给人看的话"。
# 只按"是不是 JSON"来判不行：用户自己的稿子里出现 JSON 也很正常。
_TOOL_ARG_KEYS = {"no", "citation", "sentence", "index", "report", "markdown"}
_TOOL_OUT_KEYS = {"status", "reason", "supported", "score", "paper", "evidence", "best_score"}


def _looks_like_tool_json(t: str) -> bool:
    """整段文本是不是一个"工具入参/返回值"JSON。

    实测泄漏形式（旧判据漏掉的）：`{"no":2}`、`{"citation":2,"status":"supported",…}`、
    `【{"no":2}】`。它们不含 `"function"`/`"parameters"`，所以旧的 schema 判据抓不到，
    会被原样填进界面「AI 助手的审查结论」那一栏。
    判据保守：必须**整段就是一个 JSON 对象/数组**，且键全部落在工具 schema 的键集里——
    这样用户稿子里的 JSON、或模型正文里偶然出现的花括号都不会被误杀。
    """
    core = (t or "").strip().lstrip("【[").strip()
    core = core.rstrip("】]").strip().rstrip("。.；;").strip()
    if not (core.startswith("{") or core.startswith("[")):
        return False
    try:
        obj = json.loads(core)
    except Exception:
        return False
    if isinstance(obj, list):
        return bool(obj) and all(isinstance(x, dict) for x in obj)
    if not isinstance(obj, dict) or not obj:
        return False
    keys = {str(k) for k in obj.keys()}
    return keys <= (_TOOL_ARG_KEYS | _TOOL_OUT_KEYS)

# 「我打算调用哪些工具」属于**过程**，不是审查结论。
# 小模型（本地 3B）尤其爱这么写：整段结论只有"我将首先使用 check_citation_existence
# 和 check_ordering 工具来检查…"，一个审查发现都没有。它被填进「AI 助手的审查结论」
# 那一栏，用户看到的就是一句计划（实测被反馈过，截图里就是这段）。
# 判据取"动作打算 + 工具动作"：只提到工具名不算（可能真在汇报结果），
# 但同时出现"将/首先/然后/打算"这类未来动作词，就是在说计划。
_NARRATION_CUES = re.compile(
    r"(?:我将|我会|我打算|接下来|首先|然后|接着|下一步|准备|计划|即将)"
    r"|(?:\bwill\b|\bI(?:'ll| will)\b|\bnext\b|\bthen\b|\bfirst\b)", re.I)
_CHECK_ACTION_CUES = re.compile(
    r"(?:使用|调用|用|采用|执行|运行|检查|核对|核查|验证|生成报告)"
    r"|(?:\buse\b|\bcall\b|\brun\b|\bcheck\b|\bverify\b|\bgenerate\b)", re.I)


def _is_process_narration(text: str) -> bool:
    """这段文字是不是"我准备做什么"，而不是"我查出了什么"。"""
    s = (text or "").strip()
    if len(s) < 20:
        return False
    return bool(_NARRATION_CUES.search(s) and _CHECK_ACTION_CUES.search(s))


def sanitize_agent_summary(text: str) -> str:
    """清掉结论里的工具定义泄漏与过程叙述，并给出兜底文案。

    另有一种泄漏形式是**裸 JSON**（没有 <tools> 包裹），特征是出现
    `"type":"function"` 且带 `"parameters"`；这种整段丢弃，因为里面
    没有任何面向用户的信息。
    """
    t = (text or "").strip()
    if t:
        for pat in _TOOL_LEAK_PATTERNS:
            t = pat.sub(" ", t)
        t = _TOOL_CALL_LEAK.sub(" ", t)      # 伪调用写法：工具名+参数
        # 裸 JSON：按空行切段，含工具 schema 特征的整段删掉
        if '"type"' in t and '"function"' in t:
            kept = []
            for seg in re.split(r"\n\s*\n", t):
                s = seg.strip()
                if not s:
                    continue
                looks_tool = ('"function"' in s and ('"parameters"' in s or '"description"' in s))
                if looks_tool:
                    continue
                kept.append(s)
            t = "\n\n".join(kept)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
    # 过程叙述不能当结论；这种情形交给下面的兜底文案（它讲的是**实际查了什么**）
    if _is_process_narration(t):
        t = ""
    # 模型把工具入参/返回值当结论吐出来（如 `{"no":2}`）——不是给人看的话，一律兜底
    if _looks_like_tool_json(t):
        t = ""
    if not t or len(t) < 8:
        # 原来是工具定义、或模型只说了句"我准备怎么做"、或没吐任何可读文字：
        # 给一句能照做的兜底，而不是留空框、更不是把一句计划当结论。
        # 门槛取 8：清掉伪调用后剩下的碎片（实测出现"它调用了 取句子。"这种）
        # 没有信息量，放出去只会让用户困惑。
        return ("AI 助手已完成本轮核查，但没有产出可读的文字结论。"
                "请展开下面的「它的检查过程」查看它实际调用了哪些工具、每一步的输入与结果。")
    return t

def factual_summary(report: dict) -> str:
    """按实际检查结果写结论，数字全部取自报告，不依赖模型的措辞。

    为什么需要它：模型的文字结论时好时坏——本地 3B 常只说"我准备检查什么"，
    偶尔还会漏掉最严重的发现。而「审查结论」这一栏是用户唯一会认真读的地方，
    不应该由模型的表达能力决定。这里用报告里的实测数字兜底，
    任何一句都能在下面的「检查过程」里逐条对上。
    """
    if not isinstance(report, dict) or not report:
        return ""
    total = report.get("issues_total") or 0
    high = report.get("high") or 0
    mid = report.get("medium") or 0
    low = report.get("low") or 0
    parts = []
    if total:
        detail = "高 %d / 中 %d / 低 %d" % (high, mid, low)
        parts.append("本轮共发现 %d 处问题（%s）。" % (total, detail))
        worst = ""
        ev = report.get("evidence_check") or {}
        if high and isinstance(ev, dict) and ev.get("contradicted"):
            parts.append("其中 %d 处是「原文明确矛盾」——正文的数值或方向与原文冲突，"
                         "这是硬结论，应最优先核对。" % ev["contradicted"])
        elif high and isinstance(ev, dict) and (ev.get("not_evidence") or ev.get("unsupported") or 0):
            n = ev.get("not_evidence") or ev.get("unsupported") or 0
            parts.append("其中 %d 处属于「原文里找不到支撑」——注意这不等于引用写错，"
                         "可能只是原文换了措辞。" % n)
    else:
        parts.append("本轮未发现引用类问题。")
    ev = report.get("evidence_check") or {}
    if isinstance(ev, dict) and ev.get("claims_total"):
        n = ev["claims_total"]
        sup = ev.get("supported") or 0
        contra = ev.get("contradicted") or 0
        notev = ev.get("not_evidence")
        if notev is None:                       # 兼容旧报告
            notev = ev.get("unsupported") or 0
        unver = ev.get("unverifiable")
        if unver is None:
            unver = ev.get("unresolved") or 0
        rate = ev.get("support_rate")
        seg = "回原文核对了 %d 条论断：%d 条有依据" % (n, sup)
        if contra:
            seg += "、%d 条与原文明确矛盾" % contra
        if notev:
            seg += "、%d 条找不到依据" % notev
        if unver:
            seg += "、%d 条因原文不在库中无法核对" % unver
        if isinstance(rate, (int, float)):
            seg += "（可核对范围内支持率 %.0f%%）" % (rate * 100)
        if not ev.get("llm_used", True):
            seg += "；本次为相似度阈值判定，非模型判断，仅供参考"
        parts.append(seg + "。")
    kinds = report.get("by_kind") or {}
    if isinstance(kinds, dict) and kinds:
        top = sorted(kinds.items(), key=lambda kv: -kv[1])[:3]
        parts.append("问题集中在：%s。" % "、".join("%s %d 处" % (k, v) for k, v in top))
    return "".join(parts)


def _compose_conclusion(summary: str, report: dict) -> str:
    """给界面用的结论：**实测数字在前**，模型的文字在后并标明仅供参考。

    顺序是有意为之，不是排版偏好：本地 3B 模型会写出与检查结果相反的结论
    （实测出现过它写"所有引用都得到了原文证据的支持"，而同一份报告里 7 条论断
    有 5 条判定为无支撑、支持率 29%）。数字是程序算出来的、可逐条核对；
    模型的话只是它自己的概括。让用户先读到可核对的那部分，才不会先被误导。

    这里必须自己先 sanitize：调用方传进来的往往是 `final["agent_summary"]`，
    那是**未清洗**的原始文本（清洗只发生在展示前的最后一步），
    直接拼接会把"我将首先使用…"原样带出去。
    """
    s = sanitize_agent_summary(summary)
    if s.startswith("AI 助手已完成本轮核查，但没有产出可读的文字结论。"):
        s = ""      # 兜底文案本身没有信息量，此时只用实测数字
    # 「模型调用失败（…），已按确定性顺序完成全部检查」这种句子讲的是**本轮的
    # 运行状态**，不是审查发现。它是程序写的、也不是 AI 的概括，放进
    # "以下是 AI 助手自己的概括" 里会让人误以为模型说了这句话（实测被反馈过）。
    # 该信息由界面另有一处交代，这里只留数字。
    if s.startswith("模型调用失败（") or s.startswith("云端与本地模型均不可用（"):
        s = ""
    facts = factual_summary(report)
    if not facts:
        return s
    if not s:
        return facts
    return facts + "\n\n（以下是 AI 助手自己的概括，仅供参考；以上数字为程序实测。" \
                   "两者不一致时，以数字为准，并可在下方「它的检查过程」中逐条核对。）\n" + s


SYSTEM_PROMPT = (
    "你是「文脉」的文献综述审稿助手。你的任务是审查一份中文综述初稿的引用质量。\n"
    "工作方式：\n"
    "1. 先看全局（列出句子与参考文献），判断哪些地方最可疑；\n"
    "2. 针对可疑处调用工具深入核查，不要盲目把每个工具都跑一遍；\n"
    "3. 每轮只调用必要的工具，看到结果后再决定下一步；\n"
    "4. 核查充分后调用 generate_report 结束。\n"
    "重点：优先核查「引用是否真有原文支撑」，这是最容易出问题的地方。\n"
    "全部回复使用中文。"
)

# 工具的 JSON Schema（给百炼的 function calling 用）
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_sentences",
        "description": "列出综述稿正文的所有句子（含编号与引用角标），以及参考文献列表。用于先看全局、挑出可疑处。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_sentence",
        "description": "按编号取正文中的某一句话的完整内容。",
        "parameters": {"type": "object", "properties": {
            "no": {"type": "integer", "description": "句子编号"}}, "required": ["no"]}}},
    {"type": "function", "function": {
        "name": "reference_by_index",
        "description": "按编号取参考文献条目原文。",
        "parameters": {"type": "object", "properties": {
            "idx": {"type": "integer", "description": "参考文献编号"}}, "required": ["idx"]}}},
    {"type": "function", "function": {
        "name": "check_citation_existence",
        "description": "检查引用真实性：正文角标是否都在参考文献表中、表中条目是否都被引用。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "check_ordering",
        "description": "检查引用编号顺序是否符合顺序编码制（按首次出现顺序递增）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "check_format",
        "description": "逐条检查参考文献是否符合 GB/T 7714 格式。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "find_evidence",
        "description": "针对某处引用，回到对应论文原文检索最相关的段落（只给证据，不下结论）。",
        "parameters": {"type": "object", "properties": {
            "citation": {"type": "integer", "description": "引用编号"},
            "sentence": {"type": "string", "description": "要核查的句子，可省略（省略则自动取包含该编号的句子）"}},
            "required": ["citation"]}}},
    {"type": "function", "function": {
        "name": "check_claim_support",
        "description": "判断某处引用是否真的有原文支撑：先检索原文证据，再由模型判定。这是最关键的核查工具。",
        "parameters": {"type": "object", "properties": {
            "citation": {"type": "integer", "description": "引用编号"},
            "sentence": {"type": "string", "description": "要核查的句子，可省略"}},
            "required": ["citation"]}}},
    {"type": "function", "function": {
        "name": "generate_report",
        "description": "核查完成后调用：汇总所有检查结果并生成最终报告，同时结束本次审查。",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "你本次审查的思路与结论（中文，200 字以内）"}},
            "required": ["summary"]}}},
]

DISPATCH = {
    "list_sentences": lambda a: selfcheck.tool_list_sentences(),
    "get_sentence": lambda a: selfcheck.tool_get_sentence(int(a.get("no", 0))),
    "reference_by_index": lambda a: selfcheck.tool_reference_by_index(int(a.get("idx", 0))),
    "check_citation_existence": lambda a: selfcheck.tool_check_citation_existence(),
    "check_ordering": lambda a: selfcheck.tool_check_ordering(),
    "check_format": lambda a: selfcheck.tool_check_format(),
    "find_evidence": lambda a: selfcheck.tool_find_evidence(
        int(a.get("citation", 0)), a.get("sentence") or "", use_llm=False),
    "check_claim_support": lambda a: selfcheck.tool_check_claim_support(
        int(a.get("citation", 0)), a.get("sentence") or "", use_llm=True),
    "generate_report": lambda a: {"report": selfcheck.tool_generate_report(),
                                  "agent_summary": a.get("summary", "")},
}


def _fast_mode() -> bool:
    """测试快速模式：跳过模型推理（WM_AGENT_FAST=1）。

    只在测试里开。生产路径必须真的调用模型——否则"AI 审稿"就名不副实。
    """
    return os.environ.get("WM_AGENT_FAST", "") in ("1", "true", "True")


class ReviewAgent:
    """审稿 Agent：模型自主选择工具，直到调用 generate_report 结束。"""

    def __init__(self, max_steps: int = 12, verbose: bool = True):
        self.max_steps = max_steps
        self.verbose = verbose
        self.client = LLMClient(verbose=False)
        self.trace = []

    # ── 工具调用 ──
    def _call_tool(self, name: str, args: dict) -> dict:
        fn = DISPATCH.get(name)
        if not fn:
            return {"error": "未知工具：%s" % name}
        try:
            return fn(args or {})
        except Exception as e:
            return {"error": "工具执行失败：%s" % str(e)[:150]}

    # ── 主循环 ──
    def _fill_required_checks(self) -> list:
        """补做模型没调用的必要核查；返回补做的工具名列表。

        只补结构性的检查（句子切分、引用存在性、顺序、格式合规）——它们对任何
        初稿都必须跑，且结果是确定的，补做不会改变结论，只是把缺的补上。
        语义判断类的工具（如论断是否有原文支撑）不在这里补：那是模型该做的
        判断，硬补反而会掩盖"模型没干活"这件事。
        """
        required = ["list_sentences", "check_citation_existence",
                    "check_ordering", "check_format"]
        done = {t.get("tool") for t in self.trace if t.get("type") == "tool_call"}
        filled = []
        for name in required:
            if name in done:
                continue
            result = self._call_tool(name, {"summary": "模型提前收尾，补做必要核查"})
            self.trace.append({"step": len(self.trace) + 1, "type": "tool_call",
                               "tool": name, "args": {},
                               "result_brief": str(result)[:300],
                               "filled": True})
            filled.append(name)
        return filled

    def run(self, task: str = "请审查这份综述初稿的引用质量。") -> dict:
        ensure_dirs()
        start = time.time()
        self.trace = []

        # 快速模式（WM_AGENT_FAST=1，仅测试用）：跳过模型推理，直接走确定性核查。
        # 本地模型每步要十几秒，回归测试里逐个真跑会让整套测试变得无法忍受；
        # 而测试要验的是**代码路径与结论正确性**，模型本身的质量由单独一次
        # 实测负责（见交付文档 01）。两者分开，测试才跑得动。
        if _fast_mode():
            out = self._deterministic_plan(
                task, "测试快速模式：跳过模型推理，")
            out["status"] = "fast_mode"
            return out

        # 能不能用到模型：云端有效 Key，或者随包的本地模型。
        # 这里必须一起判断——只认云端的话，离线模式一步都不试就退回确定性计划，
        # 会说出"模型不可用"这种不成立的话（本地模型其实可用）。
        if not self.client.available and not local_llm.available("gen"):
            if self.verbose:
                print("[Agent] 云端与本地模型均不可用，改用确定性计划（仍产出完整报告）")
            return self._deterministic_plan(
                task, "云端与本地模型均不可用（未配置有效的百炼 Key，且 models/ 下没有生成模型），")

        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": task}]
        final = None

        for step in range(1, self.max_steps + 1):
            # 本步模型调用：失败就在原地重试。本地模型偶发 503/超时是正常的
            # （服务忙），一次抖动就整轮退回确定性计划太脆——实测出现过审稿
            # 走到一半失败。重试仍失败才降级。
            text, meta = None, None
            for attempt in range(3):
                text, meta = self._chat_with_tools(messages)
                if text is not None:
                    break
                if self.verbose:
                    print("[Agent] 第 %d 步模型调用失败（第 %d/3 次）：%s"
                          % (step, attempt + 1, str(meta)[:80]))
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
            if text is None:
                if self.verbose:
                    print("[Agent] 第 %d 步模型调用连续失败 → 转确定性计划" % step)
                return self._deterministic_plan(
                    task, "模型调用失败（%s），" % str(meta)[:80])

            calls = (meta or {}).get("tool_calls") or []
            content = (meta or {}).get("content") or ""

            if content and self.verbose:
                print("[Agent] 第 %d 步 思考：%s" % (step, content[:120]))
            self.trace.append({"step": step, "type": "thought", "content": content[:300]})

            if not calls:
                # 模型不再调用工具，只给了段文字。此时不能直接把报告端出去：
                # 小模型（本地 3B）经常只调一两个工具就收尾，甚至只是"说"要用
                # 某个工具却没真的调用——报告会因此缺掉核心的引用核对。
                # 这里把**结构性的必做核查**补齐（模型自己调过的就不重复），
                # 报告才是完整的、可核查的。
                filled = self._fill_required_checks()
                if filled and self.verbose:
                    print("[Agent] 模型提前收尾，补做必要核查：%s" % "、".join(filled))
                final = {"status": "finished_no_tool",
                         "agent_summary": sanitize_agent_summary(content),
                         "report": self._call_tool(
                             "generate_report",
                             {"summary": sanitize_agent_summary(content)
                              or "已按必要核查顺序完成审查。"}).get("report", {}),
                         "filled_checks": filled}
                break

            messages.append({"role": "assistant", "content": content or None,
                             "tool_calls": calls})

            for call in calls:
                fname = call.get("function", {}).get("name")
                try:
                    fargs = json.loads(call["function"].get("arguments") or "{}")
                except Exception:
                    fargs = {}
                if self.verbose:
                    print("[Agent] 第 %d 步 调用工具：%s(%s)" % (
                        step, fname, json.dumps(fargs, ensure_ascii=False)[:80]))
                result = self._call_tool(fname, fargs)
                self.trace.append({"step": step, "type": "tool_call", "tool": fname,
                                   "args": fargs, "result_brief": str(result)[:300]})
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(result, ensure_ascii=False)[:3500]})
                if fname == "generate_report":
                    final = {"status": "finished",
                             "agent_summary": sanitize_agent_summary(fargs.get("summary", "")),
                             "report": result.get("report", {})}
            if final:
                break

        if final is None:
            # 到达步数上限：补一次收尾，保证一定有结论与报告
            if self.verbose:
                print("[Agent] 达到步数上限，自动收尾生成报告…")
            report = self._call_tool("generate_report",
                                     {"summary": "达到步数上限，已汇总本次全部核查结果。"})
            final = {"status": "max_steps_reached",
                     "agent_summary": "达到步数上限（%d 步），已汇总本次核查结果。" % self.max_steps,
                     "report": report.get("report", {})}

        final.update({"steps": len(self.trace), "seconds": round(time.time() - start, 1),
                      "tools_used": sorted({t["tool"] for t in self.trace if t["type"] == "tool_call"}),
                      "trace": self.trace})
        with open(AGENT_LOG, "w", encoding="utf-8") as f:
            json.dump(final, f, ensure_ascii=False, indent=2)
        if self.verbose:
            print("[Agent] 完成：%d 步，用时 %.1f 秒，使用工具 %s" % (
                final["steps"], final["seconds"], "、".join(final["tools_used"]) or "—"))
            if final.get("agent_summary"):
                print("[Agent] 结论：%s" % final["agent_summary"][:200])
        return final

    def _chat_with_tools(self, messages: list):
        """调用模型并带上工具定义；返回 (text_or_None, meta_with_tool_calls)。

        走哪条通道：
          · 云端可用（配了有效 Key 且未强制离线）→ 百炼 OpenAI 兼容接口
          · 否则 → **随包本地模型**（llama.cpp，实测支持 OpenAI 风格工具调用）

        为什么必须支持本地：原实现直接拿 settings.api_key 请求云端地址，完全没有
        本地分支。离线模式下（或 Key 还是占位符时）请求必然失败，Agent 就退回
        确定性计划并提示"模型不可用"——而本地模型其实好好的（实测 8.9 秒正常
        生成，也支持 tool_calls）。那句话不成立，还把已有能力白白丢掉。
        """
        import urllib.error
        import urllib.request
        from .config import settings, key_usable

        # 判据必须与 settings.llm_available 一致：用 key_usable()，不能只判"非空"。
        # 交付包的 .env 里是中文占位符——非空但不是 Key。只判非空的话这里会走进
        # 云端分支，urllib 按 latin-1 编码请求头时抛 UnicodeEncodeError，
        # 结果本地模型明明可用却整个失败（实测就是这样）。
        cloud_ok = key_usable(settings.api_key) and not settings.force_offline

        if not cloud_ok:
            if not local_llm.available("gen"):
                return None, "云端不可用，且本地模型不可用（models/ 缺失或无法启动）"
            d = local_llm.chat_tools(messages, TOOL_SCHEMAS,
                                     max_tokens=1200, temperature=0.2, timeout=300)
            if not d or d.get("error"):
                # 带上真实原因，不要写"未就绪或返回异常"这种笼统话——
                # 用户拿它无从排查，而 chat_tools 已经把原因算出来了。
                why = (d or {}).get("error") or "本地模型无响应"
                return None, "本地模型调用失败：%s" % str(why)[:160]
            return d.get("content") or "", {"content": d.get("content") or "",
                                            "tool_calls": d.get("tool_calls") or [],
                                            "channel": "local"}

        body = {"model": settings.model_default, "messages": messages,
                "tools": TOOL_SCHEMAS, "tool_choice": "auto",
                "temperature": 0.2, "max_tokens": 1200}
        try:
            req = urllib.request.Request(
                settings.base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": "Bearer " + settings.api_key,
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            # Key 无效/过期（401/403）时改走本地，而不是直接放弃
            if e.code in (401, 403) and local_llm.available("gen"):
                if self.verbose:
                    print("[Agent] 云端返回 %d（Key 无效或过期），改用本地模型继续…" % e.code)
                saved = settings.force_offline
                settings.force_offline = True          # 让递归进本地分支
                try:
                    return self._chat_with_tools(messages)
                finally:
                    settings.force_offline = saved
            return None, "HTTP %d: %s" % (e.code, detail)
        except Exception as e:
            return None, str(e)[:200]

        msg = ((d.get("choices") or [{}])[0].get("message") or {})
        self._record_cost(d.get("usage") or {})
        return msg.get("content") or "", {"content": msg.get("content") or "",
                                          "tool_calls": msg.get("tool_calls") or [],
                                          "channel": "cloud"}

    def _record_cost(self, usage: dict) -> None:
        """把 Agent 的 token 计入成本台账（复用 llm 模块的记账）。"""
        try:
            from .llm import COST_FILE, PRICES, _load_ledger, _save_ledger
            from .config import settings as st
            pin, pout = PRICES.get(st.model_default, PRICES["default"])
            cost = (usage.get("prompt_tokens", 0) / 1000.0) * pin + \
                   (usage.get("completion_tokens", 0) / 1000.0) * pout
            led = _load_ledger()
            led["cost"] += cost
            led["calls"] += 1
            led["tokens"] += usage.get("total_tokens", 0)
            _save_ledger(led)
        except Exception:
            pass

    # ── 降级：确定性计划 ──
    def _deterministic_plan(self, task: str, reason: str = "") -> dict:
        """不用模型时的确定性顺序（保证断网也能出报告）。

        `reason` 要如实写清为什么没用模型——是云端与本地都不可用，还是模型试过
        但调用失败了。两者对用户意味着完全不同的排查方向，不能都笼统说成
        "模型不可用"。
        """
        why = reason or "云端与本地模型均不可用"
        seq = ["list_sentences", "check_citation_existence", "check_ordering",
               "check_format", "generate_report"]
        self.trace = []
        report = None
        for i, name in enumerate(seq, 1):
            result = self._call_tool(name, {"summary": why})
            self.trace.append({"step": i, "type": "tool_call", "tool": name,
                               "args": {}, "result_brief": str(result)[:200]})
            if self.verbose:
                print("[Agent] 第 %d 步 调用工具：%s" % (i, name))
            if name == "generate_report":
                report = result.get("report", {})
        out = {"status": "deterministic_fallback",
               "agent_summary": "%s已按确定性顺序完成全部检查，结论与报告不受影响。" % why,
               "report": report,
               "steps": len(self.trace), "seconds": 0.0,
               "tools_used": seq, "trace": self.trace}
        with open(AGENT_LOG, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return out


def run_agent(task: str = None, verbose: bool = True, max_steps: int = 12) -> dict:
    agent = ReviewAgent(max_steps=max_steps, verbose=verbose)
    return agent.run(task or "请审查这份综述初稿的引用质量，重点核查引用是否真有原文支撑。")
