"""文献卡片：把一篇论文抽成结构化字段。

双通道设计（关键）：
- 本地通道：规则 + 句式定位，**始终可用、不联网、零成本**；
- 模型通道：百炼 JSON 抽取，**有 Key 且联网时自动增强**，失败即回落本地结果。

字段：解决什么问题 / 方法 / 数据 / 指标 / 局限 / 可借鉴点。
每个字段带来源与置信度，便于评测与自检。
"""
from __future__ import annotations

import json
import os
import re
import time

from . import textutil
from .config import CARDS_JSON
from .literature import load_docs
from .llm import LLMClient

# ────────────────────────── 本地抽取规则 ──────────────────────────

METRIC_PAT = re.compile(
    r"((?:accuracy|precision|recall|f1|map|auc|bleu|rouge|psnr|ssim|top-?1|top-?5)"
    r"[^.\n]{0,40}?\d{1,3}(?:\.\d+)?\s*%"
    r"|\d{1,3}(?:\.\d+)?\s*%\s*(?:accuracy|precision|recall|f1|of\s+the\s+baseline)?"
    r"|(?:准确率|精确率|召回率|F1|平均精度)[^。\n]{0,30}?\d{1,3}(?:\.\d+)?\s*%)",
    re.I)

DATASET_PAT = re.compile(
    r"(\b(?:[A-Z][A-Za-z0-9\-]*(?:\s+[A-Z][A-Za-z0-9\-]*){0,3})\s*"
    r"(?:dataset|benchmark|corpus)\b"
    r"|(?:datasets?|benchmarks?)\s*(?:named|called|:)?\s*([A-Z][A-Za-z0-9\-]{2,}(?:\s*,\s*[A-Z][A-Za-z0-9\-]{2,})*)"
    r"|(?:数据集|基准)[:：]?\s*([A-Z][A-Za-z0-9\-]{2,})"
    r"|\b(MiniImageNet|ImageNet|CIFAR-?10|CIFAR-?100|COCO|MNIST|CUB-?200|Omniglot|"
    r"TieredImageNet|SVHN|Pascal\s*VOC|GLUE|SQuAD|MS-?MARCO|WikiText)\b)", re.I)

PROBLEM_CUES = [
    r"(?i)\b(?:problem|challenge|issue|difficult(?:y|ies))\b",
    r"(?i)\b(?:suffer(?:s)? from|struggle(?:s)? with|limited by)\b",
    r"(?:问题|挑战|难点|瓶颈|受限于|难以)",
]
METHOD_CUES = [
    r"(?i)\bwe (?:propose|present|introduce|develop|design)\b",
    r"(?i)\b(?:our|the proposed) (?:method|approach|framework|model|network)\b",
    r"(?:本文提出|我们提出|提出了|采用|基于)",
]
TAKEAWAY_CUES = [
    r"(?i)\b(?:can be (?:applied|adapted|extended)|inspir(?:es|ed)|generaliz(?:es|able))\b",
    r"(?i)\b(?:useful|applicable) (?:for|to)\b",
    r"(?:可借鉴|可迁移|可用于|启发|适用于)",
]

MAX_SENT = 300


def _pick(text: str, cues: list, top_k: int = 1) -> tuple:
    """按线索挑句。返回 (句子, 命中线索) —— 空则返回 ("", None)。"""
    for s in textutil.split_sentences(text):
        if len(s) > MAX_SENT or len(s) < 15:
            continue
        for c in cues:
            if re.search(c, s):
                return s.strip(), c
    return "", None


def _metrics(text: str, top_k: int = 3) -> list:
    out, seen = [], set()
    for m in METRIC_PAT.finditer(text[:12000]):
        v = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(v) < 6 or v.lower() in seen:
            continue
        seen.add(v.lower())
        out.append(v)
        if len(out) >= top_k:
            break
    return out


def _datasets(text: str, top_k: int = 4) -> list:
    out, seen = [], set()
    for m in DATASET_PAT.finditer(text[:12000]):
        v = next((g for g in m.groups() if g), "").strip(" ,.:;")
        v = re.sub(r"(?i)\s*(dataset|benchmark|corpus|数据集|基准)$", "", v).strip()
        if len(v) < 3 or v.lower() in seen:
            continue
        seen.add(v.lower())
        out.append(v)
        if len(out) >= top_k:
            break
    return out


