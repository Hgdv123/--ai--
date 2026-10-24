# -*- coding: utf-8 -*-
"""测试用的样本文件：按需生成，不随包提交二进制。

有 5 个测试脚本需要"一份用户自己的论文稿"和"一份可解析的 PDF"。
以前靠开发机上手工放一份到 `.dsh-tmp/`，交付包里没有，于是这些脚本一跑就
FileNotFoundError。生成比提交二进制更合适：体积为零、内容可读可改。

用法（放在脚本最顶部，早于任何使用点）：

    import _fixtures
    docx_path = _fixtures.docx()
    pdf_path = _fixtures.pdf()

本模块自己负责把包目录加进 sys.path，调用方不需要先 `import _testenv`。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import _testenv  # noqa: E402


def docx() -> str:
    """一份干净的"用户自己的论文稿"：含章节、参考文献区与角标。"""
    return _testenv.fixture_docx()


def docx_with_errors() -> str:
    """一份**故意含引用错误**的稿子，用于验证自检的检出能力。

    与 docx() 分开：干净稿验"不误报"，带错稿验"能检出"，
    混用就分不清漏报与误报。
    """
    return _testenv.fixture_docx_with_errors()


def docx_with_unmatched_ref() -> str:
    """一份引用了"库里没有的文献"的稿子。

    「引用可核实性」只在有条目查不到时才产生：干净稿与带错稿的条目都能在库里
    找到，用它们断言这一项永远不可能通过，所以单独生成一份。
    """
    return _testenv.fixture_docx_with_unmatched_ref()


def pdf() -> str:
    """一份单页、有文字层的 PDF。"""
    return _testenv.fixture_pdf()


if __name__ == "__main__":
    print("docx:", docx())
    print("pdf :", pdf())
