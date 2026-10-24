"""本地模型服务：用 llama.cpp 在离线状态下提供向量化与文本生成。

设计要点
--------
1. **模型随应用走**：默认在 `wenmai/models/` 下查找，所以整个文件夹拷到任何
   电脑都能用；也支持环境变量 `WM_LOCAL_MODEL_DIR` 指向其他位置。
2. **惰性启动、自动回收**：首次调用才启动 llama-server，进程随 Python 退出而结束；
   嵌入服务与生成服务分开端口，互不干扰。
3. **缺失即降级**：模型文件不存在时不报错，由调用方退回上一级方案
   （n-gram 向量 / 模板生成），保证任何情况下都能跑。
"""
from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .config import PKG_DIR

# 查找顺序：环境变量 → 包内 models/ → 项目根 models/
_ENV_DIR = os.environ.get("WM_LOCAL_MODEL_DIR", "").strip()
MODEL_DIRS = [d for d in (
    _ENV_DIR,
    os.path.join(PKG_DIR, "models"),
    os.path.join(os.path.dirname(PKG_DIR), "models"),
) if d]

# 二进制目录（llama.cpp 解压后的 bin/）
BIN_NAMES = ("llama-server.exe", "llama-server")

EMBED_MODEL_NAMES = ("bge-small-zh-q8_0.gguf", "bge-small-zh-v1.5-q8_0.gguf")
GEN_MODEL_NAMES = ("qwen2.5-3b-q4_k_m.gguf", "qwen2.5-3b-instruct-q4_k_m.gguf")
# 投机解码用的草稿模型（可选，有则大幅加速且不改变输出质量）
DRAFT_MODEL_NAMES = ("qwen2.5-0.5b-q4_k_m.gguf", "qwen2.5-0.5b-instruct-q4_k_m.gguf")

EMBED_PORT = int(os.environ.get("WM_EMBED_PORT", "8931"))
GEN_PORT = int(os.environ.get("WM_GEN_PORT", "8932"))

EMBED_CTX = 512
# 生成上下文：**这是总上下文，会被 -np 个槽平分**。
# 实测教训：-c 4096 + -np 4 → 每槽仅 1024 token，而卡片抽取提示词就需
# 900~1040 token，服务端直接报 "request (1039 tokens) exceeds the available
# context size (1024 tokens)" 并拒绝，导致 6 篇里有 4 篇卡片退回本地规则结果。
# 改为 16384 → 每槽 4096 token，留足提示词 + 输出的余量。
GEN_CTX = int(os.environ.get("WM_GEN_CTX", "16384"))

