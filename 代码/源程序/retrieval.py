"""本地检索引擎：BM25 + 字符 n-gram 向量（哈希 TF-IDF）+ 融合排序。

全部本地计算，无网络依赖，无预训练模型下载。
"""
from __future__ import annotations

import math
import re
import zlib
from collections import Counter

from . import textutil


def _stable_bucket(s: str, dim: int) -> int:
    """稳定哈希：不依赖 PYTHONHASHSEED，保证同一文本每次运行落到同一维度。

    直接用内置 hash() 会让"相似度"在每次进程重启后变化，指标无法复现。
    """
    return zlib.crc32(s.encode("utf-8")) % dim


class BM25:
    """标准 BM25（k1=1.5, b=0.75），自行实现以免依赖外部包。"""

    def __init__(self, corpus_tokens: list, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = corpus_tokens
        self.n = len(corpus_tokens)
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avg_len = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in corpus_tokens]
        df = Counter()
        for tf in self.tf:
            for term in tf:
                df[term] += 1
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def score(self, query_tokens: list, idx: int) -> float:
        tf, idf, dl = self.tf[idx], self.idf, self.doc_len[idx]
        s = 0.0
        for t in query_tokens:
            f = tf.get(t)
            if not f:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
            s += idf.get(t, 0.0) * f * (self.k1 + 1) / (denom or 1)
        return s

    def search(self, query_tokens: list, top_k: int = 10) -> list:
        scored = [(self.score(query_tokens, i), i) for i in range(self.n)]
        scored.sort(reverse=True)
        return [(i, s) for s, i in scored[:top_k] if s > 0]


class VectorIndex:
    """字符 n-gram 哈希 + TF-IDF 加权的本地向量索引（余弦相似度）。"""

    def __init__(self, docs_text: list, dim: int = 4096, ngram: int = 3):
        self.dim, self.ngram = dim, ngram
        self.vectors = []
        raw = []
        df = Counter()
        for t in docs_text:
            grams = Counter(textutil.char_ngrams(t, ngram))
            raw.append(grams)
            for g in grams:
                df[g] += 1
        n = max(len(docs_text), 1)
        for grams in raw:
            vec = [0.0] * dim
            for g, c in grams.items():
                idf = math.log(1 + n / (1 + df[g]))
                vec[_stable_bucket(g, dim)] += (1 + math.log(c)) * idf
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            self.vectors.append([v / norm for v in vec])

    def search(self, query: str, top_k: int = 10) -> list:
        grams = Counter(textutil.char_ngrams(query, self.ngram))
        if not grams:
            return []
        qv = [0.0] * self.dim
        for g, c in grams.items():
            qv[_stable_bucket(g, self.dim)] += 1 + math.log(c)
        norm = math.sqrt(sum(v * v for v in qv)) or 1.0
        qv = [v / norm for v in qv]
        scored = []
        for i, dv in enumerate(self.vectors):
            dot = 0.0
            for a, b in zip(qv, dv):
                if a and b:
                    dot += a * b
            scored.append((dot, i))
        scored.sort(reverse=True)
        return [(i, s) for s, i in scored[:top_k] if s > 0]


class SemIndex:
    """语义向量索引：优先用百炼 embedding（跨语言/跨表述匹配语义），本地 n-gram 兜底。"""

    def __init__(self, texts: list, verbose: bool = False):
        from .embeddings import Embedder, cosine
        self.texts = texts
        self._cosine = cosine
        self.embedder = Embedder(verbose=verbose)
        self.vectors = self.embedder.encode(texts) if texts else []

    def search(self, query: str, top_k: int = 10) -> list:
        if not self.texts or not query:
            return []
        qv = self.embedder.encode([query])[0]
        scored = [(self._cosine(qv, dv), i) for i, dv in enumerate(self.vectors)]
        scored.sort(reverse=True)
        return [(i, s) for s, i in scored[:top_k] if s > 0]


