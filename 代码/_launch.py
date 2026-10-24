# -*- coding: utf-8 -*-
"""文脉一键启动器。

放在包根或「代码」子目录里都能用：
    包根/启动文脉.bat  →  代码/_launch.py  →  代码/源程序/
    或直接 代码/_launch.py

做五件事：
    1. 找到程序包（判据：目录里有 __main__.py，且该目录不在数据/文档目录里）
    2. 把包目录的父目录加进 sys.path
    3. 指定数据与产物的位置（WM_ROOT）与配置文件（WM_ENV_FILE）
    4. 检查依赖，缺失则用随包 wheels 离线安装
    5. 启动服务（等同 python -m <包名> demo）

为什么这些逻辑在 Python 而不是批处理里：
    批处理捕获 Python 输出的中文路径会因控制台代码页转换而失败；
    而且 .bat 本身的编码由 cmd 按系统代码页解释，写中文注释会被当成命令执行。
    所以批处理只保留 ASCII 的一行调用，具体逻辑都在这里。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = "__main__.py"
# 这些目录里不会放程序包，遍历时跳过（数据目录含大量文件，跳过可提速）
SKIP_DIRS = {"models", "wheels", "__pycache__", ".cache", ".git", "node_modules",
             "数据与产物", "文档与参考", "data", "outputs", "docs"}


def find_package(root: str):
    """找出含 __main__.py 的包目录，返回 (父目录, 包名)；取路径最短者。"""
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if MARKER in filenames and "__init__.py" in filenames:
            hits.append(dirpath)
    if not hits:
        return None
    hits.sort(key=len)
    pkg = hits[0]
    return os.path.dirname(pkg), os.path.basename(pkg)


def resolve_layout() -> tuple:
    """确定 (代码根, 项目根, 包父目录, 包名)。

    支持的摆放：
        <项目根>/代码/源程序/    ← 交付包的分组布局
        <项目根>/代码/wenmai/    ← 代码目录内包名保持英文
        <项目根>/wenmai/         ← 直接跑源码
    """
    found = find_package(HERE)
    if not found:
        return None
    pkg_parent, pkg_name = found

    # 代码根：包目录若叫「源程序」这类非包名，说明它上面还有一层代码目录
    code_root = os.path.dirname(pkg_parent)
    # 项目根：本脚本上方的 pyproject.toml 所在处，或再上一层
    if os.path.isfile(os.path.join(HERE, "pyproject.toml")):
        code_root = HERE
        project_root = os.path.dirname(HERE)
    else:
        project_root = os.path.dirname(HERE)
    return code_root, project_root, pkg_parent, pkg_name


def main() -> int:
    layout = resolve_layout()
    if not layout:
        print("[ERROR] 找不到程序包（未找到含 __main__.py 的目录）。")
        print("        请确认解压后目录结构完整，未被拆散。")
        return 1
    code_root, project_root, pkg_parent, pkg_name = layout

    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)

    # 数据与产物：优先用「数据与产物」分组目录，没有则用项目根下的 data/outputs
    grouped = os.path.join(project_root, "数据与产物")
    data_home = grouped if os.path.isdir(os.path.join(grouped, "data")) else project_root
    os.environ["WM_ROOT"] = data_home
    env_file = os.path.join(code_root, ".env")
    if not os.path.isfile(env_file):
        env_file = os.path.join(project_root, ".env")
    if os.path.isfile(env_file):
        os.environ["WM_ENV_FILE"] = env_file

    print("Package : %s" % os.path.join(pkg_parent, pkg_name))
    print("Data    : %s" % os.path.join(data_home, "data"))
    print("Outputs : %s" % os.path.join(data_home, "outputs"))
    print()

    # ── 依赖 ───────────────────────────────────────────────────
    if os.path.isdir(os.path.join(pkg_parent, pkg_name, "wheels")):
        print("[1/3] Checking dependencies (offline wheels included, ~15s)...")
    else:
        print("[1/3] Checking dependencies (will fetch from mirror on first run)...")
    sys.stdout.flush()
    try:
        import importlib
        boot = importlib.import_module("%s.bootstrap" % pkg_name)
        r = boot.ensure(quiet=False)
        if not r.get("ok"):
            print()
            print("[ERROR] 依赖安装失败。可手动执行：")
            print("        python -m pip install PyMuPDF python-docx numpy "
                  "scikit-learn fastapi uvicorn jinja2 openpyxl jieba")
            return 1
    except Exception as e:
        print("[ERROR] 依赖检查失败：%s" % e)
        return 1

    # ── 启动 ───────────────────────────────────────────────────
    print()
    print("[2/3] Stopping any previous WenMai service...")
    sys.stdout.flush()
    try:
        importlib.import_module("%s.__main__" % pkg_name).main(["stop"])
    except SystemExit:
        pass
    except Exception:
        pass

    print("[3/3] Starting...")
    print()
    print("-" * 60)
    print(" The browser will open automatically.")
    print(" Press Ctrl+C in this window to stop.")
    print("-" * 60)
    print()
    sys.stdout.flush()

    argv = sys.argv[1:]
    if not argv or argv[0] not in ("demo",):
        argv = ["demo"] + argv
    try:
        importlib.import_module("%s.__main__" % pkg_name).main(argv)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