def extract_local(doc: dict) -> dict:
    """本地规则抽取 —— 绝不失败。"""
    abstract = doc.get("abstract_pdf") or doc.get("abstract") or ""
    method_text = doc.get("method_text") or ""
    experiment_text = doc.get("experiment_text") or ""
    conclusion_text = doc.get("conclusion_text") or ""
    full = doc.get("full_text") or ""
    method_body = method_text or abstract

    problem, p_cue = _pick(abstract + "\n" + (doc.get("related_text") or ""), PROBLEM_CUES)
    if not problem:
        sents = textutil.split_sentences(abstract)
        problem = sents[0] if sents else ""

    method, m_cue = _pick(method_body, METHOD_CUES)
    if not method:
        method, m_cue = _pick(abstract, METHOD_CUES)

    takeaway, t_cue = _pick(full[:15000], TAKEAWAY_CUES)

    lims = doc.get("limitation_sentences") or []
    limitation = ""
    for s in lims:
        s = textutil.strip_section_head(s)
        if not textutil.is_boilerplate(s) and len(s) > 15:
            limitation = s
            break

    metrics = _metrics(experiment_text + "\n" + abstract)
    datasets = _datasets(abstract + "\n" + experiment_text)

    # 统一清洗句首粘连的章节小标题。
    # 中文论文的 method_text 常以"3.1 问题定义 小样本分类任务…"开头，
    # 不洗掉会把小节标题当成方法内容（实测踩到过）。
    def _clean(v):
        if isinstance(v, list):
            return [textutil.strip_section_head(str(x)) for x in v]
        return textutil.strip_section_head(v or "")

    fields = {
        "problem": _clean(problem),
        "method": _clean(method),
        "datasets": datasets,
        "metrics": metrics,
        "limitation": limitation,
        "takeaway": _clean(takeaway),
    }
    # 置信度：有明确线索命中 + 有实质内容
    conf = {
        "problem": 0.85 if (problem and p_cue) else (0.55 if problem else 0.0),
        "method": 0.9 if (method and m_cue) else (0.6 if method else 0.0),
        "datasets": 0.8 if datasets else 0.0,
        "metrics": 0.8 if metrics else 0.0,
        "limitation": 0.7 if limitation else 0.0,
        "takeaway": 0.6 if takeaway else 0.0,
    }
    return {"fields": fields, "confidence": conf, "source": "local"}


# ────────────────────────── 模型增强 ──────────────────────────

SYSTEM_PROMPT = (
    "你是学术文献信息抽取助手。只输出 JSON，不要任何解释。"
    "所有内容必须来自给定文本，找不到就填 null，禁止推测或编造。"
)

USER_TEMPLATE = """从下面这篇论文的片段中抽取信息，输出严格 JSON：

{{
  "problem": "该论文要解决的核心问题（一句话，中文，不超过 60 字）",
  "method": "采用的方法/技术路线（一句话，中文，不超过 60 字）",
  "datasets": ["用到的数据集名称，最多 4 个"],
  "metrics": ["关键结果指标，含数值，最多 3 条"],
  "limitation": "作者自述的研究局限（中文，不超过 60 字）",
  "takeaway": "可借鉴之处（中文，不超过 50 字，找不到填 null）"
}}

关于 limitation 的严格要求：
- 必须是**研究者对自己方法局限**的说明（如泛化性不足、未验证某场景、依赖先验假设、计算开销大）；
- **禁止**把资助信息、免责声明、版权说明、作者单位、致谢当作局限；
- 找不到就填 null，不要编造。

论文标题：{title}
发表年份：{year}

摘要：
{abstract}

方法片段：
{method}

结论片段：
{conclusion}
"""