class HybridRetriever:
    """BM25 ∪ 语义向量 → RRF 融合。索引在内存中构建，全量论文约 1–3 秒。"""

    def __init__(self, docs: list, text_fields=("title", "abstract_pdf", "full_text"),
                 verbose: bool = False):
        self.docs = docs
        self.texts = []
        for d in docs:
            parts = []
            for f in text_fields:
                v = d.get(f) or ""
                parts.append(v if f != "full_text" else v[:200000])
            self.texts.append("\n".join(parts))
        self.tokens = [textutil.tokenize(t) for t in self.texts]
        self.bm25 = BM25(self.tokens)
        self.vector = SemIndex(self.texts, verbose=verbose)

    def search(self, query: str, top_k: int = 10, k_rrf: int = 60) -> list:
        """返回 [(doc_index, 融合分, 明细)]，按融合分降序。"""
        q_tokens = textutil.tokenize(query)
        bm_hits = self.bm25.search(q_tokens, top_k=top_k * 2)
        vc_hits = self.vector.search(query, top_k=top_k * 2)
        fused = {}
        for rank, (i, s) in enumerate(bm_hits):
            fused.setdefault(i, {"rrf": 0.0, "bm25": 0.0, "vec": 0.0})
            fused[i]["rrf"] += 1.0 / (k_rrf + rank + 1)
            fused[i]["bm25"] = s
        for rank, (i, s) in enumerate(vc_hits):
            fused.setdefault(i, {"rrf": 0.0, "bm25": 0.0, "vec": 0.0})
            fused[i]["rrf"] += 1.0 / (k_rrf + rank + 1)
            fused[i]["vec"] = s
        out = sorted(fused.items(), key=lambda kv: -kv[1]["rrf"])
        return [(i, v["rrf"], v) for i, v in out[:top_k]]


# ────────────────────────── RAG 的证据检索 ──────────────────────────

def split_for_index(text: str, max_chars: int = 1200, min_chars: int = 60) -> list:
    """把论文全文切成适合检索的段落块（只返回文本）。"""
    return [c["text"] for c in chunk_spans(text, max_chars=max_chars, min_chars=min_chars)]


def chunk_spans(text: str, max_chars: int = 1200, min_chars: int = 60) -> list:
    """切块并保留每块在原文里的位置 → [{text, start, end}]。

    start/end 是**页码换算用的锚点**，用户要能照着页码翻回原文自己看。
    定位在"连续空白折叠成一个空格"的归一化文本上进行（切分本身做的是同样的
    折叠，所以两边对得上）；直接按字符 1:1 替换空白会因长度不同而定位失败。

    两条不变量（由 .dsh-tmp/check_chunkparity.py 与 check_equivalence.py 守住）：
      · 块序列与切分本体 _chunk_text 逐块一致 —— 一块不丢、一块不多；
      · 定位不到的块**照样返回**，只是 start/end 为 None（给不出页码）。
    切分口径本身没有被改动：把本次新加的 strip() 剥掉后，与改动前逐块相同。
    """
    if not text:
        return []
    # 定位在"去掉全部空白"的文本上进行：切分会在句子边界丢掉分隔空白
    # （块里是 "Figure 1.Diagram"，原文是 "Figure 1. Diagram"），
    # 保留空白的任何比对都会因此失配。去空白后逐字对上，位置再回指原文。
    flat, pos, i, n = [], [], 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        flat.append(text[i])
        pos.append(i)
        i += 1
    ftext = "".join(flat)
    chunks = _chunk_text(text, max_chars=max_chars, min_chars=min_chars)
    out, at = [], 0
    for c in chunks:
        probe = _unspace(c)
        found, cut = -1, 0
        if probe:
            # 用块首前缀定位，逐级缩短：切分会在句边界丢分隔空白、也会把相邻
            # 段落直接粘起来（"experience.Our"），整块因此不一定是原文的连续切片，
            # 但**块首**总能对上。短于 40 字的前缀太容易锚到别处，不再往下缩。
            for n in (160, 80, 60, 40):
                if len(probe) < n:
                    continue
                found = ftext.find(probe[:n], at)
                if found < 0:
                    # 顺次找不到就允许从更早的位置重找。曾是"只准往前"，
                    # 一块失配就级联带偏后面全部（实测 52% 的块拿不到页码）；
                    # 允许回退后降到 1.3%。
                    found = ftext.find(probe[:n])
                if found >= 0:
                    cut = n
                    break
        if found < 0:
            # 定位不到的块**照样保留**：它是有效的证据候选，只是给不出页码。
            # （曾把这类块直接丢掉，结果块数比切分本体少 1–9 个——
            #   证据少了比页码缺了更糟。）
            out.append({"text": c, "start": None, "end": None})
            continue
        st = pos[found]
        # 末端：前缀匹配只能保证块首对齐，末端按块内非空白字数推进即可——
        # 页码只到页级精度，块尾差几个字不影响落在哪一页。
        n_end = min(len(pos) - 1, found + max(cut, len(probe)) - 1)
        en = max(pos[n_end] + 1, st + 1)
        out.append({"text": c, "start": st, "end": min(len(text), en)})
        # 游标只推进已匹配的前缀：推少了下一块会锚回本块内部，
        # 推多了会直接跳过下一块。
        at = found + cut
    return out


