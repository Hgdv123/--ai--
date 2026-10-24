# -*- coding: utf-8 -*-
"""测试用的路径定位与隔离。

为什么需要这个文件：测试脚本里曾经把项目根写死成 `F:\deepseek harness`。
开发目录被删除、交付包改名（wenmai → 源程序，项目根也挪进「代码/」）之后，
这些脚本全部报 No module named 'wenmai' / 路径不存在——测试与代码一起交付，
却跑不起来，等于没有测试。

规则：
  · 项目根、包名一律探测，不写死；
  · 临时文件写进 `输出目录()/.dsh-tmp`；
  · 数据与产物默认写进**隔离目录**，源数据只读复制一份进去。
    否则测试会直接覆盖交付包的 outputs/（评测结果、Word、Excel 全在里面），
    交付物就不再是评测时的那一份了。
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # …/代码/测试
ROOT = os.path.dirname(HERE)                               # …/代码


def pkg_name() -> str:
    """源码包目录名（改名后也不用改测试）。"""
    for name in ("源程序", "wenmai"):
        if os.path.isfile(os.path.join(ROOT, name, "__main__.py")):
            return name
    raise RuntimeError("在 %s 下找不到源码包（应有 __main__.py）" % ROOT)


PKG = pkg_name()


class _AliasFinder(importlib.abc.MetaPathFinder):
    """把旧包名 `wenmai.*` 的导入重定向到当前包名（如 `源程序.*`）。

    测试脚本里到处是 `from wenmai import ...`。包目录改名成「源程序」是为了让
    评委一眼认出源码，但没必要为此重写全部测试——这里统一做重定向。
    """

    def __init__(self, alias: str, target: str):
        self.alias = alias
        self.target = target

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.alias and not fullname.startswith(self.alias + "."):
            return None
        real = self.target + fullname[len(self.alias):]
        try:
            real_spec = importlib.util.find_spec(real)
        except Exception:
            return None
        if real_spec is None:
            return None
        # 让 `wenmai.x` 直接复用 `源程序.x` 已加载的模块对象，
        # 避免同一份代码被加载两次（两份 settings 会各自持有一份状态）
        mod = importlib.import_module(real)
        sys.modules[fullname] = mod
        return importlib.util.spec_from_loader(fullname, _AliasLoader(mod))


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, mod):
        self.mod = mod

    def create_module(self, spec):
        return self.mod

    def exec_module(self, module):
        pass


def _install_alias() -> None:
    if PKG == "wenmai":
        return
    if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder("wenmai", PKG))


def use_package_path() -> str:
    """把项目根加进 sys.path，使 `import <PKG>` 可用；返回项目根。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    _install_alias()
    return ROOT


def outputs_dir() -> str:
    """当前生效的 outputs 目录（受 WM_ROOT 影响）。"""
    return os.path.join(sandbox_root(), "outputs")


def tmp_dir() -> str:
    """临时文件目录；从 outputs 的父目录派生，保证与沙箱一致。"""
    d = os.path.join(os.path.dirname(outputs_dir()), ".dsh-tmp")
    os.makedirs(d, exist_ok=True)
    return d


