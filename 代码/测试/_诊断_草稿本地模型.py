# -*- coding: utf-8 -*-
"""查清：离线草稿阶段为什么说"本地模型生成失败"。

local_llm.chat() 用 `except Exception: return ""` 吞掉了真实原因，
这里把异常原样抓出来，并量一下提示词规模（大提示词可能超出上下文）。
"""
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _testenv as E  # noqa: E402

E.activate()
from 源程序 import local_llm as L  # noqa: E402

print("=" * 70)
print("环境")
print("  生成可用        :", L.available("gen"))
print("  GEN_PORT        :", L.GEN_PORT)
print("  GEN_CTX         :", L.GEN_CTX)
print("  GEN_SLOTS       :", L.GEN_SLOTS)
print("  每槽上下文      :", L.GEN_CTX // max(L.GEN_SLOTS, 1))
print("  生成模型        :", os.path.basename(L.gen_model_path()))
print("=" * 70, flush=True)

port = L._start("gen")
print("_start 端口      :", port)
if not port:
    raise SystemExit("服务未启动")


def raw_completion(prompt: str, n_predict: int = 200, timeout: int = 1800):
    """原样发一次 /completion，把异常完整抛出（不吞）。"""
    wrapped = ("<|im_start|>system\n你是一个学术写作助手，只输出要求的内容，"
               "不要重复题目，不要自问自答。<|im_end|>\n"
               "<|im_start|>user\n%s<|im_end|>\n<|im_start|>assistant\n" % prompt)
    body = json.dumps({"prompt": wrapped, "n_predict": n_predict,
                       "temperature": 0.3, "stream": False,
                       "stop": list(L.STOP_MARKERS), "repeat_penalty": 1.12}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/completion" % port, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d, time.time() - t0, len(wrapped)


print("-" * 70)
print("1. 短提示词（走真实路径 local_llm.chat，含重试）")
t0 = time.time()
out = L.chat("用一句话说明什么是小样本学习。", max_tokens=80)
print("   %.1f 秒，返回 %d 字符" % (time.time() - t0, len(out)))
print("   →", repr(out[:150]))
print("   结论：", "本地模型可用 ★" if out else "❌ 仍拿不到输出")

print("-" * 70)
print("1b. 裸请求（不重试）看首次是否仍撞 503")
try:
    d, dt, n = raw_completion("用一句话说明什么是小样本学习。", 60)
    print("   OK  %.1f 秒" % dt)
except urllib.error.HTTPError as e:
    print("   HTTP %d（说明就绪与可用之间确有一段窗口，靠重试覆盖）" % e.code)
except Exception as e:
    print("   %s: %s" % (type(e).__name__, str(e)[:80]))

print("-" * 70)
print("2. 草稿阶段真实提示词（6 篇卡片）")
from 源程序 import draft as D  # noqa: E402

# 找出 draft 构造提示词的地方，尽量取到与生产一致的文本
prompt = None
for name in ("_build_prompt", "build_prompt", "_prompt", "_draft_prompt"):
    fn = getattr(D, name, None)
    if callable(fn):
        try:
            for args in ((), ):
                prompt = fn(*args)
            print("   用 %s() 取到提示词" % name)
            break
        except Exception as e:
            print("   %s() 调用失败：%s" % (name, str(e)[:80]))
if prompt is None:
    try:
        from 源程序.cards import load_cards
        cards = load_cards()
        print("   卡片数:", (cards or {}).get("count") if isinstance(cards, dict) else len(cards))
    except Exception as e:
        print("   取卡片失败:", str(e)[:80])
    print("   未找到可调用的提示词构造函数，改为量实际生成时的提示词")
    # 直接在 generate 里打点
    import 源程序.draft as DM
    orig = L.chat
    seen = {}


    def spy(p, *a, **k):
        seen["prompt"] = p
        seen["max_tokens"] = k.get("max_tokens") or (a[0] if a else None)
        return orig(p, *a, **k)


    L.chat = spy
    DM.local_llm.chat = spy
    try:
        DM.generate(use_llm=True, verbose=True)
    except Exception as e:
        print("   generate 抛错:", str(e)[:120])
    finally:
        L.chat = orig
        DM.local_llm.chat = orig
    if seen.get("prompt"):
        prompt = seen["prompt"]
        print("   实际提示词 %d 字符，max_tokens=%s" % (len(prompt), seen.get("max_tokens")))

if prompt:
    print("-" * 70)
    print("3. 用真实提示词调用（重点）")
    mt = seen.get("max_tokens") if 'seen' in dir() else 1200
    mt = int(mt or 1200)
    print("   提示词 %d 字符，请求 max_tokens=%d" % (len(prompt), mt))
    try:
        d, dt, n = raw_completion(prompt, mt)
        print("   OK  %.1f 秒" % dt)
        print("   →", repr((d.get("content") or "")[:200]))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:400]
        print("   FAILED HTTP %d" % e.code)
        print("   服务返回:", body)
    except Exception:
        print("   FAILED")
        traceback.print_exc()