def _unspace(s: str) -> str:
    """去掉全部空白（定位用：切分会在句边界丢分隔空白，只能按非空白字比对）。"""
    return re.sub(r"\s+", "", s or "")


def _chunk_text(text: str, max_chars: int = 1200, min_chars: int = 60) -> list:
    """切分本体（重构前原样保留，逐分支不加改动）。"""
    if not text:
        return []
    rough = re.split(r"\n\s*\n|(?m)^\s*(?:\d{1,2}\.?\s+)?[A-Z][A-Za-z ]{2,40}$", text)
    chunks, buf = [], ""
    for seg in rough:
        seg = re.sub(r"\s+", " ", seg).strip()
        if not seg:
            continue
        if len(seg) > max_chars:
            # 过长的段落按句子窗口再切
            sents = re.split(r"(?<=[.。!?！？；;])\s*", seg)
            cur = ""
            for s in sents:
                # 单个"句子"就超长（无标点的长文本）→ 硬切
                while len(s) > max_chars:
                    if cur:
                        chunks.append(cur.strip())
                        cur = ""
                    chunks.append(s[:max_chars])
                    s = s[max_chars:]
                if len(cur) + len(s) > max_chars and len(cur) >= min_chars:
                    chunks.append(cur.strip())
                    cur = s
                else:
                    cur += s
            if cur.strip():
                chunks.append(cur.strip())
            continue
        if len(buf) + len(seg) <= max_chars:
            buf += (" " if buf else "") + seg
        else:
            if len(buf) >= min_chars:
                # strip 只为消除"是否带首尾空格"这一歧义（位置定位要靠文本精确匹配）；
                # 长度判断仍在 strip 之前做，分组口径不变。
                chunks.append(buf.strip())
            buf = seg
    if len(buf) >= min_chars:
        chunks.append(buf.strip())
    return chunks


class PageMap:
    """把正文里的字符位置映射到页码（用解析时留下的 page_spans）。"""

    def __init__(self, spans: list, total_chars: int = 0):
        self.spans = [s for s in (spans or [])
                      if isinstance(s, dict) and s.get("start") is not None]
        self.total = total_chars

    def page_of(self, pos: int) -> str:
        """返回 "3" 这样的页码字符串；无页码信息（如 Word 稿）返回空串。"""
        if not self.spans:
            return ""
        page = self.spans[0].get("page")
        for s in self.spans:
            if (s.get("start") or 0) <= pos:
                page = s.get("page")
            else:
                break
        return str(page) if page else ""

    def range_of(self, start: int, end: int) -> str:
        """证据跨页时给 "5-6"，同页给 "5"。"""
        a, b = self.page_of(start), self.page_of(end)
        if not a:
            return ""
        return a if a == b else "%s-%s" % (a, b)


def _norm_flat(s: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (s or "").lower())


