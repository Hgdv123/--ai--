"""文本处理：分词、清洗、句切分、关键词。纯本地，无网络依赖。"""
from __future__ import annotations

import re

try:
    import jieba  # 可选：装了中文分词更准
    _HAS_JIEBA = True
except Exception:
    _HAS_JIEBA = False

STOPWORDS_ZH = set("""
的 了 和 是 在 我 有 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
自己 这 那 它 他 她 我们 你们 他们 这个 那个 这些 那些 而 与 及 以及 或 或者 但 但是
因为 所以 如果 虽然 然而 因此 并且 通过 进行 可以 能够 已经 正在 将 被 把 对 从 向
为 以 之 其 该 等 各种 一些 许多 大量 本文 本 研究 方法 结果 结论 分析 提出 表明
显示 发现 使用 采用 基于 针对 关于 对于 由于 为了 同时 此外 另外 首先 其次 最后
""".split())

STOPWORDS_EN = set("""
a an the and or but if then than that this these those of in on at to for from by with as is
are was were be been being it its we our you your they their he she his her not no do does did
can could may might will would shall should have has had using used use based propose proposed
method methods result results show shows shown paper study approach model models however also
""".split())


def clean_text(text: str) -> str:
    """去页眉页脚噪声、连字符断行、多余空白。"""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"-\n(?=[a-z])", "", t)              # 英文断词连字符
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def split_sentences(text: str, min_len: int = 4) -> list:
    """中英文混排句切分。

    注意 min_len 默认 4：中文信息密度高，"第一句。" 仅 4 字却是完整句子；
    早期用 12 会把大量中文短句整个丢掉（实测短句全部被过滤）。
    """
    if not text:
        return []
    t = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z])", t)
    out = []
    for p in parts:
        p = p.strip()
        if len(p) >= min_len:
            out.append(p)
    return out


def tokenize(text: str) -> list:
    """分词：中文走 jieba（若有），英文按词切；过滤停用词与单字噪声。"""
    if not text:
        return []
    text = text.lower()
    tokens = []
    if _HAS_JIEBA:
        for w in jieba.cut(text):
            w = w.strip()
            if len(w) < 2 and not w.isdigit():
                continue
            if w in STOPWORDS_ZH or w in STOPWORDS_EN:
                continue
            if re.fullmatch(r"[\W_]+", w):
                continue
            tokens.append(w)
    else:
        # 退化方案：英文按词，中文按双字滑窗
        for m in re.finditer(r"[a-z][a-z0-9\-]{1,}", text):
            w = m.group(0)
            if w not in STOPWORDS_EN:
                tokens.append(w)
        for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            for i in range(len(run) - 1):
                w = run[i:i + 2]
                if w not in STOPWORDS_ZH:
                    tokens.append(w)
    return tokens


def keywords(text: str, top_k: int = 12) -> list:
    """词频法关键词（无需外部模型）。"""
    from collections import Counter
    toks = tokenize(text)
    if not toks:
        return []
    cnt = Counter(toks)
    return [w for w, _ in cnt.most_common(top_k)]


def char_ngrams(text: str, n: int = 3) -> list:
    """字符 n-gram：用于无需预训练模型的本地向量化兜底。"""
    t = re.sub(r"\s+", " ", (text or "").lower())
    t = re.sub(r"[^\w\u4e00-\u9fff ]+", "", t)
    if len(t) < n:
        return [t] if t else []
    return [t[i:i + n] for i in range(len(t) - n + 1)]


