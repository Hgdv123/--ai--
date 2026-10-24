# -*- coding: utf-8 -*-

# ── 路径与样本文件：一律由仓库自带的测试工具推导，勿写死 ──
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
import _testenv as E
import _fixtures
HERE = _HERE          # 供 os.path.join(HERE, "..", "源程序", ...) 使用
PROJ = E.activate()
import io, json, os, re, subprocess, sys, time
import snapshot

SUITES = [
    ("1 纯函数层", "testA_modules.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("2 数据处理层", "testB_pipeline.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("3 接口与命令行", "testC_api.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("4 鲁棒性与安全", "testD_robust.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("5 新功能专项", "test_new_features.py", r"汇总：通过 (\d+) 项，失败 (\d+) 项"),
    ("6 内容校验专项", "test_verify_content.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("7 数据持久化专项", "test_persistence.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("8 接口模糊测试", "fuzz_api.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("9 稿子模式", "test_mydraft_api.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("10 边界与一致性", "fuzz_edge.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("11 清空专项", "test_clear.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("12 空库启动专项", "test_empty_start.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("13 全流程四套旅程", "fulltest_A.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("14 四态判定专项", "test_claim_verdict.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("15 证据溯源专项", "test_evidence_source.py", r"汇总：通过 (\d+)，失败 (\d+)"),
    ("16 结论净化专项", "test_agent_summary.py", r"汇总：通过 (\d+)，失败 (\d+)"),
]
# 路径一律相对**脚本自身目录**（HERE）。原来写的是旧布局的 tests/ 与 .dsh-tmp/，
# 现在所有脚本都在 代码/测试/ 下；旧路径让子进程直接找不到文件，
# 整批显示 "pass 0 fail ?"（实测踩到）。
env = dict(os.environ)
env["PYTHONPATH"] = PROJ
env["PYTHONIOENCODING"] = "utf-8"
env["WM_OFFLINE"] = "0"
# 每个套件都用全新服务：连跑时避免上一个套件残留的服务状态造成偶发失败
env["WM_TEST_FRESH"] = "1"
snapshot.snapshot()
rows, tp, tf = [], 0, 0
t0 = time.time()
for name, rel, pat in SUITES:
    path = os.path.join(HERE, rel.replace("/", os.sep))
    st = time.time()
    try:
        out = subprocess.run([sys.executable, "-X", "utf8", path], cwd=PROJ, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=3600).stdout.decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        out = ""
    m = re.search(pat, out)
    p, f = (int(m.group(1)), int(m.group(2))) if m else (0, -1)
    tp += max(p, 0)
    tf += max(f, 0) + (1 if f == -1 else 0)
    rows.append({"name": name, "pass": p, "fail": f, "seconds": round(time.time() - st, 1)})
    print("%-18s pass %3d  fail %-4s %6.1fs" % (name, p, f if f >= 0 else "?", rows[-1]["seconds"]))
    # 失败时把该套件的完整输出落盘。
    # 为什么需要：此前只记 pass/fail 计数，导致"整批跑红、单独跑绿"的套件
    # 永远查不到是哪条断言、为什么失败——同一现象出现过多次，每次都只能猜。
    # 现在只要有失败，输出就留在 _fail_<套件>.log 里。
    if f != 0:
        safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", name)
        logp = os.path.join(E.tmp_dir(), "_fail_%s.log" % safe)
        with io.open(logp, "w", encoding="utf-8") as lf:
            lf.write("套件：%s\n命令：%s\n结果：pass %s fail %s（%.1fs）\n\n%s"
                     % (name, rel, p, f, rows[-1]["seconds"], out))
        print("      ↳ 失败详情已存 .dsh-tmp/_fail_%s.log" % safe)
    sys.stdout.flush()
    snapshot.restore(verbose=False)
print()
print("=" * 60)
print("TOTAL pass %d  fail %d  (%.1f min)" % (tp, tf, (time.time() - t0) / 60))
print("=" * 60)
with io.open(os.path.join(E.tmp_dir(), "_all_tests_result.json"), "w", encoding="utf-8") as f:
    json.dump({"rows": rows, "total_pass": tp, "total_fail": tf,
               "minutes": round((time.time() - t0) / 60, 1)}, f, ensure_ascii=False, indent=2)

# ── 跑完自己收尾 ──────────────────────────────────────────────
# 测试过程中会生成一批临时件（上传测试用的 _bad.txt/_up.docx、各套件的
# *_result.json、矩阵导出的 _test_matrix.xlsx）。它们每次运行都会重建，
# 留在磁盘上只会让"哪些是交付物、哪些是测试垃圾"变得难以分辨——
# 实测我为此手工清理了三轮，而每跑一次测试又全部回来。
# 所以让跑测试的人（这个脚本）自己负责收拾：只删这些明确的临时件，
# 测试脚本、测试素材、快照一律不动。
print()
print("收尾：清理本次运行产生的临时件")
_junk = ["_bad.txt", "_up.docx", "testA_result.json", "testB_result.json",
         "testC_result.json", "testD_result.json", "_test_matrix.xlsx"]
for n in _junk:
    p = os.path.join(E.tmp_dir(), n)
    if os.path.isfile(p):
        os.remove(p)
        print("  删除 .dsh-tmp/" + n)
# 快照目录是下次运行的基线，保留；但它里面不该有测试垃圾
_snap = os.path.join(E.tmp_dir(), "snapshot")
if os.path.isdir(_snap):
    print("  保留 .dsh-tmp/snapshot（下次运行的还原基线）")