class EvidenceRetriever:
    """按篇检索证据：给一句话，从该篇论文原文中找出最相关的段落。

    这就是 RAG 里的「R」——把生成/校验的依据落回原文，而不是凭模型记忆。

    覆盖要求：必须尽量覆盖全文。实测发现，若只索引前 6 万字符（论文 5 万字），
    会漏掉正文中段的证据（例如自蒸馏相关段落），导致把真实引用误判为"无支撑"。

    索引口径（实测教训）：只索引**正文**。此前的做法把标题与摘要单独拼在正文前，
    于是"论文标题"本身成了一个独立片段——它和任何谈论该论文的句子都高度相似，
    检索首条命中经常是它，再被判定为"有支撑"，支持率因此虚高。
    页眉/页脚/参考文献区同样不进索引：前者每页重复，后者讲的是别人的论文。
    """

    def __init__(self, docs: list, max_chars: int = 800, per_doc_chunks: int = 400,
                 full_text_limit: int = 200000, verbose: bool = False):
        from .literature import strip_page_furniture

        self.by_doc = {}          # doc_key -> {"chunks":[...], "index":HybridRetriever}
        self.meta = {}
        self.pagemap = {}
        self.dropped = {}         # doc_key -> 未进索引的区间数（供界面解释）
        for i, d in enumerate(docs):
            key = d.get("id") or d.get("local_pdf") or ("doc-%d" % i)
            raw = (d.get("full_text") or "")[:full_text_limit]
            repeated = {(r or "").strip() for r in (d.get("repeated_lines") or [])}
            # 第三个返回值（坐标平移表）在这里用不上：检索只用过滤后的文本。
            text, ignored, _shift = strip_page_furniture(raw, repeated, drop_refs=True)
            if not text.strip():
                text, ignored = raw, []
            chunks = [c for c in ({"text": c["text"].strip(),
                                   "start": c["start"], "end": c["end"]}
                                  for c in chunk_spans(text, max_chars=max_chars, min_chars=40))
                      if c["text"]]
            title_key = _norm_flat(d.get("title") or "")[:80]
            kept = []
            for c in chunks:
                flat = _norm_flat(c["text"])
                # 纯标题片段没有证据价值，只会抢首位（见类文档字符串）
                if title_key and len(flat) <= len(title_key) + 20 and flat.startswith(title_key):
                    continue
                kept.append(c)
            kept = kept[:per_doc_chunks]
            if not kept:
                continue
            self.by_doc[key] = {
                "chunks": [c["text"] for c in kept],
                "spans": [(c["start"], c["end"]) for c in kept],
                "index": HybridRetriever([{"full_text": c["text"], "title": ""} for c in kept],
                                         verbose=verbose),
            }
            self.meta[key] = {"title": d.get("title"), "year": d.get("year"),
                              "pages": d.get("pages"), "chunks": len(kept),
                              "ignored_spans": len(ignored)}
            self.pagemap[key] = PageMap(d.get("page_spans"), len(text))
            self.dropped[key] = len(ignored)

    def __len__(self) -> int:
        return len(self.by_doc)

    def find(self, doc_key: str, claim: str, top_k: int = 3, extra_queries: list = None) -> list:
        """在某篇论文中检索与该论断最相关的段落。

        多查询检索：整句 + 关键词 + 子句（+ 可选的外语关键词），取并集后按相似度排序。
        单用整句容易因长句噪声漏检；中文论断检索英文论文时，还需要英文关键词补充。
        """
        entry = self.by_doc.get(doc_key)
        if not entry or not claim:
            return []

        queries = [claim[:200]]
        kws = textutil.keywords(claim, top_k=8)
        if kws:
            queries.append(" ".join(kws))
        subs = [s.strip() for s in re.split(r"[；;，,]", claim) if len(s.strip()) >= 8]
        queries.extend(subs[:2])
        if extra_queries:
            queries.extend([str(q) for q in extra_queries if q])

        pm = self.pagemap.get(doc_key) or PageMap([])
        best = {}
        for q in queries:
            for idx, rrf, detail in entry["index"].search(q, top_k=top_k):
                sc = detail.get("vec", 0.0)
                if idx not in best or sc > best[idx]["score"]:
                    st, en = entry["spans"][idx]
                    # 定位不到的块 start 为 None：位置留空，页码也留空，
                    # 绝不能当成 0 —— 那会把证据说成第 1 页。
                    if st is None:
                        best[idx] = {"chunk": entry["chunks"][idx], "score": sc, "rrf": rrf,
                                     "start": None, "end": None, "page": "", "page_range": ""}
                    else:
                        best[idx] = {"chunk": entry["chunks"][idx], "score": sc, "rrf": rrf,
                                     "start": st, "end": en,
                                     "page": pm.page_of(st), "page_range": pm.range_of(st, en)}
        return sorted(best.values(), key=lambda x: -x["score"])[:top_k]

    def best_score(self, doc_key: str, claim: str) -> float:
        hits = self.find(doc_key, claim, top_k=1)
        return hits[0]["score"] if hits else 0.0
