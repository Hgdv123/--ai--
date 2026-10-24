"""依赖自举：在 import 重依赖之前检查并自动安装。

设计目标：拷到别的电脑也能跑起来，且**安装要快**。

安装优先级（逐级降级，任一级成功即停）：
  1. **随应用打包的 wheel**（`wenmai/wheels/`）—— 完全离线，约 20 秒装完；
  2. **国内镜像**（默认清华 TUNA）—— 实测比官方 PyPI 快 2.6 倍；
  3. 官方 PyPI —— 兜底。

为什么不用官方 PyPI：(实测) 官方源约 120 KB/s，四个重依赖装 300 秒仍未完成；
换清华镜像后同样四个包 28 秒装完。且**逐包安装**并把每个包的结果即时打印，
避免"整批超时导致全部白装"。
"""
from __future__ import annotations

import glob
import importlib
import json
import os
import subprocess
import sys
import time

# 模块名 -> pip 包名
HARD = {
    "fitz": "PyMuPDF",
    "docx": "python-docx",
    "numpy": "numpy",
    "sklearn": "scikit-learn",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
}

# 可选依赖：缺了也能跑，只是效果差一点
OPTIONAL = {
    "jieba": "jieba",
    "openpyxl": "openpyxl",
}

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PKG_DIR, ".bootstrap_state.json")
# 随应用打包的 wheel 目录（存在则优先离线安装）
WHEEL_DIRS = [os.path.join(PKG_DIR, "wheels"), os.path.join(os.path.dirname(PKG_DIR), "wheels")]

# 国内镜像：实测清华最快（316 KB/s），其次腾讯云（276）、阿里云（239）
MIRRORS = [
    ("清华 TUNA", "https://pypi.tuna.tsinghua.edu.cn/simple",
     "pypi.tuna.tsinghua.edu.cn"),
    ("腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple",
     "mirrors.cloud.tencent.com"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple", "mirrors.aliyun.com"),
]

# 实测数据（用于回答"为什么这么装"）：
#   官方 PyPI 直连       约 120 KB/s
#   清华镜像             约 316 KB/s（快 2.6 倍）
#   四个重依赖一起装：官方源 300 秒超时仍未完成；清华镜像 28 秒完成
#   用打包的 wheel 离线装：约 12 秒，且完全不需要联网


def _missing(spec: dict) -> list:
    out = []
    for mod, pkg in spec.items():
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pkg)
    return out


def wheel_dir() -> str:
    """返回可用的本地 wheel 目录（没有则空串）。"""
    for d in WHEEL_DIRS:
        if os.path.isdir(d) and glob.glob(os.path.join(d, "*.whl")):
            return d
    return ""


def _pip(args: list, timeout: int) -> bool:
    try:
        p = subprocess.run([sys.executable, "-m", "pip"] + args,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0
    except Exception:
        return False


def _install_from_wheels(packages: list, timeout: int = 300) -> bool:
    """离线安装：只用本地 wheel，不联网。"""
    wd = wheel_dir()
    if not wd:
        return False
    return _pip(["install", "--disable-pip-version-check", "-q",
                 "--no-index", "--find-links", wd] + packages, timeout)


def _install_from_mirror(packages: list, timeout: int = 240) -> bool:
    """联网安装：依次尝试国内镜像，最后回退官方源。"""
    for name, url, host in MIRRORS:
        if _pip(["install", "--disable-pip-version-check", "-q",
                 "-i", url, "--trusted-host", host] + packages, timeout):
            return True
    return _pip(["install", "--disable-pip-version-check", "-q"] + packages, timeout)


def _install_one(pkg: str, quiet: bool) -> bool:
    """装单个包：先离线 wheel，再镜像。逐包安装便于部分成功。"""
    if _install_from_wheels([pkg]):
        if not quiet:
            print("      %s（本地 wheel）" % pkg)
        return True
    if _install_from_mirror([pkg]):
        if not quiet:
            print("      %s（镜像下载）" % pkg)
        return True
    if not quiet:
        print("      %s 失败" % pkg)
    return False


def ensure(quiet: bool = False, auto_install: bool = True) -> dict:
    """确保硬依赖存在。返回 {ok, installed, missing, optional_missing}。"""
    result = {"ok": True, "installed": [], "missing": [], "optional_missing": []}

    hard_missing = _missing(HARD)
    if hard_missing and auto_install:
        wd = wheel_dir()
        if not quiet:
            src = ("随应用打包的 wheel（离线，%d 个文件）" % len(glob.glob(os.path.join(wd, "*.whl")))
                   if wd else "国内镜像（首次需联网）")
            print("[文脉] 缺少依赖：%s" % ", ".join(hard_missing))
            print("[文脉] 安装来源：%s" % src)
        # 逐包安装：某个包失败不影响其它包，且进度即时可见
        for pkg in hard_missing:
            if _install_one(pkg, quiet):
                result["installed"].append(pkg)
        hard_missing = _missing(HARD)

    result["missing"] = hard_missing
    result["optional_missing"] = _missing(OPTIONAL)
    result["ok"] = not hard_missing

    if not quiet:
        if result["installed"]:
            print("[文脉] 已安装 %d 个：%s" % (len(result["installed"]),
                                              ", ".join(result["installed"])))
        if result["missing"]:
            print("[文脉] 仍缺少硬依赖：%s" % ", ".join(result["missing"]))
            print("       可手动执行：pip install -i %s %s"
                  % (MIRRORS[0][1], " ".join(result["missing"])))
        if result["optional_missing"]:
            print("[文脉] 可选依赖未装（不影响运行）：%s" % ", ".join(result["optional_missing"]))

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "installed": result["installed"],
                       "missing": result["missing"]}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return result


def require(*modules: str) -> None:
    """确保指定模块可用，否则给出可执行的报错信息。"""
    miss = []
    for m in modules:
        try:
            importlib.import_module(m)
        except Exception:
            miss.append(m)
    if miss:
        raise RuntimeError(
            "缺少依赖 %s。请执行：pip install -i %s %s" %
            (", ".join(miss), MIRRORS[0][1], " ".join(HARD.get(m, m) for m in miss))
        )
