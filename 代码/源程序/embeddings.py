"""向量化：优先使用阿里云百炼的 text-embedding 模型，不可用时退回本地字符 n-gram。

为什么需要它：字符 n-gram 相似度只反映"字面重叠"，不是语义匹配。
中文论断检索英文论文时，字面几乎不重叠，会把真实引用误判为"无支撑"。
真正的 embedding 模型能跨语言/跨表述匹配语义，这才是 RAG 检索该有的样子。

设计：
- 云端：百炼 text-embedding-v3（阿里云产品，本项目"必须使用阿里云"的又一落点）
- 本地：字符 n-gram 哈希（断网兜底，保证可演示）
- 缓存：向量按内容哈希缓存到磁盘，同一段文本只嵌入一次（控制成本）
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

from . import local_llm, textutil
from .config import settings

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "embed")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-v3")
BATCH = 10            # 百炼 embedding 单次最多 10 条


def _key(text: str, backend: str = None) -> str:
    """缓存键含**后端标识**：云端 1024 维与本地 512 维向量绝不能混用。"""
    tag = backend or EMBED_MODEL
    return hashlib.sha256((tag + "|" + text).encode("utf-8")).hexdigest()[:40]


def _cache_path(k: str) -> str:
    return os.path.join(CACHE_DIR, k + ".json")


def _load_cache(k: str):
    p = _cache_path(k)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)["vec"]
        except Exception:
            return None
    return None


def _save_cache(k: str, vec) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(_cache_path(k), "w", encoding="utf-8") as f:
            json.dump({"vec": vec}, f)
    except Exception:
        pass


def _local_vector(text: str, dim: int = 2048) -> list:
    """本地兜底：字符 n-gram 哈希 + L2 归一化。"""
    import zlib
    grams = {}
    t = textutil.char_ngrams(text, 3)
    if not t:
        return [0.0] * dim
    for g in t:
        b = zlib.crc32(g.encode("utf-8")) % dim
        grams[b] = grams.get(b, 0) + 1
    vec = [0.0] * dim
    for b, c in grams.items():
        vec[b] = 1 + math.log(c)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _remote_embed(texts: list, verbose: bool = False) -> list:
    """调用百炼 embedding 接口。失败返回 None。

    按每批 BATCH（10 条）发一次请求，几千个文本块就是几百次 HTTP —— 全程
    不打字同样会让用户以为程序卡住。verbose 时每 10 批报一次进度。
    """
    if not (settings.api_key and not settings.force_offline):
        return None
    url = settings.base_url + "/embeddings"
    out = []
    n_batches = (len(texts) + BATCH - 1) // BATCH
    if verbose and n_batches > 3:
        print("   [向量] 需调用百炼 text-embedding 共 %d 批（每批 %d 条），首次会慢一些…"
              % (n_batches, BATCH))
        sys.stdout.flush()
    for bi, i in enumerate(range(0, len(texts), BATCH), 1):
        chunk = texts[i:i + BATCH]
        body = {"model": EMBED_MODEL, "input": chunk,
                "encoding_format": "float"}
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"),
                headers={"Authorization": "Bearer " + settings.api_key,
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8"))
            items = sorted(d.get("data") or [], key=lambda x: x.get("index", 0))
            got = [it.get("embedding") for it in items]
            if len(got) != len(chunk) or any(v is None for v in got):
                return None
            out.extend(got)
            # 记账（embedding 单价极低，仅用于成本台账）
            try:
                from .llm import _load_ledger, _save_ledger
                led = _load_ledger()
                led["cost"] += 0.00002 * len(chunk)
                led["calls"] += 1
                _save_ledger(led)
            except Exception:
                pass
            if verbose and n_batches > 3 and (bi % 10 == 0 or bi == n_batches):
                print("   [向量] 进度 %d/%d 批" % (bi, n_batches))
                sys.stdout.flush()
        except urllib.error.HTTPError as e:
            if verbose:
                print("   [向量] 第 %d 批失败：HTTP %d，改用下一级后备" % (bi, e.code))
                sys.stdout.flush()
            return None
        except Exception as e:
            if verbose:
                print("   [向量] 第 %d 批失败：%s，改用下一级后备" % (bi, str(e)[:70]))
                sys.stdout.flush()
            return None
    return out


class Embedder:
    """统一的向量化入口：云端优先、缓存优先、本地兜底。"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.backend = "local"
        self.n_remote = 0
        self.n_cached = 0
        self.n_local = 0

    def encode(self, texts: list) -> list:
        """返回与 texts 等长的向量列表。

        取向量优先级（逐级降级，保证任何情况下都有结果）：
          1. 云端百炼 text-embedding-v3（有 Key 且未强制离线）
          2. **本地模型 bge-small-zh**（离线首选；用 llama.cpp 推理）
          3. 本地字符 n-gram 哈希（最终兜底，永远可用）

        缓存按后端隔离：云端 1024 维与本地 512 维是不同向量空间，
        混用会让相似度失去意义。
        """
        if not texts:
            return []
        offline = settings.force_offline
        cloud_ok = settings.llm_available

        # ── 选择本次使用的后端 ──
        backend_tag = None
        if cloud_ok:
            backend_tag = EMBED_MODEL
        elif local_llm.available("embed"):
            backend_tag = "local-bge-small-zh"

        results = [None] * len(texts)
        todo, todo_idx = [], []
        use_cache = settings.enable_cache and backend_tag is not None
        for i, t in enumerate(texts):
            k = _key(t, backend_tag) if backend_tag else None
            v = _load_cache(k) if (use_cache and k) else None
            if v is not None:
                results[i] = v
                self.n_cached += 1
                if self.backend == "local":
                    self.backend = backend_tag
            else:
                todo.append(t)
                todo_idx.append((i, k))

        if todo:
            if cloud_ok:
                vecs = _remote_embed(todo, verbose=self.verbose)
                if vecs and len(vecs) == len(todo):
                    self.backend = "dashscope:" + EMBED_MODEL
                    self.n_remote += len(todo)
                    for (i, k), v in zip(todo_idx, vecs):
                        results[i] = v
                        if settings.enable_cache and k:
                            _save_cache(k, v)
                else:
                    cloud_ok = False       # 云端失败，继续走本地

            if not cloud_ok and local_llm.available("embed"):
                vecs = local_llm.embed_texts(todo)
                if vecs and len(vecs) == len(todo):
                    self.backend = "local-bge-small-zh"
                    self.n_local += len(todo)
                    for (i, k), v in zip(todo_idx, vecs):
                        results[i] = v
                        if settings.enable_cache and k:
                            _save_cache(k, v)

            # 兜底：任何空位都用 n-gram 向量补齐
            missing = [(i, k, t) for (i, k), t in zip(todo_idx, todo) if results[i] is None]
            if missing:
                if self.backend not in ("dashscope:" + EMBED_MODEL, "local-bge-small-zh"):
                    self.backend = "local-ngram"
                self.n_local += len(missing)
                for i, k, t in missing:
                    results[i] = _local_vector(t)

        # 极少数情况下仍有空位（例如 texts 含空串）→ 补齐
        for i, v in enumerate(results):
            if v is None:
                results[i] = _local_vector(texts[i])
                self.n_local += 1
        if self.verbose:
            print("[向量] 后端=%s 缓存命中=%d 云端=%d 本地=%d" % (
                self.backend, self.n_cached, self.n_remote, self.n_local))
        return results


def cosine(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n])) or 1.0
    nb = math.sqrt(sum(x * x for x in b[:n])) or 1.0
    return dot / (na * nb)
