"""综述初稿生成 + 参考文献自动编号。

关键约束（决定自检是否有意义）：
- 引用编号**由程序统一分配**，按正文首次出现顺序编号（顺序编码制）；
- 模型只能引用"给定卡片列表里已存在的编号"，**禁止新增编号**；
- 因此生成结果天然满足"正文角标 ↔ 文末条目"一一对应，
  自检环节再去验证这一点（并故意支持埋错测试）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

from . import gbt7714
from . import textutil
from .cards import load_cards
from .config import DRAFT_MD, OUT_DIR, ensure_dirs
from .llm import LLMClient
from .matrix import cluster_topics, load_matrix


def _card_to_ref(card: dict) -> dict:
    """转成参考文献条目所需的字段。

    注意：优先使用**可访问的网络地址**（DOI / OpenAlex 页面），
    而不是本地 pdf/xxx.pdf 路径 —— 本地路径既不是合法引用，也会触发格式校验问题。
    """
    doi = (card.get("doi") or "").strip()
    url = (card.get("source_url") or "").strip()
    if not url and doi:
        url = doi if doi.startswith("http") else "https://doi.org/" + doi.replace("https://doi.org/", "")
    if not url:
        oid = (card.get("openalex_id") or "").strip()
        if oid:
            url = oid
    return {
        "title": card.get("title"),
        "authors": card.get("authors"),
        "year": card.get("year"),
        "venue": card.get("venue"),
        "doi": doi or None,
        "pdf_url": url,
        "ref_type": gbt7714.guess_type(card.get("venue") or "", card.get("title") or ""),
    }


def renumber_by_citation_order(body: str, refs: list) -> tuple:
    """按正文**首次出现顺序**重排编号，保证符合顺序编码制。

    同时剔除**未被正文引用的条目** —— 参考文献表只收录真正引用过的文献，
    避免出现"文末有、正文无"的不一致（这一点由自检独立复核）。

    反过来，正文里**指向不存在条目**的编号也必须删掉：原来用的是
    `mapping.get(n, n)`，找不到就保留旧号，于是正文会留着 [4][5] 而
    参考文献表里没有——"正文引用编号必须都在表中"这条不变量就破了
    （实测 fuzz 抓到的正是这个）。编号是程序分配的，正文不该出现
    分配表之外的号，所以这里直接删除而不是保留。

    返回 (新正文, 新参考文献列表, 旧号→新号 映射)。
    """
    order = []
    for m in re.finditer(r"\[(\d{1,3})\]", body):
        n = int(m.group(1))
        if n not in order:
            order.append(n)
    if not order:
        return body, [], {}

    ref_by_idx = {r["index"]: r for r in refs}
    # 只保留正文确实引用到的条目
    used = [n for n in order if n in ref_by_idx]
    mapping = {old: new for new, old in enumerate(used, 1)}

    def _sub(m):
        old = int(m.group(1))
        if old in mapping:
            return "[%d]" % mapping[old]
        # 指向不存在的条目：整段编号删掉（连同紧跟其后的空格，避免留下空档）
        return ""

    new_body = re.sub(r"\[(\d{1,3})\](?=[ \t]?)", _sub, body)
    # 删除相邻编号后可能留下连续空格或行尾空格，清理一下
    new_body = re.sub(r"[ \t]{2,}", " ", new_body)
    new_body = re.sub(r"[ \t]+(\n|$)", r"\1", new_body)

    new_refs = []
    inv = {new: old for old, new in mapping.items()}
    for new in sorted(inv.keys()):
        r = ref_by_idx.get(inv[new])
        if not r:
            continue
        text = re.sub(r"^\[\d+\]\s*", "[%d] " % new, r["text"])
        new_refs.append({"index": new, "cite": "[%d]" % new, "text": text,
                         "title": r.get("title"), "year": r.get("year"),
                         "venue": r.get("venue"), "type": r.get("type")})
    return new_body, new_refs, mapping


def _cite_group(nums: list) -> str:
    """把连续编号合并成 [1-3] 形式，符合顺序编码制习惯写法。"""
    ns = sorted(set(int(n) for n in nums))
    if not ns:
        return ""
    groups, start, prev = [], ns[0], ns[0]
    for n in ns[1:]:
        if n == prev + 1:
            prev = n
            continue
        groups.append((start, prev))
        start = prev = n
    groups.append((start, prev))
    parts = []
    for a, b in groups:
        parts.append("[%d]" % a if a == b else ("[%d-%d]" % (a, b) if b > a + 1 else "[%d][%d]" % (a, b)))
    return "".join(parts)


def _clean_field(text: str, max_len: int = 90) -> str:
    """清洗卡片字段，避免把英文原文、章节标题、超长句子塞进中文模板。

    这是本地降级通道的质量闸门：宁可留空，也不要产出"采用then we present..."这类乱码。
    """
    if not text:
        return ""
    t = str(text).strip()
    # 去掉句首粘连的章节标题（如 "Methodology In this section, we..."）
    t = re.sub(r"^(?:abstract|introduction|method(?:s|ology)?|approach|related work|"
               r"experiment(?:s)?|conclusion(?:s)?|discussion|references|"
               r"摘要|引言|方法|实验|结果|结论|讨论|参考文献)\b[\s:：,，]*", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    # 过长说明抓错了段落（多半是整段正文），丢弃
    if len(t) > max_len:
        return ""
    # 含 LaTeX/公式/引用残留的丢弃
    if re.search(r"[{}]|\\[a-zA-Z]+|\$|\[\d+\]|\(\d+\)|§|et al\.", t):
        return ""
    # 英文占比过高（说明是原文而非中文概括）丢弃 —— 中文语境下不适合作正文
    cn = len(re.findall(r"[\u4e00-\u9fff]", t))
    en = len(re.findall(r"[A-Za-z]", t))
    if en and cn / (cn + en) < 0.25:
        return ""
    # 必须以句读结尾或本身够短，否则视为半截句
    if not t.endswith(("。", ".", "；", ";", "！", "？")) and len(t) > 40:
        return ""
    return t.strip("。.；; ")


def _local_paragraph(topic_name: str, cards: list, num_of: dict) -> str:
    """本地通道：模板化段落，句末带引用角标。

    严格清洗每个字段；没有可用内容的论文只保留标题与编号，不硬凑句子。
    """
    sents = []
    idxs = [num_of[id(c)] for c in cards]
    cite_all = _cite_group(idxs)
    sents.append("围绕「%s」方向，共纳入 %d 篇文献%s。" % (topic_name, len(cards), cite_all))

    for c in cards:
        f = c.get("fields", {})
        n = num_of[id(c)]
        title = re.sub(r"\s+", " ", (c.get("title") or "").strip())[:38]
        year = c.get("year") or ""
        method = _clean_field(f.get("method"))
        problem = _clean_field(f.get("problem"))

        if method:
            piece = "《%s》采用%s" % (title, method) if title else "有研究采用%s" % method
        elif problem:
            piece = "《%s》针对%s" % (title, problem) if title else "有研究针对%s" % problem
        else:
            # 拿不到中文概括时，只列标题与年份，不硬凑句子
            piece = "《%s》%s" % (title, ("（%s）" % year) if year else "")

        if piece:
            sents.append(piece.rstrip("。.") + "[%d]。" % n)

        metrics = [str(m) for m in (f.get("metrics") or [])
                   if m and not re.search(r"[{}]|\\[a-zA-Z]", str(m))]
        if metrics:
            sents.append("其报告的关键结果为%s[%d]。" % ("；".join(m[:50] for m in metrics[:2]), n))

    lims = []
    for c in cards:
        l = _clean_field(c.get("fields", {}).get("limitation"))
        if l:
            lims.append((c, l))
    if lims:
        parts = ["%s（《%s》）[%d]" % (l, (c.get("title") or "")[:22], num_of[id(c)])
                 for c, l in lims[:3]]
        sents.append("现有工作的局限主要集中在：%s。" % "；".join(parts))
    return "".join(sents)


LLM_SYSTEM = (
    "你是学术写作助手，负责起草文献综述的「研究现状」部分。"
    "硬性规则：只允许使用我提供的编号进行引用，格式为 [n]，绝对不能出现列表之外的编号；"
    "不得编造文献内容；每句话都要有引用支撑；语言为中文，客观、学术、不用第一人称。"
)

LLM_USER = """请基于以下文献卡片，起草「研究现状」综述段落。