def fixture_docx() -> str:
    """按需生成一份"用户自己的论文稿"docx，返回路径。

    有 5 个测试脚本要用它（上传、删除、fuzz 上传、mydraft 接口…）。
    以前靠开发机上手工放一份到 `.dsh-tmp/`，交付包里没有 → 这些脚本一跑就
    FileNotFoundError。**生成**比提交二进制更合适：体积为零、内容可读可改、
    且不会把某次调试用的样本固化进仓库。

    内容刻意包含**章节标题**（摘要/引言/方法/实验/结论）与"参考文献"区，
    否则上传后解析不出章节、也识别不到文献表，检查项会大面积落空
    （实测：只写两三段普通文字时，test_mydraft_api 有 9 项失败）。
    """
    path = os.path.join(tmp_dir(), "我的论文初稿.docx")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    from docx import Document
    doc = Document()
    doc.add_heading("基于元学习的少样本图像分类方法研究", level=1)
    doc.add_heading("摘要", level=2)
    doc.add_paragraph(
        "少样本分类要求在每类只有少量标注样本时仍能取得可用精度。"
        "本文研究元学习在该场景下的适应效率问题，并在公开基准上做了对比实验。")
    doc.add_heading("1 引言", level=2)
    doc.add_paragraph(
        "少样本分类的核心困难在于标注数据稀缺[9]。"
        "元学习通过在任务分布上学习先验，使模型能快速适应新任务[2]。")
    doc.add_heading("2 相关工作", level=2)
    doc.add_paragraph(
        "基于梯度的元学习方法通过优化初始参数来获得快速适应能力[1]。"
        "在此基础上，有工作把学习率与更新方向也纳入优化[2]。")
    doc.add_heading("3 方法", level=2)
    doc.add_paragraph(
        "本文沿用任务分布的设定，在每轮任务中联合利用有标签与无标签样本，"
        "以降低对标注量的依赖[3]。")
    doc.add_heading("4 实验", level=2)
    doc.add_paragraph(
        "实验在 mini-ImageNet 上完成，与基线方法做了对比[3]。"
        "结果表明，联合建模初始化与优化过程可以提升适应效率[2]。")
    doc.add_heading("5 结论", level=2)
    doc.add_paragraph(
        "综合已有工作可以看出，元学习方法在小样本条件下的总体表现仍然受到"
        "任务分布差异与标注成本的共同约束，这一判断在现有公开基准上是普遍成立的。")
    doc.add_heading("参考文献", level=2)
    doc.add_paragraph("[1] Finn C, Abbeel P, Levine S. "
                      "Model-Agnostic Meta-Learning for Fast Adaptation of Deep Networks[C]. 2017.")
    doc.add_paragraph("[2] Li Z, Zhou F, Chen F, et al. "
                      "Meta-SGD: Learning to Learn Quickly for Few-Shot Learning[J]. 2017.")
    doc.add_paragraph("[2] Li Z, Zhou F, Chen F, et al. "
                      "Meta-SGD: Learning to Learn Quickly for Few-Shot Learning[J]. 2017.")
    doc.add_paragraph("[3] Ren M, Triantafillou E, Ravi S, et al. "
                      "Meta-Learning for Semi-Supervised Few-Shot Classification. 2018.")
    doc.add_paragraph("[4] Khodadadeh S, Bölöni L, Shah M. "
                      "Unsupervised Meta-Learning For Few-Shot Image Classification[C]. 2018.")
    doc.save(path)
    return path


def fixture_docx_with_errors() -> str:
    """一份**故意含引用错误**的稿子，用于验证自检的检出能力。

    为什么单独做一份而不改上面的正常稿：干净的稿子用来验"不误报"，
    带错的稿子用来验"能检出"，两者要分开——混在一起就分不清是漏报还是误报。

    刻意埋入的错误（与 selfcheck 的检查项一一对应）：
      · [9] 引用了不存在的编号        → 引用真实性 / 引用越界
      · [2] 条目重复著录              → 重复著录
      · [3] 条目缺文献类型标识 [J]    → 格式合规
      · [1] 条目列了但正文未引用      → 文末未引用
      · 一句明显的研究结论无角标      → 论断有据 / 该引未引
    """
    path = os.path.join(tmp_dir(), "我的论文初稿_含错误.docx")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    from docx import Document
    doc = Document()
    doc.add_heading("基于元学习的少样本图像分类方法研究", level=1)
    doc.add_heading("摘要", level=2)
    doc.add_paragraph(
        "少样本分类要求在每类只有少量标注样本时仍能取得可用精度。"
        "本文研究元学习在该场景下的适应效率问题。")
    doc.add_heading("1 引言", level=2)
    doc.add_paragraph(
        "少样本分类的核心困难在于标注数据稀缺[9]。"
        "元学习通过在任务分布上学习先验，使模型能快速适应新任务[2]。")
    doc.add_heading("2 方法", level=2)
    doc.add_paragraph("本文沿用任务分布的设定，并在每轮任务中联合利用有标签与无标签样本[3]。")
    doc.add_heading("3 实验", level=2)
    doc.add_paragraph(
        "所有对比方法在小样本条件下的准确率均未超过监督学习基线，这一结论在三个数据集上一致成立。")
    doc.add_heading("4 结论", level=2)
    doc.add_paragraph("本文验证了元学习在少样本分类中的有效性。")
    doc.add_heading("参考文献", level=2)
    doc.add_paragraph("[1] Finn C, Abbeel P, Levine S. "
                      "Model-Agnostic Meta-Learning for Fast Adaptation of Deep Networks[C]. 2017.")
    doc.add_paragraph("[2] Li Z, Zhou F, Chen F, et al. "
                      "Meta-SGD: Learning to Learn Quickly for Few-Shot Learning[J]. 2017.")
    doc.add_paragraph("[2] Li Z, Zhou F, Chen F, et al. "
                      "Meta-SGD: Learning to Learn Quickly for Few-Shot Learning[J]. 2017.")
    doc.add_paragraph("[3] Ren M, Triantafillou E, Ravi S, et al. "
                      "Meta-Learning for Semi-Supervised Few-Shot Classification. 2018.")
    doc.save(path)
    return path