SECTION_PATTERNS = [
    # 中文期刊的标题常在字间加空格（"摘  要"、"引  言"、"参 考 文 献"），
    # 所以中文词一律写成"字\s*字"；否则整篇只能识别出极少数章节（实测踩到过）。
    # 摘要：PDF 页眉里的 "Apr. 2023" 会被 "abstract|摘\s*要" + [\s:：]* 误匹配
    #（"Apr" 前缀不匹配，但 "abstract" 会匹配到 "Abstracts" 之类）。
    # 这里用 (?![a-z]) 排除英文单词后缀，并要求中文"摘 要"后不能紧跟汉字。
    ("abstract", r"(?im)^\s*(?:abstract(?![a-z])|摘\s*要(?![^\s:：]))[\s:：]*"),
    ("introduction", r"(?im)^\s*(?:\d{1,2}\s*[\.、]?\s*)?(?:introduction|引\s*言|绪\s*论)\s*$"),
    ("related", r"(?im)^\s*(?:\d{1,2}\s*[\.、]?\s*)?(?:related\s+work|相\s*关\s*工\s*作|"
                r"文\s*献\s*综\s*述|研\s*究\s*现\s*状)\s*$"),
    ("method", r"(?im)^\s*(?:\d{1,2}\s*[\.、]?\s*)?(?:method(?:s|ology)?|approach|proposed\s+method|"
               r"方\s*法|模\s*型|算\s*法)\s*$"),
    ("experiment", r"(?im)^\s*(?:\d{1,2}\s*[\.、]?\s*)?(?:experiment(?:s)?|evaluation|results?|"
                   r"实\s*验|实\s*验\s*结\s*果|结\s*果\s*与\s*分\s*析)\s*$"),
    ("conclusion", r"(?im)^\s*(?:\d{1,2}\s*[\.、]?\s*)?(?:conclusion(?:s)?|discussion|"
                   r"结\s*论|结\s*论\s*与\s*展\s*望|总\s*结\s*与\s*展\s*望|讨\s*论)\s*$"),
    ("references", r"(?im)^\s*(?:references|bibliography|参\s*考\s*文\s*献)\s*[:：]?\s*$"),
]

LIMITATION_CUES = [
    r"(?i)\blimitation(?:s)?\b", r"(?i)\bfuture work\b",
    r"(?i)\b(?:our|the proposed) (?:method|approach|model)\b[^.]{0,80}\b(?:cannot|fails?|struggles?|is limited)\b",
    r"局限", r"不足之处", r"未来工作", r"有待进一步", r"尚未", r"未能",
]

# 套话/免责声明/资助信息：这些句子虽然命中线索，但不是"研究局限"，必须排除
BOILERPLATE_CUES = [
    r"(?i)\bviews? expressed\b", r"(?i)\bthose of the author",
    r"(?i)\bdoes not necessarily represent\b", r"(?i)\bendorsed by\b",
    r"(?i)\bfunding\b", r"(?i)\bgrant no", r"(?i)\backnowledg(?:e|ment)",
    r"(?i)\bthis work was supported\b", r"(?i)\b版权所有", r"(?i)\bdisclaimer\b",
    r"(?i)\bpermission to (?:make|reproduce)\b", r"(?i)\bcopyright\b",
    r"(?i)\bauthor\(s\)\b", r"(?i)\bU\.S\. Government\b",
]

# 真正的局限句应包含这些"研究动作"线索
LIMITATION_STRONG = [
    r"(?i)\b(?:has|have) (?:not|yet to) been\b",
    r"(?i)\bremain(?:s)? (?:an? )?(?:open|unclear|challenging|limited)\b",
    r"(?i)\b(?:future|further) (?:work|research|study)\b",
    r"(?i)\bwe (?:leave|defer)\b",
    r"(?i)\bis limited to\b",
    r"(?i)\bsuffer(?:s)? from\b",
    r"有待", r"尚需", r"未来(?:将|工作|研究)", r"局限", r"不足",
]


def is_boilerplate(sentence: str) -> bool:
    """判断是否为套话（免责声明/资助/版权），这类句子不能当作研究局限。"""
    for c in BOILERPLATE_CUES:
        if re.search(c, sentence):
            return True
    return False


_SECTION_HEAD_PREFIX = re.compile(
    r"^\s*(?:\d{1,2}(?:\s*\.\s*\d{1,2})?\s*[\.、]?\s*)?"
    r"(?:discussion and future work|future work|discussion|"
    r"conclusions?|limitations?|experiments?|methods?|"
    r"讨论与未来工作|未来工作|讨论|结论|局限|"
    # 中文小标题：数字编号后跟 2~20 个汉字，再接分隔空白
    #（覆盖"3.1 问题定义""2.1 基于度量学习的方法""4.2 对比实验结果"等任意写法）
    r"[\u4e00-\u9fff]{2,20})\s{1,}",
    re.I)


def strip_section_head(sentence: str) -> str:
    """去掉句首粘连的章节小标题，如 'Discussion and Future Work We introduced...'。"""
    return _SECTION_HEAD_PREFIX.sub("", sentence or "").strip()

CONTRIBUTION_CUES = [
    r"(?i)\bwe propose\b", r"(?i)\bwe present\b", r"(?i)\bthis paper (?:proposes|presents|introduces)\b",
    r"(?i)\bour contribution", r"本文提出", r"本文的主要贡献", r"提出了",
]