要求：
1. 按主题组织，每个主题 1 段，共 {n_topics} 段左右；
2. 每句话末尾用 [编号] 标注来源，编号只能取自我给你的列表；
3. 段落首句、以及任何总结性/规律性陈述（如"这些工作共同表明…""总体来看…"）
   也必须带引用编号——它们同样是依赖文献的论断；
4. 最后用一段总结现有工作的共同局限（同样要带引用）；
5. 不要写参考文献列表（系统会自动生成）；
6. 只输出正文，不要标题以外的解释。

文献卡片：
{cards_block}
"""


def _llm_draft(topics: list, cards: list, num_of: dict, client: LLMClient,
               verbose: bool = False):
    blocks = []
    for c in cards:
        f = c.get("fields", {})
        blocks.append(
            "编号 [%d]\n标题：%s\n年份：%s\n解决的问题：%s\n方法：%s\n数据：%s\n结果：%s\n局限：%s"
            % (num_of[id(c)], (c.get("title") or "")[:100], c.get("year") or "",
               f.get("problem") or "—", f.get("method") or "—",
               "、".join(f.get("datasets") or []) or "—",
               "；".join(f.get("metrics") or []) or "—", f.get("limitation") or "—"))
    prompt = LLM_USER.format(n_topics=max(len(topics), 2), cards_block="\n\n".join(blocks))
    # 同一批文献、同一提示词要得到**同一份草稿**。
    # 原先用 temperature=0.3 采样，实测两次生成相似度只有 77.3%、字数 1554 vs 1716，
    # 于是被审对象每次都不一样，自检结果（句数、论断数、支持率）每次都变，
    # 用户合理地追问"为什么两次审查不一样"。
    # 只把温度设 0 还不够：实测 llama-server 在 temperature=0 下仍会采样
    # （前 80 字相同、之后分叉），必须同时固定 seed 才逐字节一致。
    # 需要多样本时用 WM_DRAFT_TEMPERATURE 覆盖温度（此时 seed 也一并放开）。
    draft_temp = float(os.environ.get("WM_DRAFT_TEMPERATURE", "0") or 0)
    draft_seed = None if draft_temp > 0 else int(os.environ.get("WM_DRAFT_SEED", "20260918"))
    text, meta = client.chat(
        [{"role": "system", "content": LLM_SYSTEM},
         {"role": "user", "content": prompt}],
        max_tokens=2400, temperature=draft_temp, verbose=verbose, seed=draft_seed)
    if not text:
        return None, meta
    allowed = set(num_of.values())
    used = set(int(x) for x in re.findall(r"\[(\d{1,3})\]", text))
    bad = used - allowed
    if bad:
        # 清理越界编号，保证自检能通过
        for b in bad:
            text = text.replace("[%d]" % b, "")
    text = _attach_citations_to_topic_sentences(text)
    # 统一引用格式：去掉角标前的空格（" [1]" → "[1]"）
    text = re.sub(r"\s+(\[\d{1,3}(?:\s*[-,–]\s*\d{1,3})*\])", r"\1", text)
    return text.strip(), meta


SYNTHESIS_CUES = re.compile(
    r"(?i)(共同表明|总体来看|综上所述|这些(?:工作|方法|研究)|纵观|整体而言|"
    r"collectively|taken together|overall|in summary)")

# 引用纠错用：从卡片字段里挖出的"指纹词"，用于反查某句话究竟在讲哪篇论文
_FP_STOP = set("""
方法 提出 一种 基于 通过 使用 采用 结果 数据 表明 验证 实现 训练 学习 模型 算法 网络
the and for with that this from are was were 一种 进行 可以 以及 并且 提升 提高 达到
""".split())

# 引用角标：单个或区间（[1]、[1-3]、[1,2]）
CITE_RE = re.compile(r"\[\d{1,3}(?:\s*[-,–]\s*\d{1,3})*\]")


def _fingerprints(card: dict) -> set:
    """从一张卡片提取可辨识的词（标题实词 + 方法/数据集/指标里的专名）。

    这些词是判断"这句话在说哪篇论文"的依据：模型复述某篇论文时，
    几乎必然带上该论文的专有名词（如 UMTRA、DFR、Bi-directional、Omniglot）。
    """
    f = card.get("fields", {}) or {}
    parts = [card.get("title") or ""]
    parts.append(str(f.get("method") or ""))
    for k in ("datasets", "metrics"):
        v = f.get(k)
        if isinstance(v, list):
            parts.append(" ".join(str(x) for x in v))
        elif v:
            parts.append(str(v))
    text = " ".join(parts)
    fps = set()
    # 英文专名（UMTRA、DFR、Omniglot、ImageNet…）。
    # 注意：不能用 \b —— \b 是 ASCII 词边界，而中文里专名常直接紧邻汉字
    # （如 "UMTRA算法"），\b 在此不成立，会把专名整个漏掉。改用显式断言。
    for w in re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9\-]{2,}(?![A-Za-z0-9])", text):
        if w.lower() not in _FP_STOP and len(w) >= 3:
            fps.add(w.lower())
    # 中文术语（3-8 字）。上限放到 8，才能保留"双向特征重建"这类完整术语。
    for w in re.findall(r"[\u4e00-\u9fff]{3,8}", text):
        if w not in _FP_STOP:
            fps.add(w)
    return fps


def _distinctive(fp: set) -> list:
    """去掉被其它更长词包含的碎片，并按长度排序（长词更能指认论文）。

    例：同时有"双向特征重建"与"双向特征"时，只保留前者。
    """
    words = sorted(fp, key=len, reverse=True)
    kept = []
    for w in words:
        if any(w != k and w in k for k in kept):
            continue
        kept.append(w)
    return kept


def _split_by_citation(text: str) -> list:
    """按引用角标切分文本，保留角标。

    为什么不用句号切：本地小模型经常漏掉句号，把两句粘成
    "…达到了75%的准确率[2] 则引入了…"，按句号切会得到一整块混合内容，
    指纹归属就乱了。**角标切换处就是论断边界**，按它切更可靠。
    """
    parts = re.split(r"(\[\d{1,3}(?:\s*[-,–]\s*\d{1,3})*\])", text or "")
    merged = []
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if i + 1 < len(parts):
            seg += parts[i + 1]
        merged.append(seg)
    return merged


def correct_citations(text: str, ordered: list, num_of: dict) -> tuple:
    """纠正模型输出里错配的引用编号。

    背景：本地小模型（3B）难以严格遵守"编号↔论文"的对应关系，实测会把
    A 论文的内容标成 B 论文的编号。这里用卡片指纹词反查每句话真正对应的
    论文，把编号改正；**只在证据明确时改动**（命中 4 字以上专有词且得分明显
    领先），否则保留原样交给自检环节判定，避免越改越错。

    返回 (纠正后的文本, 改动条数)。
    """
    if not text or not ordered:
        return text, 0
    fps = [(_distinctive(_fingerprints(c)), num_of[id(c)]) for c in ordered]
    fixed_count = 0
    out_lines = []
    for para in text.split("\n"):
        if not para.strip() or CITE_RE.search(para) is None:
            out_lines.append(para)
            continue
        new_pieces = []
        for piece in _split_by_citation(para):
            nums = [int(x) for x in re.findall(r"\[(\d{1,3})\]", piece)]
            if not nums or len(piece) < 20:
                new_pieces.append(piece)
                continue
            low = piece.lower()
            scores = []
            for fp, num in fps:
                # 加权：长词更能指认论文（"双向特征重建"远比 "cub" 有区分度）
                sc = 0
                max_len = 0
                hits = 0
                for w in fp:
                    if w in low:
                        hits += 1
                        sc += len(w)
                        max_len = max(max_len, len(w))
                if hits:
                    scores.append((sc, max_len, num))
            if not scores:
                new_pieces.append(piece)
                continue
            scores.sort(reverse=True)
            best_sc, best_max, best_num = scores[0]
            second_sc = scores[1][0] if len(scores) > 1 else 0
            # 该片**末尾的角标**才是它真正在标注的那篇论文
            tail = re.findall(r"\[(\d{1,3})\]", piece)
            tail_num = int(tail[-1]) if tail else None
            if tail_num is None:
                new_pieces.append(piece)
                continue
            # ⚠ 段的**开头**就是角标时，不纠正：
            # 那多半是"这篇论文提出了…"的段首标注（[1] 提出了…、[2] 则专注于…），
            # 是合法的中文综述写法，不是"引用错配"。
            # 实测这类被误改的比例极高——5 句里改错 4 句，把模型**原本正确**的
            # 编号改成了别的论文，用户看到的就是"研究现状与原文不符"。
            if re.match(r"^\s*\[\d{1,3}", piece):
                new_pieces.append(piece)
                continue
            # 判据：至少命中一个 **4 字以上的专有词**（如 UMTRA、DFR、Omniglot、
            # 双向特征重建），得分明显领先第二名，且**判定的论文与末尾角标不一致**。
            # 用"最长命中词长度"而不是累计分数做门槛，是因为累计分数会漏掉
            # 单个但极有辨识度的缩写。
            #
            # 另一个必要条件：**其余卡片一条指纹都没命中**。
            # 一句话里提多篇论文是合法的，此时"命中最多"不等于"角标所指"——
            # 只要别的卡片也有线索，就说明这句跨了多篇，改它必然出错。
            # 实测：讲 UMTRA 的句子因为也提到别的词，角标从正确的 [2] 被改成 [1]。
            if (best_max >= 4 and best_sc > second_sc * 1.5
                    and len(scores) == 1 and best_num != tail_num):
                # 只改末尾那个角标：它是模型为这一句标注的来源
                idx = piece.rfind("[%d]" % tail_num)
                if idx >= 0:
                    piece = piece[:idx] + "[%d]" % best_num + piece[idx + len("[%d]" % tail_num):]
                    fixed_count += 1
                new_pieces.append(piece)
            else:
                new_pieces.append(piece)
        line = "".join(new_pieces)
        # 去掉紧邻的重复角标（"[1][1]" → "[1]"）：模型偶尔会重复标注
        line = re.sub(r"(\[\d{1,3}(?:\s*[-,–]\s*\d{1,3})*\])(?:\s*\1)+", r"\1", line)
        out_lines.append(line)
    return "\n".join(out_lines), fixed_count


def _attach_citations_to_topic_sentences(text: str) -> str:
    """给"引入句/总结句"补上该段已出现的引用编号。

    依据：这两类句子都是在**概括本段已引用文献**的论断，其引用来源就是同段内
    已被标注的编号集合——因此补引用有据可依，不是凭空添加。
    若不补，引用自检会报"论断有据"，而该问题对模型而言较难稳定自愈。
    """
    out = []
    for para in text.split("\n"):
        p = para.strip()
        if not p:
            out.append(para)
            continue
        all_nums = []
        for n in re.findall(r"\[(\d{1,3})\]", p):
            if n not in all_nums:
                all_nums.append(n)

        # 切句（保留标点）
        pieces = re.findall(r"[^。！？]*[。！？]|[^。！？]+$", p)
        pieces = [x for x in pieces if x.strip()]
        fixed = []
        for k, s in enumerate(pieces):
            need = False
            if not re.search(r"\[\d{1,3}", s):
                if k == 0:                       # 段落首句（主题引入句）
                    need = True
                elif SYNTHESIS_CUES.search(s):   # 总结性/规律性陈述
                    need = True
            if need and all_nums:
                m = re.match(r"^(.*?)([。！？])$", s.strip())
                if m:
                    s = m.group(1) + _cite_group(all_nums[:5]) + m.group(2)
                else:
                    s = s.rstrip() + _cite_group(all_nums[:5])
            fixed.append(s)
        out.append("".join(fixed))
    return "\n".join(out)


def _groundedness(body: str, cards: list, min_ratio: float = 0.35) -> tuple:
    """统计正文里的句子有多少能在文献卡片中找到语言依据。

    为什么需要它：本地部署的小模型（随包 3B）生成的段落常常看似通顺、
    实则与原文不符——实测支持率只有 **14.3%**（7 条论断仅 1 条有原文支撑），
    而云端同一任务是 60%。更糟的是它把 A 论文的内容标成 B 的编号，
    一次就纠正了 5 处。
    对用户来说，"一份读起来像模像样、实际大部分无据"的稿子比"一份老实的
    论文清单"危险得多——前者会被直接采用。所以这里给出客观的可验证性，
    低于阈值就**退回逐字取卡片的模板**，并如实说明原因。

    判据：句子里出现卡片文本的 3 字连续片段（中文里 3 字重合已属罕见），
    占比达到 min_ratio 即视为有依据。
    """
    sents = [s for s in textutil.split_sentences(body or "") if len(s) >= 12]
    if not sents:
        return 0.0, 0, 0
    blob = "".join((c.get("fields", {}).get("method") or "") +
                   (c.get("fields", {}).get("problem") or "") +
                   (c.get("fields", {}).get("limitation") or "") +
                   (c.get("fields", {}).get("takeaway") or "") +
                   "".join(c.get("fields", {}).get("metrics") or []) +
                   "".join(c.get("fields", {}).get("datasets") or []) +
                   (c.get("title") or "") for c in cards)
    grams = set()
    for i in range(len(blob) - 2):
        g = blob[i:i + 3]
        if re.search(r"[\u4e00-\u9fff]{3}", g):
            grams.add(g)
    if not grams:
        return 1.0, len(sents), len(sents)
    ok = 0
    for s in sents:
        clean = re.sub(r"\[\d{1,3}\]", "", s.replace(" ", ""))
        hit = sum(1 for i in range(len(clean) - 2) if clean[i:i + 3] in grams)
        if hit >= max(2, int(len(clean) * 0.06)):
            ok += 1
    return (ok / len(sents)), ok, len(sents)


def generate(use_llm: bool = None, verbose: bool = True) -> dict:
    """生成综述草稿，返回 {markdown, citations, refs, meta}。"""
    ensure_dirs()
    cards_data = load_cards()
    cards = [c for c in cards_data.get("cards", []) if not c.get("llm_note", "").startswith("模型通道不可用")]
    if not cards:
        cards = cards_data.get("cards", [])
    if not cards:
        raise RuntimeError("没有文献卡片，请先执行 build-cards")

    # 1) 聚类主题
    topics = cluster_topics(cards, top_k=3)
    if not topics:
        topics = [{"name": "总体", "members": list(range(len(cards)))}]

    # 2) 分配编号：按主题顺序 + 主题内被引数降序，得到正文出现顺序
    ordered = []
    topic_cards = []
    for t in topics:
        members = [cards[i] for i in t["members"] if i < len(cards)]
        members.sort(key=lambda c: -(c.get("cited_by_count") or 0))
        topic_cards.append((t["name"], members))
        for c in members:
            if c not in ordered:
                ordered.append(c)
    for c in cards:
        if c not in ordered:
            ordered.append(c)

    num_of = {id(c): i for i, c in enumerate(ordered, 1)}

    # 3) 生成正文
    client = LLMClient() if (use_llm is None or use_llm) else None
    llm_used, llm_note = False, ""
    # 未走模型时的原因标记（供界面区分文案）：
    #   ""           没降级
    #   "offline"    确实没配 Key / 关了模型通道
    #   "model_failed" 调用了模型但失败
    #   "ungrounded" 模型有输出、但没通过"与文献对得上"的一致性检查
    fallback_reason = ""
    body = None
    if client and client.available:
        if verbose:
            # 这一步只有**一次**云调用，期间不打字就会让用户以为程序卡死
            # （实测被当成"卡在这里了"）。把"要做什么、大概多久"先说清楚。
            print("[草稿] 已按主题归并 %d 张卡片，正在请模型起草正文"
                  "（一次调用，通常 10~30 秒）…" % len(ordered))
            sys.stdout.flush()
        body, meta = _llm_draft(topics, ordered, num_of, client, verbose=verbose)
        if body:
            llm_used = True
            llm_note = "模型通道：已使用（编号由系统分配）"
            # 本地小模型容易搞错"编号↔论文"的对应关系（实测会把 A 论文的内容
            # 标成 B 论文的编号），用卡片指纹词反查并纠正
            body, n_fixed = correct_citations(body, ordered, num_of)
            if n_fixed:
                llm_note += "；已自动纠正 %d 处引用错配" % n_fixed
            # 可验证性检查：句子在卡片里找不到语言依据的比例过高时，
            # 这份稿子"看起来像综述、实际大半无据"，退回模板更负责任。
            ratio, ok_n, tot_n = _groundedness(body, ordered)
            if tot_n and ratio < 0.35:
                if verbose:
                    print("[草稿] 模型输出与所给文献对不上（%d/%d 句有依据，%.0f%%），"
                          "已改用逐字取卡片的模板生成。" % (ok_n, tot_n, ratio * 100))
                body = None
                llm_used = False
                # 这是"模型调用了、但输出没通过一致性检查"，**不是**离线降级。
                # 两种情况必须分开标记：界面上原先把它们混在一起，只要说明里出现
                # "本地"二字就显示"本次用的是离线模式/不会调用大模型"——那是假的，
                # 实测让人以为 Key 没生效（截图反馈过）。
                fallback_reason = "ungrounded"
                llm_note = ("云端/模型确实生成了正文，但其中 %d/%d 句在所给文献卡片里"
                            "找不到语言依据（低于 35%% 阈值），为避免出现"
                            "\"读起来像综述、实际无据\"的稿子，已改用逐字取自卡片的模板。"
                            % (ok_n, tot_n))
            elif verbose:
                print("[草稿] 模型输出可验证性：%d/%d 句有依据（%.0f%%）"
                      % (ok_n, tot_n, ratio * 100))
        else:
            fallback_reason = "model_failed"
            llm_note = "模型通道不可用，已切本地模板：%s" % str(meta)[:70]
    else:
        fallback_reason = "offline"
        llm_note = "模型通道关闭，使用本地模板生成"

    if not body:
        paras = []
        for name, members in topic_cards:
            if members:
                paras.append(_local_paragraph(name, members, num_of))
        body = "\n\n".join(paras)

    # 4) 参考文献（严格按编号顺序）
    refs = gbt7714.build_reference_list([_card_to_ref(c) for c in ordered])

    # 5) 按正文首次出现顺序重排编号（保证顺序编码制）
    body, refs, renum = renumber_by_citation_order(body, refs)

    md_lines = ["# 研究现状", "", body, "", "## 参考文献", ""]
    md_lines += [r["text"] for r in refs]
    markdown = "\n".join(md_lines)

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "llm_used": llm_used, "llm_note": llm_note,
        "fallback_reason": fallback_reason,
        "topics": [t["name"] for t in topics],
        "renumber_map": renum,
        # 生成时实际纳入的论文篇数（= 文献卡片数）。用于判断结果是否已过期：
        # 不能拿 refs 条数比，因为未被正文引用的条目会被剔出参考文献表。
        "source_papers": len(cards),
        "citations": [{"index": r["index"], "title": r.get("title"), "year": r.get("year"),
                       "ref": r["text"]} for r in refs],
        "refs": refs, "markdown": markdown,
    }
    with open(DRAFT_MD, "w", encoding="utf-8") as f:
        f.write(markdown)
    with open(os.path.join(OUT_DIR, "draft.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if verbose:
        print("[草稿] 主题 %s；引用 %d 条；通道：%s" % (
            "、".join(out["topics"]), len(refs), "模型" if llm_used else "本地"))
        print("[草稿] 已写出：%s" % DRAFT_MD)
    return out


def load_draft() -> dict:
    p = os.path.join(OUT_DIR, "draft.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)
