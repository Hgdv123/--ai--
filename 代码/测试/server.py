# -*- coding: utf-8 -*-
"""测试用的服务管理：需要时自动起一个，跑完自动关。

为什么要有它：所有 HTTP 层测试（testC_api、fuzz_api、fuzz_edge、
test_mydraft_api、fulltest_A、test_new_features）此前都**默默假设** 8848
端口上已经有人在跑服务。于是：

  · 单独跑某个测试 → 满屏 ConnectionRefused，被误当成"产品坏了"
  · 批量跑时恰好有残留进程 → 测试连到的是**上一次的旧进程**，结论失真
  · 旧进程用的还是旧代码，改了源码却"没生效"

用法：
    import server
    srv = server.ensure(force_own=True)   # 改过源码时必须 force_own
    try:
        ... 测试 ...
    finally:
        srv.stop()                        # 只关自己起的，别人的不动
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 包名与项目根都可能被改名/搬移，这里探测而不是写死：
# 写死过一次（wenmai → 源程序 改名后，起服务的命令仍叫 -m wenmai，
# 于是所有 HTTP 套件都报"服务未能启动"）。
PKG = next((d for d in ("源程序", "wenmai")
            if os.path.isfile(os.path.join(PROJ, d, "__main__.py"))), "源程序")
PORT = int(os.environ.get("WM_TEST_PORT", "8848"))
BASE = "http://127.0.0.1:%d" % PORT


def reachable(timeout: float = 2.0) -> bool:
    try:
        urllib.request.urlopen(BASE + "/api/state", timeout=timeout).read()
        return True
    except Exception:
        return False


def _port_free() -> bool:
    """端口真的空着吗（比"HTTP 不通"更严格，能识别半个死进程占着端口）。"""
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", PORT))
        return False
    except Exception:
        return True
    finally:
        s.close()


def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        return r.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def _pids_on_port() -> list:
    """占用本端口的进程 PID。

    原先用 wmic，但 **wmic 已被 Windows 移除**（实测本机 CommandNotFound），
    于是清理函数静默杀掉 0 个进程 —— 一个跑着旧代码的服务会一直霸占端口，
    让所有 HTTP 测试连到旧进程，"改了代码却不生效"，排查了非常久。
    这里改用 netstat（所有 Windows 都有）作为主路径。
    """
    pids = []
    out = _run(["netstat", "-ano", "-p", "TCP"])
    for line in out.splitlines():
        parts = line.split()
        # 形如：TCP  127.0.0.1:8848  0.0.0.0:0  LISTENING  12345
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":%d" % PORT):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return sorted(set(pids))


def _kill(pid: int) -> bool:
    _run(["taskkill", "/F", "/T", "/PID", str(pid)])
    return True


def kill_stale(verbose: bool = False) -> int:
    """杀掉占用测试端口的进程，并等到端口空闲。返回杀掉的个数。"""
    killed = 0
    for pid in _pids_on_port():
        if pid == os.getpid():
            continue
        if verbose:
            print("  [服务] 端口 %d 被 PID %d 占用，终止它" % (PORT, pid))
        _kill(pid)
        killed += 1
    for _ in range(30):
        if _port_free():
            break
        for pid in _pids_on_port():
            if pid != os.getpid():
                _kill(pid)
        time.sleep(1)
    return killed


class Server:
    def __init__(self, proc=None, mine: bool = False):
        self.proc = proc
        self.mine = mine

    def stop(self) -> None:
        if not self.mine or self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=10)
        # 端口上可能还有子进程残留，一并清掉
        kill_stale()
        print("  [服务] 已停止（本次测试启动的那个）")


def _running_code_mtime():
    """已运行的服务所加载的 app.py 修改时间；取不到返回 None。"""
    try:
        import json
        with urllib.request.urlopen(BASE + "/api/state", timeout=4) as r:
            d = json.loads(r.read().decode("utf-8"))
        return float(d.get("code_mtime") or 0.0)
    except Exception:
        return None


def _local_code_mtime() -> float:
    try:
        return os.path.getmtime(os.path.join(PROJ, PKG, "app.py"))
    except Exception:
        return 0.0


def _env_force_fresh() -> bool:
    """连跑多个套件时，用 WM_TEST_FRESH=1 让每个套件都从**全新服务**开始。

    为什么需要它：连跑时上一个套件留下的服务进程可能在内存里保留了状态
    （缓存、环境变量、隐私模式），下一个套件就连到它，偶发地测出与单独
    运行不一致的结果。单独跑全绿、连跑偶发红，排查成本很高。
    设了这个环境变量就每个套件重启一次，牺牲几十秒换取确定性的环境。
    """
    return os.environ.get("WM_TEST_FRESH", "") not in ("", "0", "false", "False")


def ensure(verbose: bool = True, force_own: bool = False) -> Server:
    """确保有一个**用当前代码**跑起来的服务。

    默认策略（force_own=False）：
      · 端口上已有服务，且它加载的代码不比本地旧 → 直接复用
      · 已有服务但代码是旧的 → 只重启它（这正是"改了源码却测到旧行为"的根因）
      · 端口空闲 → 起一个新的

    注意**不要**无条件杀掉端口上的一切：那会连带杀掉别人正在用的服务
    （另一个测试、或调试时开着日志的实例），对方会收到 ConnectionResetError，
    看起来像产品崩溃。实测为此误判过一轮。
    """
    if not force_own and not _env_force_fresh() and reachable():
        run_mt = _running_code_mtime()
        local_mt = _local_code_mtime()
        # 本地 app.py 比"服务开始运行时的版本"新 → 源码改过了，必须重启。
        # 允许 1.5 秒的写入误差，避免刚启动就被误判为旧。
        if run_mt is not None and local_mt > run_mt + 1.5:
            if verbose:
                print("  [服务] 在跑的服务是旧代码（服务 %.0f < 本地 %.0f），重启它"
                      % (run_mt, local_mt))
            kill_stale(verbose=verbose)
        else:
            if verbose:
                print("  [服务] 复用已在 %d 上运行的服务（代码为最新）" % PORT)
            return Server(None, mine=False)

    if not _port_free():
        kill_stale(verbose=verbose)

    env = dict(os.environ)
    env["PYTHONPATH"] = PROJ
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("WM_OFFLINE", "0")
    # 被测服务的数据与产物写进当前沙箱（WM_ROOT），源码仍从包目录加载。
    # 必须显式覆盖而不是 setdefault：环境里可能已经有一个指向别处的 WM_ROOT，
    # 而服务恰恰要看到沙箱才读得到测试用的数据。不覆盖的话测试会直接写进
    # 交付包的 outputs/，评测产物就不再是交付的那一份。
    try:
        import _testenv as _env
        env["WM_ROOT"] = _env.sandbox_root()
    except Exception:
        pass
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", PKG, "serve", "--port", str(PORT)],
        cwd=PROJ, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(90):
        time.sleep(1)
        if reachable(3):
            if verbose:
                print("  [服务] 已启动（PID %d，端口 %d）" % (proc.pid, PORT))
            return Server(proc, mine=True)
    proc.kill()
    raise RuntimeError("服务未能启动（端口 %d）" % PORT)


if __name__ == "__main__":
    print("端口 %d 占用者 PID: %s" % (PORT, _pids_on_port() or "无"))
    s = ensure(force_own=True)
    print("BASE =", BASE, "| 由本次启动 =", s.mine)
    s.stop()
    print("收尾后端口空闲:", _port_free())