def enhance_with_llm(card: dict, doc: dict, client: LLMClient) -> dict:
    """模型通道增强。失败时原样返回本地结果。"""
    if not client or not client.available:
        card["llm_note"] = "未启用模型通道"
        return card
    prompt = USER_TEMPLATE.format(
        title=(doc.get("title") or "")[:200],
        year=doc.get("year") or "",
        abstract=(doc.get("abstract_pdf") or doc.get("abstract") or "")[:2600],
        method=(doc.get("method_text") or "")[:1800],
        conclusion=(doc.get("conclusion_text") or "")[:1500])
    data, meta = client.chat_json(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
        model=None, max_tokens=900)
    if not data:
        card["llm_note"] = "模型通道不可用，已用本地结果：%s" % str(meta)[:80]
        return card

    f = card["fields"]
    conf = card["confidence"]
    for k in ("problem", "method", "limitation", "takeaway"):
        v = data.get(k)
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none", "无"):
            f[k] = v.strip()
            conf[k] = 0.95
    for k in ("datasets", "metrics"):
        v = data.get(k)
        if isinstance(v, list) and v:
            f[k] = [str(x).strip() for x in v if str(x).strip()][:4]
            conf[k] = 0.9
    card["source"] = "local+llm"
    card["llm_note"] = "已用模型增强"
    # meta 可能是 dict（正常）或字符串（错误原因），两种都要容忍
    if isinstance(meta, dict):
        card["llm_meta"] = {kk: meta.get(kk) for kk in ("model", "cached", "tokens")}
        if meta.get("local"):
            card["llm_note"] = "已用本地部署模型增强"
    elif meta:
        card["llm_meta"] = {"note": str(meta)[:80]}
    return card


# ────────────────────────── 批量 ──────────────────────────

def build_cards(use_llm: bool = None, limit: int = None, verbose: bool = True) -> dict:
    docs_data = load_docs()
    docs = docs_data.get("docs", [])
    if limit:
        docs = docs[:limit]
    if use_llm is None:
        use_llm = True

    client = LLMClient(verbose=False) if use_llm else None
    llm_on = bool(client and client.available)
    if verbose:
        print("[卡片] 共 %d 篇；模型通道：%s" % (len(docs), "开启" if llm_on else "关闭（本地抽取）"))

    todo = [(i, d) for i, d in enumerate(docs, 1) if not d.get("parse_error")]

    def make_card(item):
        i, doc = item
        card = extract_local(doc)
        if llm_on:
            card = enhance_with_llm(card, doc, client)
        card.update({
            "index": i,
            "title": doc.get("title"),
            "year": doc.get("year"),
            "venue": doc.get("venue"),
            "authors": doc.get("authors"),
            "cited_by_count": doc.get("cited_by_count"),
            "pdf": doc.get("local_pdf"),
            # 用于生成合法的 GB/T 7714 条目：优先网络地址，而非本地路径
            "doi": doc.get("doi"),
            "source_url": doc.get("pdf_url"),
            "openalex_id": doc.get("id"),
        })
        return card

    # 逐篇抽取是纯独立任务，可并发。
    # 实测：本地模型下 4 篇并发比串行快 100%（llama-server 多槽位批处理），
    # 且输出内容不受影响（每个请求各自独立采样）。
    # 云端调用也受益（并发请求），但为避免触发限流，云端并发度取小一些。
    workers = 4 if llm_on else 1
    if llm_on and not os.environ.get("WM_OFFLINE"):
        from .config import settings
        if settings.llm_available:
            workers = int(os.environ.get("WM_CARD_WORKERS", "3"))
    if workers > 1 and len(todo) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            cards = list(ex.map(make_card, todo))
    else:
        cards = [make_card(t) for t in todo]

    if verbose:
        for card in cards:
            print("   [%d] %s" % (card["index"], (card.get("title") or "")[:44]))
            print("       方法：%s" % ((card.get("fields") or {}).get("method") or "—")[:60])
            print("       来源：%s" % card.get("source"))

    out = {"count": len(cards), "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "llm_enabled": llm_on, "cards": cards}
    with open(CARDS_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    if verbose:
        print("[卡片] 完成：%d 张 → %s" % (len(cards), CARDS_JSON))
    return out


def load_cards() -> dict:
    if not os.path.isfile(CARDS_JSON):
        return {"count": 0, "cards": []}
    with open(CARDS_JSON, encoding="utf-8") as f:
        return json.load(f)
