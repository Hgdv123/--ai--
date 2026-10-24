"""百炼（DashScope）客户端：增强通道。

三条护栏（对应成本要求）：
1. 结果缓存 —— 相同请求不重复计费；
2. 单日成本上限 —— 超限直接拒绝调用并降级到本地引擎；
3. 调用失败/无网络/无 Key —— 一律返回 None，由上层自动降级，绝不中断演示。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

import re

from . import local_llm
from .config import OUT_DIR, settings, key_problem, key_usable


def _extract_json(text: str):
    """从模型输出里尽力抠出 JSON 对象。

    本地小模型常把 JSON 包在解释文字或代码块里，需要做一轮修复提取；
    这是离线可用的关键一环——不修就整张卡片丢失。
    """
    if not text:
        return None
    t = text.strip()
    # 去掉代码围栏
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    # 取第一个 { 到最后一个 } 之间的内容
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        frag = t[i:j + 1]
        try:
            return json.loads(frag)
        except Exception:
            # 常见毛病：多余尾逗号、单引号、未加引号的键
            fixed = re.sub(r",\s*([}\]])", r"\1", frag)
            fixed = fixed.replace("'", '"')
            fixed = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', fixed)
            try:
                return json.loads(fixed)
            except Exception:
                return None
    return None

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
COST_FILE = os.path.join(CACHE_DIR, "cost_ledger.json")

# 参考单价（元 / 千 tokens）——仅用于本地成本估算与限流，非账单依据
PRICES = {
    "qwen-turbo": (0.0003, 0.0006),
    "qwen-plus": (0.0008, 0.002),
    "qwen-max": (0.0024, 0.0096),
    "default": (0.001, 0.002),
}


def _ensure_cache() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)


def _hash_key(model: str, messages: list, temperature: float) -> str:
    blob = json.dumps({"m": model, "msgs": messages, "t": temperature},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, key + ".json")


def _load_ledger() -> dict:
    _ensure_cache()
    today = time.strftime("%Y-%m-%d")
    try:
        with open(COST_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if d.get("date") != today:
        d = {"date": today, "cost": 0.0, "calls": 0, "cached": 0, "tokens": 0}
    return d


def _save_ledger(d: dict) -> None:
    try:
        with open(COST_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def today_cost() -> dict:
    return _load_ledger()


def _validate_key(raw: str) -> tuple:
    """返回 (可用 key, 问题说明)。判据与 settings.llm_available 共用 config.key_problem。

    为什么不再在这里单独实现：两处判据若不一致，会出现
    "状态条说云端可用、实际调用却失败"的自相矛盾（实测踩到过）。
    """
    key = (raw or "").strip()
    if not key:
        return "", None                       # 没填是正常情况，不报错
    prob = key_problem(key)
    if prob:
        return "", prob + "本次改用本地通道。"
    return key, None


class LLMClient:
    """统一生成入口：云端百炼优先，离线时自动改用**本地部署模型**。

    调用方只关心 chat() / chat_json()，不需要知道背后是云端还是本地——
    离线时若本地模型可用则返回真实生成结果，否则返回 (None, 原因) 由调用方降级。
    """

    def __init__(self, api_key: str = None, base_url: str = None, verbose: bool = False):
        raw = api_key if api_key is not None else settings.api_key
        self.api_key, self.key_problem = _validate_key(raw)
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self.verbose = verbose
        self.used_local = False
        self.n_local = 0
        if self.key_problem:
            # 让用户知道"为什么没走云端"，而不是只看到一串编码错误
            print("   [模型] %s" % self.key_problem)
            sys.stdout.flush()

    @property
    def available(self) -> bool:
        """是否可生成：云端可用，或离线时本地模型可用。"""
        if self.cloud:
            return True
        return local_llm.available("gen")

    @property
    def cloud(self) -> bool:
        """本次是否走云端。

        判据只此一处，其他模块都引用它，避免三个地方各写一遍再各错一遍：
        光看 `api_key` 非空是不够的——占位符（如"sk-在此填入你的百炼APIKey"）
        非空但不是 Key，会让界面显示"云端处理"而实际走本地。
        """
        return key_usable(self.api_key) and not settings.force_offline

    def _local_chat(self, messages: list, max_tokens: int, temperature: float,
                    seed: int = None):
        """把对话消息压成单条提示词交给本地模型。

        返回的元信息与云端保持**同样的 dict 结构**（model/cached/tokens/local），
        调用方无需区分来源；失败时返回字符串原因，便于直接展示。

        `seed` 透传给本地服务：需要可复现的输出时必须给（见 local_llm.chat 说明）。
        """
        parts = []
        for m in messages or []:
            c = (m.get("content") or "").strip()
            if not c:
                continue
            if m.get("role") == "system":
                parts.append("[要求] " + c)
            else:
                parts.append(c)
        prompt = "\n\n".join(parts)
        t0 = time.time()
        out = local_llm.chat(prompt, max_tokens=max_tokens, temperature=temperature,
                             seed=seed)
        if not out:
            # 不要写"未安装模型或服务未启动"——那是猜的。模型在不在、服务起没起，
            # 这里一查就知道；真实失败原因（超时/503/上下文超限）由 local_llm
            # 自己打印。把可能的原因如实列出来，用户才有得查。
            if not local_llm.available("gen"):
                why = "本地生成模型不可用（models/ 下缺少 .gguf 或 llama-server）"
            else:
                why = ("本地模型调用失败（模型已就位；具体原因见上方 [本地模型] 日志，"
                       "常见为服务仍在加载或提示词超出上下文）")
            return None, why
        self.used_local = True
        self.n_local += 1
        return out, {
            "model": "本地部署模型 Qwen2.5-3B",
            "cached": False,
            "local": True,
            "tokens": None,
            "seconds": round(time.time() - t0, 2),
        }

    def chat(self, messages: list, model: str = None, temperature: float = 0.2,
             max_tokens: int = 1200, json_mode: bool = False, retries: int = 2,
             timeout: int = None, verbose: bool = False, seed: int = None):
        """返回 (文本, 元信息) 或 (None, 原因)。失败绝不抛异常。

        timeout：单次请求的读超时。**必须随 max_tokens 放大**——固定 60 秒
        对"生成 2000+ tokens"的调用太紧（实测生成 2400 tokens 的草稿约需
        15 秒，但网络抖动时会直接撞上 60 秒上限，然后重试，用户侧就是
        "卡了好几分钟没反应"）。默认按 max_tokens 估算：至少 60 秒，
        每 1000 tokens 再加 30 秒。

        verbose：打印"正在等待模型返回/超时后重试"等进度。草稿阶段只有
        一次云调用，全程不打字会让用户以为程序死了（实测被当成卡死）。
        """
        if not self.available:
            return None, "未配置 API Key 且未安装本地模型"

        if not self.cloud:
            # 离线：走本地部署模型。
            # 本地生成**也走缓存**：llama.cpp 在 CPU 上无法保证长序列逐位可复现
            # （同一提示词、同温度同 seed，两次仍会分叉——实测 380 vs 623 字符，
            # 且把生成槽数降到 1 也没有改善）。于是"同一批文献两次生成不同草稿"
            # 会连带让自检结论每次都变，用户合理地追问"为什么两次审查不一样"。
            # 缓存是确定性的：同一输入第二次直接复用，不再依赖推理是否可复现。
            lkey = _hash_key("local", messages, temperature)
            lcp = _cache_path(lkey)
            if settings.enable_cache and os.path.isfile(lcp):
                try:
                    with open(lcp, encoding="utf-8") as f:
                        cached = json.load(f)
                    self.used_local = True
                    return cached["text"], {
                        "model": "本地部署模型 Qwen2.5-3B", "cached": True,
                        "local": True, "tokens": cached.get("tokens", 0)}
                except Exception:
                    pass
            text, meta = self._local_chat(messages, max_tokens, temperature, seed=seed)
            if text and settings.enable_cache:
                try:
                    os.makedirs(os.path.dirname(lcp), exist_ok=True)
                    with open(lcp, "w", encoding="utf-8") as f:
                        json.dump({"text": text,
                                   "tokens": (meta or {}).get("tokens", 0)}, f,
                                  ensure_ascii=False)
                except Exception:
                    pass
            return text, meta

        model = model or settings.model_default
        ledger = _load_ledger()
        if ledger["cost"] >= settings.daily_cost_limit:
            return None, "已达单日成本上限 %.0f 元，自动降级本地引擎" % settings.daily_cost_limit

        key = _hash_key(model, messages, temperature)
        cp = _cache_path(key)
        if settings.enable_cache and os.path.isfile(cp):
            try:
                with open(cp, encoding="utf-8") as f:
                    cached = json.load(f)
                ledger["cached"] += 1
                _save_ledger(ledger)
                return cached["text"], {"cached": True, "model": model}
            except Exception:
                pass

        body = {"model": model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        # 按输出规模给足读超时：固定 60 秒对长生成太紧，网络一抖动就会
        # 白等 60 秒再重试，用户侧只看到"卡住"。
        if timeout is None:
            timeout = max(60, 60 + (int(max_tokens or 0) // 1000) * 30)
        if verbose:
            print("   [模型] 正在等待 %s 返回（最多 %d 秒，输出上限 %d tokens）…"
                  % (model, timeout, max_tokens))
            sys.stdout.flush()

        last_err = ""
        for attempt in range(retries + 1):
            t_call = time.time()
            try:
                req = urllib.request.Request(
                    self.base_url + "/chat/completions",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Authorization": "Bearer " + self.api_key,
                             "Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
                text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                usage = d.get("usage") or {}
                dt = time.time() - t_call
                pin, pout = PRICES.get(model, PRICES["default"])
                cost = (usage.get("prompt_tokens", 0) / 1000.0) * pin + \
                       (usage.get("completion_tokens", 0) / 1000.0) * pout
                ledger["cost"] += cost
                ledger["calls"] += 1
                ledger["tokens"] += usage.get("total_tokens", 0)
                _save_ledger(ledger)
                if settings.enable_cache and text:
                    try:
                        with open(cp, "w", encoding="utf-8") as f:
                            json.dump({"text": text, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                      f, ensure_ascii=False)
                    except Exception:
                        pass
                if verbose:
                    print("   [模型] %s 已返回：用时 %.1fs，输出 %s 字，本次 %.4f 元，今日累计 %.3f 元"
                          % (model, dt, len(text), cost, ledger["cost"]))
                    sys.stdout.flush()
                return text, {"cached": False, "model": model, "cost": cost,
                              "tokens": usage.get("total_tokens", 0)}
            except urllib.error.HTTPError as e:
                last_err = "HTTP %d: %s" % (e.code, e.read().decode("utf-8", "ignore")[:160])
            except Exception as e:
                last_err = str(e)[:160]
            dt = time.time() - t_call
            # 重试必须说出来：用户看不到任何字时，会把"在重试"当成"程序死了"。
            if verbose:
                print("   [模型] 第 %d 次请求失败（等了 %.1fs）：%s%s"
                      % (attempt + 1, dt, last_err[:90],
                         "，正在重试…" if attempt < retries else "，不再重试"))
                sys.stdout.flush()
            time.sleep(1.0 + attempt)
        return None, "调用失败（已重试）：%s" % last_err

    def chat_json(self, messages: list, model: str = None, max_tokens: int = 1500):
        """要求返回 JSON；解析失败先做修复提取，再重试一次。"""
        for attempt in range(2):
            text, meta = self.chat(messages, model=model, json_mode=True,
                                   max_tokens=max_tokens, temperature=0.1)
            if not text:
                return None, meta
            got = _extract_json(text)
            if got is not None:
                return got, meta
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": text[:400]},
                    {"role": "user", "content": "只输出合法 JSON，不要任何解释文字、不要代码块。"}]
        return None, "JSON 解析失败"
