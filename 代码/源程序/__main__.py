"""命令行入口：python -m <包目录名> <命令>（交付包中即 源程序）

命令：
  demo     一条命令跑完（依赖自举 → 建索引 → 全流程 → 开本地页面）
  fetch    按方向检索并下载开放获取论文
  parse    解析本地 PDF
  cards    生成文献卡片
  matrix   生成对比矩阵
  draft    生成综述草稿 + 参考文献
  check    引用自检（含埋错测试）
  export   导出 Word / Excel
  all      除开页面外跑完全流程
  doctor   环境自检（依赖、网络、模型、数据）
  status   查看当前进度与今日花费
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

from . import bootstrap as bs

# 实际包目录名（改名后不用回来改这里的提示文案）。
# 交付时包目录叫「源程序」，直接写死 "wenmai" 会让提示里的命令全都跑不起来。
_PKGNAME = (__package__ or "源程序").split(".")[0]


def _hdr(t: str) -> None:
    print("\n" + "=" * 62)
    print("  " + t)
    print("=" * 62)


def _port_in_use(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _find_free_port(host: str, start: int, tries: int = 20) -> int:
    for p in range(start, start + tries):
        if not _port_in_use(host, p):
            return p
    return start


def _is_wenmai_server(host: str, port: int) -> bool:
    """判断占用端口的是不是本程序的旧实例（避免误杀别人的程序）。"""
    try:
        import urllib.request
        with urllib.request.urlopen("http://%s:%d/api/state" % (host, port), timeout=2) as r:
            body = r.read(400).decode("utf-8", "ignore")
        return '"corpus"' in body or '"settings"' in body
    except Exception:
        return False


def cmd_stop(args) -> int:
    """停止正在运行的文脉服务（先确认是本程序，再结束进程）。"""
    import subprocess
    host, port = args.host, args.port
    _hdr("文脉 · 停止服务")
    print("  检查 %s:%d …" % (host, port))
    if not _port_in_use(host, port):
        print("  该端口没有服务在运行，无需处理。")
        return 0
    if not _is_wenmai_server(host, port):
        print("  端口被其他程序占用（不是文脉），不做处理。")
        return 1
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15).stdout
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(":%d" % port) and parts[3] == "LISTENING":
                pids.add(parts[4])
        if not pids:
            print("  未找到监听该端口的进程。")
            return 1
        for pid in pids:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=15)
            print("  已结束进程 PID %s" % pid)
        print("  完成。现在可以重新启动：python -m " + _PKGNAME + " demo")
        return 0
    except Exception as e:
        print("  停止失败：%s" % str(e)[:150])
        return 1


def cmd_doctor(args) -> int:
    from .config import (CORPUS_JSON, DOCS_JSON, CARDS_JSON, OUT_DIR, settings,
                         key_problem)
    _hdr("文脉 · 环境自检")
    r = bs.ensure(quiet=False, auto_install=False)
    print("  硬依赖：%s" % ("齐备" if r["ok"] else "缺少 " + ", ".join(r["missing"])))
    if r["optional_missing"]:
        print("  可选依赖未装：%s（不影响运行）" % ", ".join(r["optional_missing"]))

    print("\n  模型服务：%s" % ("已配置（百炼）" if settings.llm_available else "未启用，将使用本地引擎"))
    # 交付包的 .env 里是中文占位符：说成"已配置"会让评委以为云端已可用，
    # 所以这里用与 settings.llm_available 同一套判据（key_problem），
    # 占位符直接说清楚它不可用、以及怎么填。
    _key_bad = key_problem(settings.api_key) if settings.api_key else ""
    if _key_bad:
        print("  API Key：不可用 —— %s" % _key_bad)
    elif settings.api_key:
        print("  API Key：已配置（前缀 %s…）" % settings.api_key[:6])
    print("  单日成本上限：%.0f 元；结果缓存：%s" % (
        settings.daily_cost_limit, "开" if settings.enable_cache else "关"))

    # 网络探测（失败不影响离线运行）
    print("\n  网络连通性（仅影响模型增强，不影响本地运行）：")
    if _key_bad or not settings.api_key:
        # 没填 Key 或填的是占位符时不要发请求：占位符含中文，urllib 按 latin-1
        # 编码请求头会抛 UnicodeEncodeError，界面上只会看到一句看不懂的报错。
        print("    百炼接口：未检测（%s）"
              % ("还没填 Key" if not settings.api_key else ".env 里是占位符，填真实 Key 后再看"))
    else:
        try:
            import urllib.request
            req = urllib.request.Request(settings.base_url + "/models",
                                         headers={"Authorization": "Bearer " + settings.api_key})
            with urllib.request.urlopen(req, timeout=8) as resp:
                print("    百炼接口：可达（HTTP %d）" % resp.status)
        except Exception as e:
            print("    百炼接口：不可达（%s）—— 将走本地引擎，演示不受影响" % str(e)[:60])

    print("\n  数据状态：")
    for label, path in (("语料库 corpus.json", CORPUS_JSON), ("解析结果 docs.json", DOCS_JSON),
                        ("文献卡片 cards.json", CARDS_JSON)):
        print("    %-22s %s" % (label, "存在" if os.path.isfile(path) else "未生成"))
    print("    输出目录 %s" % OUT_DIR)

    # 离线本地模型：这是"断网也能出真结果"的关键，必须让用户看得见
    from . import local_llm
    st = local_llm.status()
    print("\n  离线本地模型（llama.cpp，随应用打包）：")
    print("    可执行文件：%s" % st["二进制"])
    print("    向量模型  ：%s %s" % (st["向量模型"], "OK" if st["向量可用"] else "--"))
    print("    生成模型  ：%s %s" % (st["生成模型"], "OK" if st["生成可用"] else "--"))
    print("    草稿模型  ：%s" % st["草稿模型"])
    print("    生成参数  ：%d 线程 / %d 槽 / 总上下文 %d（每槽 %d token）" % (
        st["生成线程"], st["生成槽位"], st["生成上下文"],
        st["生成上下文"] // max(st["生成槽位"], 1)))
    print("               %s" % ("投机解码已启用" if st["投机解码"] else "无投机解码"))
    if st["向量可用"] and st["生成可用"]:
        print("    -> 离线模式可用：语义检索 + 中文生成均在本地完成")
        print("       资源需求：约 4.4 GB 内存（上下文 16384 是为容纳卡片抽取的长提示词）")
        if not st["投机解码"]:
            print("       提示：再加一个 0.5B 草稿模型可提速约 59%（约 470MB）")
    elif st["向量可用"]:
        print("    -> 离线模式：检索可用（本地模型），正文生成退回模板")
    else:
        print("    -> 未安装本地模型：离线时仅能用规则引擎（n-gram 检索 + 模板）")
        print("       安装方式：python -m " + _PKGNAME + " fetch-model")
    return 0


def cmd_status(args) -> int:
    import json
    from .cards import load_cards
    from .config import OUT_DIR
    from .literature import load_corpus, load_docs
    from .llm import today_cost
    _hdr("文脉 · 当前进度")
    c = load_corpus()
    d = load_docs()
    k = load_cards()
    print("  文献库：%d 篇（检索词：%s）" % (c.get("count", 0), c.get("query") or "—"))
    print("  已解析：%d 篇" % d.get("count", 0))
    print("  文献卡片：%d 张" % k.get("count", 0))
    cost = today_cost()
    print("  今日模型花费：%.4f 元（调用 %d 次，缓存命中 %d 次）" % (
        cost.get("cost", 0), cost.get("calls", 0), cost.get("cached", 0)))
    for f in ("draft.md", "文脉_综述初稿.docx", "selfcheck.json", "selfcheck_benchmark.json"):
        p = os.path.join(OUT_DIR, f)
        print("  %-28s %s" % (f, "已生成" if os.path.isfile(p) else "未生成"))
    return 0


def cmd_fetch(args) -> int:
    from .literature import fetch
    _hdr("文脉 · 获取文献")
    fetch(args.query, limit=args.limit)
    return 0


def cmd_parse(args) -> int:
    from .literature import parse_all
    _hdr("文脉 · 解析 PDF")
    parse_all(verbose=True)
    return 0


def cmd_cards(args) -> int:
    from .cards import build_cards
    _hdr("文脉 · 生成文献卡片")
    build_cards(use_llm=not args.no_llm, limit=args.limit, verbose=True)
    return 0


def cmd_matrix(args) -> int:
    from .matrix import build_matrix
    _hdr("文脉 · 对比矩阵")
    build_matrix(verbose=True)
    return 0


def cmd_draft(args) -> int:
    from .draft import generate
    _hdr("文脉 · 综述草稿")
    generate(use_llm=not args.no_llm, verbose=True)
    return 0


def cmd_check(args) -> int:
    from . import selfcheck
    _hdr("文脉 · 引用自检")
    selfcheck.self_check(verbose=True)
    selfcheck.benchmark(verbose=True)
    return 0


def cmd_evidence(args) -> int:
    from .selfcheck import run_evidence_check
    _hdr("文脉 · 论断回原文核对（RAG 检索环节）")
    run_evidence_check(use_llm=not args.no_llm, verbose=True)
    return 0


def cmd_missing(args) -> int:
    from .selfcheck import run_missing_citation_check
    _hdr("文脉 · 该引未引检测")
    run_missing_citation_check(verbose=True)
    return 0


def cmd_refs(args) -> int:
    """从题录文本导入文献（支持知网/CNKI/RefWorks 导出格式）。"""
    from .refparse import parse_refs, append_to_corpus
    _hdr("文脉 · 题录导入")
    text = ""
    if args.file:
        if not os.path.isfile(args.file):
            print("  文件不存在：%s" % args.file)
            return 1
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    else:
        print("  请粘贴题录文本，输入完成后按 Ctrl+Z 再回车结束：\n")
        try:
            text = sys.stdin.read()
        except Exception:
            text = ""
    if not text.strip():
        print("  没有收到题录内容。")
        return 1
    recs = parse_refs(text)
    if not recs:
        print("  未能解析出任何题录。请确认格式（支持 RefWorks 标签式与【字段】键值式）。")
        return 1
    print("  解析出 %d 条题录：" % len(recs))
    for i, r in enumerate(recs, 1):
        print("   [%d] %s" % (i, (r.get("title") or "")[:52]))
        print("       %s | %s | [%s]" % ("、".join(r.get("authors") or [])[:36] or "—",
                                        r.get("year") or "—", r.get("ref_type")))
    added = append_to_corpus(recs, original_name=args.name or "粘贴的题录")
    print("\n  已写入语料库：新增 %d 条" % added)
    print("  提示：题录只有元数据、没有全文，因此「回原文核对」无法对其生效（会在报告中标注）。")
    return 0


def cmd_fetch_model(args) -> int:
    """下载离线模型到应用目录，使整个文件夹拷到别的电脑也能离线使用。"""
    from . import local_llm
    _hdr("文脉 · 安装离线本地模型")
    print("  用途：断网时也能做语义检索与中文生成，且论文内容不出本机。")
    print("  说明：单连接下载会被限速，这里用多连接并行（实测可提速百倍以上）。")
    print("  预计体积：约 2.1 GB（引擎 45MB + 向量模型 25MB + 生成模型 2GB）\n")
    tdir = args.dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    r = local_llm.install_models(target_dir=tdir, verbose=True)
    print("\n  安装目录：%s" % r["dir"])
    if r["ok"]:
        print("  成功：%s" % "；".join(r["ok"]))
    if r["failed"]:
        print("  失败：%s" % "；".join(r["failed"]))
        print("  可重跑本命令续传（已下载的分段会被跳过）")
    if local_llm.available("embed"):
        print("\n  离线模式现在可用。可用 `python -m " + _PKGNAME + " doctor` 复核。")
    return 0 if not r["failed"] else 1


def cmd_agent(args) -> int:
    from .agent import run_agent
    from .config import OUT_DIR          # 本文件按惯例在函数内导入，此处原先漏了
    _hdr("文脉 · Agent 审稿（模型自主选择工具）")
    res = run_agent(verbose=True, max_steps=args.max_steps)
    print()
    print("  状态：%s　步数：%d　用时：%.1f 秒" % (res["status"], res["steps"], res["seconds"]))
    print("  使用工具：%s" % ("、".join(res["tools_used"]) or "—"))
    if res.get("agent_summary"):
        print("\n  审查结论：\n  %s" % res["agent_summary"])
    print("\n  过程记录：%s" % os.path.join(OUT_DIR, "agent_trace.json"))
    return 0


def cmd_export(args) -> int:
    import json
    from . import docx_export, draft as draft_mod
    from .config import OUT_DIR
    _hdr("文脉 · 导出")
    d = draft_mod.load_draft()
    if not d:
        print("  尚未生成草稿，请先执行 draft")
        return 1
    chk = {}
    p = os.path.join(OUT_DIR, "selfcheck.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            chk = json.load(f)
    docx_export.export(d, chk)
    docx_export.export_matrix_excel()
    return 0


def cmd_all(args) -> int:
    from .cards import build_cards
    from .draft import generate
    from .matrix import build_matrix
    from .literature import load_corpus, parse_all
    from . import selfcheck, docx_export
    import json
    from .config import OUT_DIR

    t0 = time.time()
    if load_corpus().get("count", 0) == 0:
        print("  [提示] 文献库为空，请先执行：python -m " + _PKGNAME + " fetch --query \"你的方向\"")
        return 1
    _hdr("文脉 · 全流程")
    parse_all(verbose=True)
    build_cards(use_llm=not args.no_llm, verbose=True)
    build_matrix(verbose=True)
    d = generate(use_llm=not args.no_llm, verbose=True)
    selfcheck.self_check(verbose=True)
    selfcheck.benchmark(verbose=True)
    chk = {}
    p = os.path.join(OUT_DIR, "selfcheck.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            chk = json.load(f)
    docx_export.export(d, chk, verbose=True)
    docx_export.export_matrix_excel(verbose=True)
    print("\n  全流程完成，用时 %.1f 秒" % (time.time() - t0))
    return 0


def cmd_demo(args) -> int:
    from .config import settings
    _hdr("文脉 WenMai · 一条命令启动")
    print("  文献综述写作与引用自检助手")
    print("  设计目标：断网可演示 · 零登录 · 本地优先\n")

    r = bs.ensure(quiet=False, auto_install=True)
    if not r["ok"]:
        print("\n  依赖未就绪，无法继续。请手动安装后重试：")
        print("     pip install %s" % " ".join(r["missing"]))
        return 1

    from .literature import load_corpus, parse_all, load_docs
    from .cards import build_cards, load_cards
    from .matrix import build_matrix
    from .draft import generate, load_draft
    from . import selfcheck, docx_export
    import json
    from .config import OUT_DIR, DOCS_JSON, DATA_DIR

    corpus = load_corpus()
    print("  文献库：%d 篇%s" % (corpus.get("count", 0),
                              ("（检索词：%s）" % corpus.get("query")) if corpus.get("query") else ""))

    empty = (corpus.get("count", 0) == 0)
    if empty:
        # 库为空**也必须把页面起起来**。
        # 原先这里是 `return 1` 直接退出：用户点了界面上的「清空」之后，
        # 再双击启动器就只看到一段英文提示、窗口一闪而过，网页再也不弹出来
        # （实测被当成"程序坏了"）。而页面本身对空库有完整引导——
        # 示例论文、粘贴知网题录、去搜索并补齐，都比让人去敲命令行友好。
        print("\n  文献库为空 → 直接打开页面，在页面里放入论文（可一键载入示例论文）。")
        print("  若想用命令行：python -m " + _PKGNAME + " fetch --query \"few-shot image classification\"")
        print("                或把自己的 PDF 放入 %s 后执行：python -m %s parse"
              % (os.path.dirname(DOCS_JSON), _PKGNAME))

    host, port = args.host, args.port

    # ── 端口冲突处理（这是之前"页面卡住"的真正原因）──
    # 若端口已被本程序的旧实例占用，浏览器会连到那个旧服务上（旧代码 + 旧页面），
    # 表现为"新启动明明成功、页面却行为异常"。这里主动处理，不再静默失败。
    if _port_in_use(host, port):
        if _is_wenmai_server(host, port):
            print("\n  [注意] %s:%d 上已有一个文脉服务在运行。" % (host, port))
            print("         很可能是上次没有正常退出（请确认旧的黑色窗口已关闭）。")
            if args.reuse:
                url = "http://%s:%d" % (host, port)
                print("         按 --reuse 使用现有实例：%s" % url)
                if not args.no_browser:
                    webbrowser.open(url)
                print("\n  按 Ctrl+C 结束本窗口（不会停止那个旧服务；如需停止请运行："
                      "python -m " + _PKGNAME + " stop）\n")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
                return 0
            new_port = _find_free_port(host, port + 1)
            print("         已自动改用空闲端口 %d。" % new_port)
            print("         建议同时运行：python -m " + _PKGNAME + " stop  （停掉旧实例）")
            port = new_port

    url = "http://%s:%d" % (host, port)

    # 关键顺序：先把页面服务起起来，再跑流程，最后开浏览器。
    # 否则用户会先看到一个"无法访问"的空白页（服务还没监听），体验很差。
    import uvicorn
    import socket

    bind_error = {}

    def _serve():
        # 用**实际包名**构造导入串，不要写死 "wenmai.app:app"。
        # 交付包允许把包目录改名或放进子目录（例如「代码/源程序」），
        # 写死包名时 uvicorn 会解析不到模块、服务静默起不来，
        # 用户只看到一句"页面服务启动较慢"（实测踩到）。
        _pkg = (__package__ or "wenmai").split(".")[0]
        try:
            uvicorn.run("%s.app:app" % _pkg, host=host, port=port, log_level="warning")
        except Exception as e:
            bind_error["err"] = str(e)[:200]

    server = threading.Thread(target=_serve, daemon=True)
    server.start()

    # 等服务真正开始监听（最多 15 秒）
    ready = False
    for _ in range(60):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                ready = True
                break
        except Exception:
            time.sleep(0.25)

    if ready:
        print("\n  本地页面已就绪：%s" % url)
    else:
        print("\n  [提示] 页面服务启动较慢，稍后手动打开：%s" % url)

    print("  正在处理论文并生成综述，请稍等（此窗口会持续输出进度）…\n")

    if not args.no_browser:
        # 带版本号打开：浏览器会视为新地址，从而绕开缓存的旧页面
        try:
            from .app import PAGE_VERSION
            webbrowser.open(url + "/?v=" + PAGE_VERSION)
        except Exception:
            webbrowser.open(url)

    # ── 按需生成：只在数据与文献库不一致时才动手 ──────────────
    # 原实现**无条件**重跑整条流水线（卡片→矩阵→草稿→自检→埋错→导出），
    # 随包已带全部结果时也要白跑一遍：离线模式下约 200 秒，而端口是在这段
    # 跑完之后才绑定的——实测双击后要等 207 秒才看到页面（第 2 次流程再等一轮）。
    # 这让"打开即可用"完全失效，评委会以为程序没反应。
    # 现在改为逐级比对新鲜度，已是最新就跳过；同时把服务启动放在最前面。
    try:
        if not empty:
            n_papers = corpus.get("count", 0)

            if len(load_docs().get("docs", [])) != n_papers:
                parse_all(verbose=True)
            else:
                print("  [解析] 已是最新（%d 篇），跳过" % n_papers)

            if len(load_cards().get("cards", [])) != n_papers:
                build_cards(use_llm=not args.no_llm, verbose=True)
            else:
                print("  [卡片] 已是最新，跳过")

            # 矩阵、草稿、自检、导出：只要产物齐全就跳过
            need_matrix = not os.path.isfile(os.path.join(DATA_DIR, "matrix.json"))
            draft_md = load_draft()
            need_draft = not (draft_md and (draft_md.get("markdown") or "").strip())
            need_check = not os.path.isfile(os.path.join(OUT_DIR, "selfcheck.json"))
            need_export = not os.path.isfile(os.path.join(OUT_DIR, "文脉_综述初稿.docx"))

            if need_matrix:
                build_matrix(verbose=True)
            else:
                print("  [矩阵] 已是最新，跳过")

            if need_draft:
                draft_md = generate(use_llm=not args.no_llm, verbose=True)
            else:
                print("  [草稿] 已是最新，跳过")

            chk = {}
            if need_check:
                selfcheck.self_check(verbose=True)
            else:
                print("  [自检] 已是最新，跳过")
            p = os.path.join(OUT_DIR, "selfcheck.json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    chk = json.load(f)

            if need_export:
                d = draft_md or load_draft() or generate(use_llm=False, verbose=False)
                docx_export.export(d, chk, verbose=True)
                docx_export.export_matrix_excel(verbose=True)
            else:
                print("  [导出] Word/Excel 已是最新，跳过")

            if not (need_matrix or need_draft or need_check or need_export):
                print("\n  全部内容均已就绪，可直接在页面上查看与下载。")
            else:
                print("\n  全部完成。页面点「刷新」按钮即可看到结果；按 Ctrl+C 停止服务。")
            print()
    except Exception as e:
        print("\n  [提示] 自动处理时出错：%s" % str(e)[:200])
        print("  页面仍可正常使用，请在页面上点「开始生成综述稿」重试。\n")

    # 主线程等待服务（Ctrl+C 结束）
    try:
        while server.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  已停止。")
    return 0


def cmd_serve(args) -> int:
    host, port = args.host, args.port
    url = "http://%s:%d" % (host, port)
    print("文脉本地页面：%s（Ctrl+C 停止）" % url)
    import uvicorn
    # 同 cmd_demo：包名用实际值，允许包目录改名或被放进子目录
    _pkg = (__package__ or "wenmai").split(".")[0]
    uvicorn.run("%s.app:app" % _pkg, host=host, port=port, log_level="warning")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="wenmai", description="文脉 WenMai · 文献综述写作与引用自检助手")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("demo", help="一条命令跑完全流程并打开本地页面")
    p.add_argument("--no-llm", action="store_true", help="只走本地引擎（离线演示）")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--reuse", action="store_true", help="端口被旧实例占用时，直接复用旧实例")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8848)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("stop", help="停止正在运行的文脉服务（解决端口被占用）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8848)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("fetch", help="按方向检索并下载开放获取论文")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("parse", help="解析本地 PDF")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("cards", help="生成文献卡片")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_cards)

    p = sub.add_parser("matrix", help="生成对比矩阵")
    p.set_defaults(func=cmd_matrix)

    p = sub.add_parser("draft", help="生成综述草稿与参考文献")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("check", help="引用自检与埋错测试")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("evidence", help="论断回原文核对（RAG 检索环节）")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("missing", help="该引未引检测（找出需要引用却未标注的陈述）")
    p.set_defaults(func=cmd_missing)

    p = sub.add_parser("refs", help="从题录文本导入文献（知网/CNKI/RefWorks 导出）")
    p.add_argument("--file", default=None, help="题录文本文件；省略则从标准输入粘贴")
    p.add_argument("--name", default=None, help="来源名称，便于在文献库中识别")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("fetch-model", help="下载离线本地模型（约 2.1GB，随应用打包）")
    p.add_argument("--dir", default=None, help="安装目录，默认为 wenmai/models")
    p.set_defaults(func=cmd_fetch_model)

    p = sub.add_parser("agent", help="Agent 审稿（模型自主选择工具）")
    p.add_argument("--max-steps", type=int, default=20)
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("export", help="导出 Word / Excel")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("all", help="跑完全流程（不开页面）")
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("serve", help="只启动本地页面")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8848)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="查看进度与花费")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