def fixture_docx_with_unmatched_ref() -> str:
    """一份引用了"库里没有的文献"的稿子，用来验证「引用可核实性」能真正触发。

    为什么还要第三份：干净稿与带错稿的参考文献恰好都能在文献库里找到
    （实测命中 5 / 未命中 0），而「引用可核实性」只在"有条目查不到"或
    "匹配到了但库中没有全文"时才产生。拿这两份稿子断言这一项，等于断言一件
    在当前数据下不可能发生的事——要么永远失败，要么永远假通过。

    这份稿在带错稿末尾追加一条与库中主题明显无关的条目：前面几条命中库中文献
    （继续验证不误报），追加的那条查不到（验证能报）。
    """
    path = os.path.join(tmp_dir(), "我的论文初稿_含未收录引用.docx")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    from docx import Document
    src = Document(fixture_docx_with_errors())
    out = Document()
    for p in src.paragraphs:
        style = (p.style.name or "") if p.style is not None else ""
        if style.startswith("Heading"):
            lvl = style.split()[-1]
            out.add_heading(p.text, level=int(lvl) if lvl.isdigit() else 2)
        else:
            out.add_paragraph(p.text)
    out.add_paragraph("[4] Chen L, Wang H. Deep Reinforcement Learning for "
                      "Autonomous Driving: A Survey[J]. 2023.")
    out.save(path)
    return path


def fixture_pdf() -> str:
    """按需生成一份单页 PDF（有文字层），返回路径。

    test_clear 的边界用例需要"一个真实可解析的 PDF"，同样不该提交二进制。
    """
    path = os.path.join(tmp_dir(), "中文测试论文.pdf")
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "A Minimal Paper for Citation Checking", fontsize=14)
    page.insert_text((72, 130), "Abstract: this fixture exists so tests do not ship binaries.",
                     fontsize=10)
    doc.save(path)
    doc.close()
    return path


def sandbox_root() -> str:
    """数据与产物的根目录。

    WM_TEST_ROOT 优先：它是测试专门指定的隔离目录，必须压过环境里可能残留的
    WM_ROOT。否则源数据会被解释成沙箱自己，复制逻辑以为"源和目标同一目录"
    而跳过，沙箱里一份数据都没有，测试全部误报"语料库不存在"。
    两者都没设才退回项目根——单独跑某个脚本时仍然能用。
    """
    return os.environ.get("WM_TEST_ROOT", "").strip() or \
        os.environ.get("WM_ROOT", "").strip() or ROOT


def activate() -> str:
    """在导入被测代码**之前**调用：固定 WM_ROOT，并造好隔离沙箱。

    返回沙箱根目录。数据目录只复制一次——源数据保持只读，测试怎么折腾都不影响
    交付包里的数据与产物。
    """
    root = sandbox_root()
    os.environ["WM_ROOT"] = root
    # 默认开快速模式：Agent 不做模型推理（每步十几秒，整套测试会慢到没法用）。
    # 想验真实模型效果时显式设 WM_AGENT_FAST=0。
    os.environ.setdefault("WM_AGENT_FAST", "1")

    # 源数据在哪：交付包布局是「代码/」与「数据与产物/」并列在包根下，
    # 所以要从「代码」再往上一级找。源码目录单独跑时则在 <项目根>/data。
    # 两种布局都认，顺序即优先级。
    roots = [ROOT, os.path.dirname(ROOT)]
    src_data = ""
    for base in roots:
        for cand in (os.path.join(base, "数据与产物", "data"),
                     os.path.join(base, "data")):
            if os.path.isdir(cand):
                src_data = cand
                break
        if src_data:
            break
    dst_data = os.path.join(root, "data")
    if src_data and os.path.abspath(src_data) != os.path.abspath(dst_data):
        if not os.path.isdir(dst_data):
            shutil.copytree(src_data, dst_data)
            print("  [沙箱] 已把源数据复制到 %s" % dst_data)
    os.makedirs(os.path.join(root, "outputs"), exist_ok=True)

    # .env 也要指过去：交付包的 .env 是中文占位符，测试要能看见真实配置形态
    src_env = os.path.join(ROOT, ".env")
    if os.path.isfile(src_env) and os.path.abspath(root) != os.path.abspath(ROOT):
        dst_env = os.path.join(root, ".env")
        if not os.path.isfile(dst_env):
            shutil.copy2(src_env, dst_env)

    use_package_path()
    return root


if __name__ == "__main__":
    print("项目根 :", ROOT)
    print("包名   :", PKG)
    print("沙箱根 :", activate())
    print("临时目录:", tmp_dir())