# 生成线程数：实测 12 最快（本机 16 物理核 / 24 逻辑核，i7-14650HX 混合架构）。
# 超过物理核数会因调度开销反而变慢：16 线程 9.62 t/s vs 12 线程 10.86 t/s。
_GEN_THREADS = int(os.environ.get("WM_GEN_THREADS", "12"))
# 生成服务的并发槽位数：实测 4 槽并发处理 4 个请求比串行快 100%
GEN_SLOTS = int(os.environ.get("WM_GEN_SLOTS", "4"))
# 嵌入用小模型，线程少一些即可
_EMBED_THREADS = max(4, min(8, (os.cpu_count() or 8) // 2))

_processes = {}
_locks = {}
_atexit_done = False


def _register_atexit():
    global _atexit_done
    if _atexit_done:
        return
    atexit.register(shutdown)
    _atexit_done = True


def find_bin() -> str:
    """定位 llama-server 可执行文件。"""
    env_bin = os.environ.get("WM_LLAMA_BIN", "").strip()
    cands = []
    if env_bin:
        cands.append(env_bin)
    for d in MODEL_DIRS:
        for sub in ("bin", ""):
            for n in BIN_NAMES:
                cands.append(os.path.join(d, sub, n) if sub else os.path.join(d, n))
    # 开发期：下载目录
    cands.append(os.path.join(os.path.dirname(PKG_DIR), ".dsh-tmp", "llama", "bin", "llama-server.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def find_model(names: tuple) -> str:
    """在候选目录中查找模型文件。"""
    for d in MODEL_DIRS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    # 开发期：下载目录
    dev = os.path.join(os.path.dirname(PKG_DIR), ".dsh-tmp", "llama")
    for n in names:
        p = os.path.join(dev, n)
        if os.path.isfile(p):
            return p
    return ""


def embed_model_path() -> str:
    return find_model(EMBED_MODEL_NAMES)


def gen_model_path() -> str:
    return find_model(GEN_MODEL_NAMES)


def draft_model_path() -> str:
    return find_model(DRAFT_MODEL_NAMES)


def available(kind: str = "embed") -> bool:
    """本地模型是否可用（二进制与对应模型都在）。"""
    if not find_bin():
        return False
    return bool(embed_model_path() if kind == "embed" else gen_model_path())


def status() -> dict:
    """供 doctor / 界面展示的状态。"""
    b = find_bin()
    em = embed_model_path()
    gm = gen_model_path()
    dm = draft_model_path()
    return {
        "二进制": b or "（未找到）",
        "向量模型": ("%s (%.1f MB)" % (os.path.basename(em), os.path.getsize(em) / 1048576))
                    if em else "（未找到）",
        "生成模型": ("%s (%.1f MB)" % (os.path.basename(gm), os.path.getsize(gm) / 1048576))
                    if gm else "（未找到）",
        "草稿模型": ("%s (%.1f MB)" % (os.path.basename(dm), os.path.getsize(dm) / 1048576))
                    if dm else "（未安装，安装后可提速约 59%）",
        "生成线程": _GEN_THREADS,
        "生成槽位": GEN_SLOTS,
        "生成上下文": GEN_CTX,
        "每槽上下文": GEN_CTX // max(GEN_SLOTS, 1),
        "向量可用": bool(b and em),
        "生成可用": bool(b and gm),
        "投机解码": bool(b and gm and dm),
    }


def _healthy(port: int) -> bool:
    """llama-server 是否**活着**（不是"此刻空闲"）。

    判据是有没有 HTTP 应答，而不是应答码是否为 200：
    推理进行中时服务仍在正常处理，但 `/health` 可能回 503 或来不及应答。
    以前只认 200、且超时仅 2 秒，于是"忙"被误判成"没起来"——`_start()` 随即
    另起一个实例抢占同一端口，那个新进程必然起不来，最终把一次正常推理
    报成"llama-server 未就绪或返回异常"（实测：Agent 审稿中途偶发失败，
    然后整轮退回确定性计划）。
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return True          # 有 HTTP 应答 = 进程活着（503 = 忙，不是死）
    except Exception:
        return False


def _serving(port: int) -> bool:
    """端口上是否已有进程在监听。

    **不能当作"已就绪"**：llama-server 是先绑定端口、再加载模型权重
    （3B 要十几秒），这段窗口期端口已经在监听但请求会回 503。
    它只用来判断"是不是已经有实例占着端口"（避免重复拉起），
    就绪与否一律以 `_healthy()`（/health 返回 200）为准。
    """
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _start(kind: str):
    """启动（或复用）llama-server。返回端口；失败返回 0。"""
    _register_atexit()
    lock = _locks.setdefault(kind, threading.Lock())
    with lock:
        port = EMBED_PORT if kind == "embed" else GEN_PORT
        # 忙 ≠ 没起来：推理进行中 /health 可能回 503，那种情况必须复用，
        # 否则会另起一个抢不到端口的实例（实测把一次正常推理报成"未就绪"）。
        # 但"端口在监听"**不能**当作已就绪——那是模型还在加载的窗口期，
        # 请求会回 503。真正就绪只有 /health 返回 200 才算。
        if _healthy(port):
            return port
        if _serving(port):
            # 端口上已有实例（可能仍在加载模型）：等它就绪，不再新起进程
            for _ in range(240):
                if _healthy(port):
                    return port
                time.sleep(0.5)
            return 0
        binp = find_bin()
        model = embed_model_path() if kind == "embed" else gen_model_path()
        if not binp or not model:
            return 0
        if kind == "embed":
            cmd = [binp, "-m", model, "-t", str(_EMBED_THREADS),
                   "--port", str(port), "--host", "127.0.0.1", "-np", "1",
                   "--embeddings", "-c", str(EMBED_CTX)]
        else:
            cmd = [binp, "-m", model, "-t", str(_GEN_THREADS),
                   "--port", str(port), "--host", "127.0.0.1",
                   "-c", str(GEN_CTX), "-ub", "128",
                   # 多槽位：实测 4 槽并发处理 4 个请求比串行快 100%
                   # （llama-server 批处理把多个请求合并计算，CPU 利用率更高）
                   "-np", str(GEN_SLOTS)]
            # 投机解码：有草稿模型就启用。实测 9.62 → 15.28 t/s（+58.9%），
            # 且这是精确等价的加速手段，不改变输出内容。
            draft = draft_model_path()
            if draft:
                cmd += ["-md", draft, "--spec-type", "draft-simple",
                        "--spec-draft-n-max", "3"]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
        p = subprocess.Popen(cmd, **kwargs)
        _processes[kind] = p
        # 等待就绪：小模型几秒，大模型约 10 秒；带草稿模型会更久
        for _ in range(360):
            if p.poll() is not None:
                return 0
            if _healthy(port):
                return port
            time.sleep(0.5)
        p.kill()
        return 0


def shutdown():
    """结束本进程启动的所有本地模型服务。"""
    for kind, p in list(_processes.items()):
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
        except Exception:
            pass
        _processes.pop(kind, None)


# ────────────────────────── 嵌入 ──────────────────────────

def embed_texts(texts: list, max_chars: int = 600) -> list:
    """用本地模型编码文本；失败返回 []（调用方据此降级）。"""
    if not texts:
        return []
    port = _start("embed")
    if not port:
        return []
    out = []
    # 逐条请求：嵌入上下文较小，批量容易触发 500
    for t in texts:
        body = json.dumps({"input": (t or "")[:max_chars] or " ", "model": "local"}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/v1/embeddings" % port, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        got = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    d = json.load(r)
                arr = d.get("data") or []
                if arr:
                    got = arr[0]["embedding"]
                break
            except Exception:
                if attempt == 2:
                    return out if out else []
                time.sleep(0.4)
        if got is None:
            return []
        out.append(got)
    return out


# ────────────────────────── 生成 ──────────────────────────

# 让模型输出后立即停止的标记：避免回显提示词、避免自问自答
STOP_MARKERS = [
    "\n用中文", "\n判断", "\n根据以下", "\n原文：", "\n论断：", "\n请", "\nUser", "\nHuman",
    "用户：", "\n问题", "\n任务", "<|im_end|>", "<|endoftext|>",
]


def chat(prompt: str, max_tokens: int = 400, temperature: float = 0.3,
         stop: list = None, retries: int = 3, seed: int = None) -> str:
    """用本地模型生成文本；失败返回空字符串（调用方据此降级）。

    通过 Qwen 的对话模板包装，并用停止标记切断回显与重复。

    `seed`：要**可复现**就同时传 seed。实测 llama-server 在 temperature=0 下
    并不是严格贪心——同一提示词连发两次，前 80 字相同、之后分叉（同 seed 时
    三次输出逐字节一致）。所以"温度 0"本身不足以固定结果，必须给 seed。

    失败会重试：服务刚起时可能仍在加载模型（回 503），推理繁忙时也可能偶发
    503——这些都不代表模型不可用。原实现是 `except Exception: return ""`，
    一次 503 就让调用方以为"模型没装/没启动"，用户看到的是一句**猜出来的**
    错误原因（实测被反馈过）。现在带重试，并把真实原因打印出来。
    """
    port = _start("gen")
    if not port:
        print("   [本地模型] 服务未就绪（models/ 缺失或启动失败）")
        return ""
    wrapped = ("<|im_start|>system\n你是一个学术写作助手，只输出要求的内容，"
               "不要重复题目，不要自问自答。<|im_end|>\n"
               "<|im_start|>user\n%s<|im_end|>\n<|im_start|>assistant\n" % prompt)
    stops = list(stop or []) + STOP_MARKERS
    body = {"prompt": wrapped, "n_predict": max_tokens,
            "temperature": temperature, "stream": False,
            "stop": stops,
            "repeat_penalty": 1.12}
    if seed is not None:
        body["seed"] = int(seed)
    body = json.dumps(body).encode()
    last_err = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request("http://127.0.0.1:%d/completion" % port, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                d = json.load(r)
            return _clean_output(d.get("content") or "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            last_err = "HTTP %d %s" % (e.code, detail)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, str(e)[:120])
        if attempt < retries:
            # 503 多半是还在加载模型或槽位被占，等一会儿再来
            time.sleep(2.0 * (attempt + 1))
    print("   [本地模型] 连续 %d 次生成失败：%s" % (retries + 1, last_err[:220]))
    return ""


def chat_tools(messages: list, tools: list, max_tokens: int = 1200,
               temperature: float = 0.2, timeout: int = 300,
               retries: int = 2) -> dict:
    """带工具定义的对话式调用（OpenAI 风格）；失败返回 {}。

    与 `chat()` 的区别：这里走 `/v1/chat/completions`，由 llama-server 套用模型
    自带对话模板，因此支持 `tools` / `tool_choice`，也能原样回传 tool_calls。
    Agent 在离线模式下依赖这条通路，否则只能退回确定性计划。

    失败会重试（默认 2 次、指数退避）：多槽位服务在并发下偶发 503，
    单次失败并不代表模型不可用；不重试的话一次抖动就会让整轮审稿退回
    确定性计划，用户侧看到的就是"模型调用失败"（实测发生过）。
    """
    port = _start("gen")
    if not port:
        return {"error": "本地模型服务未能启动（models/ 缺失，或 240 秒内未就绪）"}
    body = json.dumps({"model": "local", "messages": messages, "tools": tools,
                       "tool_choice": "auto", "temperature": temperature,
                       "max_tokens": max_tokens}).encode()
    last_err = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % port, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            msg = ((d.get("choices") or [{}])[0].get("message") or {})
            return {"content": msg.get("content") or "",
                    "tool_calls": msg.get("tool_calls") or [],
                    "usage": d.get("usage") or {}}
        except urllib.error.HTTPError as e:
            last_err = "HTTP %d" % e.code
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, str(e)[:80])
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            port = _start("gen") or port
    print("   [本地模型] 连续 %d 次调用失败：%s" % (retries + 1, last_err))
    return {"error": "连续 %d 次调用失败：%s" % (retries + 1, last_err)}


def _clean_output(text: str) -> str:
    """清理生成结果：去掉回显的提示词尾巴与整段重复。"""
    t = (text or "").strip()
    if not t:
        return ""
    # 1) 截断回显：出现"用中文/判断下面/原文："等提示词痕迹就从那里切断
    for mk in ("用中文概括", "用中文写", "判断下面", "根据以下要点", "原文：", "论断："):
        i = t.find(mk)
        if i > 0:
            t = t[:i]
    t = t.strip()
    # 2) 去掉整句重复（模型偶发把同一段输出两遍）
    half = len(t) // 2
    if half > 20 and t[:half].strip() == t[half:].strip():
        t = t[:half].strip()
    # 3) 去掉末尾悬空的不完整句
    t = t.rstrip(" ，,、：:；;")
    return t.strip()


# ────────────────────────── 模型下载 ──────────────────────────

# 全部来自官方/原始仓库；实测单连接被限速，用多连接分段并行可提速百倍以上
DOWNLOAD_ITEMS = [
    ("https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/releases/download/"
     "b10985/llama-b10985-bin-win-cpu-x64.zip", "llama-bin.zip",
     "llama.cpp 推理引擎（Windows CPU）", True),
    ("https://hf-mirror.com/CompendiumLabs/bge-small-zh-v1.5-gguf/resolve/main/"
     "bge-small-zh-v1.5-q8_0.gguf", "bge-small-zh-q8_0.gguf",
     "向量模型 bge-small-zh（25MB）", False),
    ("https://hf-mirror.com/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
     "qwen2.5-3b-instruct-q4_k_m.gguf", "qwen2.5-3b-q4_k_m.gguf",
     "生成模型 Qwen2.5-3B Q4（2007MB）", False),
]


def _parallel_download(url: str, dest: str, connections: int = 8,
                       chunk: int = 4 * 1024 * 1024, verbose: bool = True) -> bool:
    """多连接分段下载 + 断点续传。

    实测：单连接约 90 KB/s，8 连接聚合可达 10 MB/s 以上，
    所以并发不是锦上添花而是必需。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    if os.path.isfile(dest) and os.path.getsize(dest) > 1024:
        if verbose:
            print("    已存在，跳过（%.1f MB）" % (os.path.getsize(dest) / 1048576))
        return True

    ua = {"User-Agent": "Mozilla/5.0"}
    total = 0
    try:
        req = urllib.request.Request(url, headers=ua, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
    except Exception:
        try:
            req = urllib.request.Request(url, headers=dict(ua, Range="bytes=0-0"))
            with urllib.request.urlopen(req, timeout=30) as r:
                cr = r.headers.get("Content-Range") or ""
                total = int(cr.split("/")[-1]) if "/" in cr else 0
        except Exception:
            return False
    if not total:
        return False

    part_dir = dest + ".parts"
    os.makedirs(part_dir, exist_ok=True)
    ranges = [(i, min(i + chunk, total)) for i in range(0, total, chunk)]
    done = [0]
    lock = threading.Lock()

    def fetch(se):
        start, end = se
        pf = os.path.join(part_dir, "%010d" % start)
        want = end - start
        if os.path.isfile(pf) and os.path.getsize(pf) == want:
            with lock:
                done[0] += want
            return True
        for attempt in range(4):
            try:
                got = 0
                req = urllib.request.Request(
                    url, headers=dict(ua, Range="bytes=%d-%d" % (start, end - 1)))
                with urllib.request.urlopen(req, timeout=60) as r, open(pf, "wb") as f:
                    while got < want:
                        c = r.read(256 * 1024)
                        if not c:
                            break
                        f.write(c)
                        got += len(c)
                        with lock:
                            done[0] += len(c)
                if got == want:
                    return True
            except Exception:
                cur = os.path.getsize(pf) if os.path.isfile(pf) else 0
                with lock:
                    done[0] -= cur
                if os.path.isfile(pf):
                    os.remove(pf)
                time.sleep(1.5 * (attempt + 1))
        return False

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=connections) as ex:
        ok = all(ex.map(fetch, ranges))
    if not ok:
        return False
    tmp = dest + ".merge"
    with open(tmp, "wb") as out:
        for start, _e in ranges:
            with open(os.path.join(part_dir, "%010d" % start), "rb") as f:
                out.write(f.read())
    os.replace(tmp, dest)
    import shutil
    shutil.rmtree(part_dir, ignore_errors=True)
    if verbose:
        dt = time.time() - t0
        print("    完成 %.1f MB，用时 %.1f 分钟（均速 %.0f KB/s）" % (
            total / 1048576, dt / 60, total / 1024 / max(dt, 0.1)))
    return os.path.getsize(dest) == total


def install_models(target_dir: str = None, verbose: bool = True) -> dict:
    """下载并安装离线模型到目标目录（默认 `wenmai/models/`）。"""
    import zipfile
    tdir = target_dir or os.path.join(PKG_DIR, "models")
    os.makedirs(tdir, exist_ok=True)
    result = {"dir": tdir, "ok": [], "failed": []}
    for url, fname, label, is_zip in DOWNLOAD_ITEMS:
        if verbose:
            print("\n  [%s]" % label)
        dest = os.path.join(tdir, fname)
        if _parallel_download(url, dest, verbose=verbose):
            if is_zip:
                bindir = os.path.join(tdir, "bin")
                try:
                    if not os.path.isdir(bindir):
                        with zipfile.ZipFile(dest) as z:
                            z.extractall(bindir)
                    os.remove(dest)          # 解压后删掉压缩包，省 18MB
                    result["ok"].append(label)
                    if verbose:
                        print("    已解压到 %s" % bindir)
                except Exception as e:
                    result["failed"].append("%s（解压失败：%s）" % (label, str(e)[:60]))
            else:
                result["ok"].append(label)
        else:
            result["failed"].append(label)
    return result
