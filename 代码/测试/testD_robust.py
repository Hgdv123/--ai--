# -*- coding: utf-8 -*-
"""全套测试 · 第四批：鲁棒性与边界场景。

覆盖：离线模式全流程、空文献库、损坏数据文件、隐私与合规边界、资源清理。
每项测试后恢复现场，最后统一校验数据未被破坏。
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
import _testenv as ENV  # noqa: E402

PROJ = ENV.activate()
BK = os.path.join(ENV.tmp_dir(), "backup")
R = {"pass": [], "fail": []}


def ck(name, ok, detail=""):
    (R["pass"] if ok else R["fail"]).append({"name": name, "detail": str(detail)[:200]})
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("　→ " + str(detail)[:90]) if detail else ""))


def run(args, env_extra=None, timeout=1200):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, "-X", "utf8", "-m", ENV.PKG] + args,
                          capture_output=True, text=True, cwd=PROJ, env=env,
                          timeout=timeout, encoding="utf-8", errors="ignore")


def restore_data():
    """从备份恢复 data 与 outputs。"""
    for sub in ("data", "outputs"):
        src = os.path.join(BK, sub)
        dst = os.path.join(PROJ, sub)
        if os.path.isdir(src):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
    return True


# ══════════ 0. 备份准备（测试会改动数据，须先备份以便恢复） ══════════
print("=" * 70)
print("0. 备份准备")
print("=" * 70)
if not os.path.isdir(os.path.join(BK, "data")):
    print("  备份不存在，正在创建…")
    os.makedirs(BK, exist_ok=True)
    for sub in ("data", "outputs"):
        src = os.path.join(PROJ, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(BK, sub), dirs_exist_ok=True)
ck("备份目录可用", os.path.isdir(os.path.join(BK, "data")))
_pdfs = [f for f in os.listdir(os.path.join(BK, "data", "pdf")) if f.endswith(".pdf")]
ck("备份含 PDF 文件（%d 个）" % len(_pdfs), len(_pdfs) >= 1)

# ══════════ 1. 离线模式全流程 ══════════
print()
print("=" * 70)
print("1. 离线模式（WM_OFFLINE=1）：全流程应无云端调用")
print("=" * 70)
led_before = json.load(open(os.path.join(HERE, "..", "源程序", ".cache", "cost_ledger.json"),
                            encoding="utf-8")) if os.path.isfile(
    os.path.join(HERE, "..", "源程序", ".cache", "cost_ledger.json")) else {"calls": 0, "cost": 0.0}
print("  （调用前：%d 次 / %.4f 元）" % (led_before.get("calls", 0), led_before.get("cost", 0.0)))

p = run(["all"], env_extra={"WM_OFFLINE": "1"})
out = (p.stdout or "") + (p.stderr or "")
ck("离线 all 正常退出（code=%d）" % p.returncode, p.returncode == 0, out[-200:] if p.returncode else "")
ck("离线时卡片不走云端（本地模型或规则抽取）",
   "模型通道：开启" in out or "本地抽取" in out or "模型通道：关闭" in out,
   [l for l in out.splitlines() if "卡片" in l][:2])
ck("离线时草稿由本地生成（模型或模板）",
   ("通道：模型" in out) or ("本地模板" in out) or ("通道：本地" in out),
   [l for l in out.splitlines() if "草稿" in l][:2])
ck("离线仍完成自检", "自检" in out)
ck("离线仍完成埋错测试", "埋错测试" in out, [l for l in out.splitlines() if "埋错" in l][:1])
ck("离线仍完成导出", "导出" in out)

led_after = json.load(open(os.path.join(HERE, "..", "源程序", ".cache", "cost_ledger.json"),
                           encoding="utf-8"))
ck("离线运行未产生任何云端费用（%.4f → %.4f 元）" % (led_before.get("cost", 0), led_after.get("cost", 0)),
   abs(led_after.get("cost", 0) - led_before.get("cost", 0)) < 1e-9,
   led_after.get("cost"))

# 离线时向量后端
from wenmai.embeddings import Embedder
os.environ["WM_OFFLINE"] = "1"
em = Embedder()
em.encode(["离线测试"])
ck("离线向量后端为本地（模型或 n-gram）", em.backend.startswith("local"), em.backend)
os.environ.pop("WM_OFFLINE", None)

# ══════════ 2. 云端产物不得含失实内容 ══════════
print()
print("=" * 70)
print("2. 离线产物质量：不得把英文原文当结果")
print("=" * 70)
draft_txt = open(os.path.join(PROJ, "outputs", "draft.md"), encoding="utf-8").read()
ck("离线草稿无英文原文长句",
   not any(s in draft_txt for s in ("We propose an algorithm", "Related Work The",
                                    "In this paper we", "extensive experiments")),
   [s for s in ("We propose an algorithm", "Related Work The") if s in draft_txt])
ck("离线草稿含参考文献表", "参考文献" in draft_txt)
ck("离线草稿含中文说明", any("\u4e00" <= c <= "\u9fff" for c in draft_txt))

# ══════════ 3. 空文献库 ══════════
print()
print("=" * 70)
print("3. 空文献库：不应崩溃，应给出可理解的提示")
print("=" * 70)
cpath = os.path.join(PROJ, "data", "corpus.json")
shutil.copy2(cpath, cpath + ".keep")
with open(cpath, "w", encoding="utf-8") as f:
    json.dump({"query": "", "count": 0, "records": []}, f, ensure_ascii=False)
for f in ("docs.json", "cards.json", "matrix.json"):
    fp = os.path.join(PROJ, "data", f)
    if os.path.isfile(fp):
        os.remove(fp)

p = run(["status"])
ck("空库时 status 不崩", p.returncode == 0, (p.stderr or "")[-120:])
p = run(["check"])
ck("空库时 check 不崩（给出提示）", p.returncode == 0, (p.stderr or "")[-150:])
p = run(["doctor"])
ck("空库时 doctor 不崩", p.returncode == 0, (p.stderr or "")[-120:])

shutil.move(cpath + ".keep", cpath)

# ══════════ 4. 损坏的数据文件 ══════════
print()
print("=" * 70)
print("4. 数据文件损坏：应降级而不是崩溃")
print("=" * 70)
with open(cpath, "w", encoding="utf-8") as f:
    f.write("{ 这不是合法 JSON")
p = run(["status"])
ck("corpus.json 损坏时 status 不崩", p.returncode == 0, (p.stderr or "")[-150:])
restore_data()

with open(cpath, "w", encoding="utf-8") as f:
    json.dump({"query": "", "count": 0, "records": "不是列表"}, f, ensure_ascii=False)
p = run(["status"])
ck("records 类型错误时不崩", p.returncode == 0, (p.stderr or "")[-150:])
restore_data()

# privacy.json 损坏
pf = os.path.join(PROJ, "data", "privacy.json")
orig_pf = open(pf, encoding="utf-8").read() if os.path.isfile(pf) else None
with open(pf, "w", encoding="utf-8") as f:
    f.write("@@@ 坏文件")
import importlib
import wenmai.app as APP
importlib.reload(APP)
pv = APP._load_privacy()
ck("privacy.json 损坏时回落默认值", pv["mode"] == "cloud" and pv["acknowledged"] is False, pv)
if orig_pf is not None:
    with open(pf, "w", encoding="utf-8") as f:
        f.write(orig_pf)

# ══════════ 5. 没有 API Key ══════════
print()
print("=" * 70)
print("5. 无 API Key：应自动降级为本地，不报错")
print("=" * 70)
env_nokey = {"DASHSCOPE_API_KEY": "", "WM_OFFLINE": "0"}
p = run(["all"], env_extra=env_nokey)
out = (p.stdout or "") + (p.stderr or "")
ck("无 Key 时 all 正常完成（code=%d）" % p.returncode, p.returncode == 0, out[-200:])
ck("无 Key 时不走云端（本地模型或规则抽取）",
   ("模型通道：开启" in out) or ("本地抽取" in out) or ("模型通道：关闭" in out),
   [l for l in out.splitlines() if "卡片" in l][:2])
ck("无 Key 时无 Traceback", "Traceback" not in out)

# ══════════ 6. 并发与重复调用 ══════════
print()
print("=" * 70)
print("6. 重复运行与并发：结果应一致且不互相破坏")
print("=" * 70)
p1 = run(["draft"])
h1 = open(os.path.join(PROJ, "outputs", "draft.md"), encoding="utf-8").read()
p2 = run(["draft"])
h2 = open(os.path.join(PROJ, "outputs", "draft.md"), encoding="utf-8").read()
ck("连续两次 draft 结果一致（可复现）", h1 == h2,
   "长度 %d vs %d" % (len(h1), len(h2)))

# ══════════ 7. 隐私边界（合规） ══════════
print()
print("=" * 70)
print("7. 隐私与合规边界")
print("=" * 70)
# 全库搜索明文密钥
KEY = ""
try:
    for line in open(os.path.join(PROJ, ".env"), encoding="utf-8"):
        if line.strip().startswith("DASHSCOPE_API_KEY"):
            KEY = line.split("=", 1)[1].strip()
except Exception:
    pass
ck(".env 中读到了密钥（用于检查泄露）", bool(KEY) and KEY.startswith("sk-"), KEY[:8] + "…")

# 交付包的 .env 是占位符，拿它当"密钥"去全库搜，命中的只会是文档里解释占位符的
# 那几句话（config.py / llm.py 的注释），不是泄露。没有真 Key 时这一项不可判定，
# 明说"跳过"，不伪造一个通过。
PLACEHOLDER = "sk-在此填入你的百炼APIKey"
if not KEY or not KEY.startswith("sk-") or KEY == PLACEHOLDER:
    print("  ⏭️  跳过明文密钥扫描：.env 是占位符，包里没有真实 Key 可查")
else:
    leaks = []
    for root, dirs, files in os.walk(PROJ):
        if any(x in root for x in (".git", ".dsh-tmp", "node_modules", "__pycache__", ".cache")):
            continue
        for fn in files:
            if fn.endswith((".py", ".html", ".md", ".json", ".bat", ".ps1", ".toml", ".example")):
                fp = os.path.join(root, fn)
                try:
                    if KEY in open(fp, encoding="utf-8", errors="ignore").read():
                        leaks.append(os.path.relpath(fp, PROJ))
                except Exception:
                    pass
    ck("代码与文档中无明文密钥泄露", not leaks, leaks)

_gi = os.path.join(PROJ, ".gitignore")
ck(".env 已在 .gitignore 中",
   os.path.isfile(_gi) and ".env" in open(_gi, encoding="utf-8").read(),
   _gi if not os.path.isfile(_gi) else "")

# 离线模式设置会持久化并生效
APP._save_privacy({"mode": "local", "acknowledged": True, "acknowledged_at": "x"})
importlib.reload(APP)
os.environ["WM_OFFLINE"] = "1"
APP._apply_saved_privacy()
from wenmai import config as C
ck("保存 local 后重启仍为离线", C.settings.force_offline is True,
   "force_offline=%s WM_OFFLINE=%r" % (C.settings.force_offline, os.environ.get("WM_OFFLINE")))
APP._save_privacy({"mode": "cloud", "acknowledged": True, "acknowledged_at": "x"})
os.environ["WM_OFFLINE"] = "1"          # 模拟"启动时环境仍残留离线标记"
APP._apply_saved_privacy()
ck("改回 cloud 后恢复在线能力", C.settings.force_offline is False,
   "force_offline=%s WM_OFFLINE=%r 载入的mode=%s" % (
       C.settings.force_offline, os.environ.get("WM_OFFLINE"),
       APP._load_privacy().get("mode")))

# ══════════ 8. 资源清理与恢复 ══════════
print()
print("=" * 70)
print("8. 恢复现场并校验数据完整")
print("=" * 70)
restore_data()
corpus = json.load(open(cpath, encoding="utf-8"))
ck("文献库恢复为 6 篇", corpus["count"] == 6, corpus["count"])
ck("PDF 文件齐全 6 个",
   len([f for f in os.listdir(os.path.join(PROJ, "data", "pdf")) if f.endswith(".pdf")]) == 6)
p = run(["doctor"])
ck("恢复后 doctor 正常", p.returncode == 0)

# ══════════ 汇总 ══════════
print()
print("=" * 70)
print("第四批汇总：通过 %d，失败 %d" % (len(R["pass"]), len(R["fail"])))
print("=" * 70)
if R["fail"]:
    print("失败项：")
    for x in R["fail"]:
        print("  ✗ %s　→ %s" % (x["name"], x["detail"]))
with open(os.path.join(ENV.tmp_dir(), "testD_result.json"), "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=2)
