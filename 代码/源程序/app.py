"""本地 Web 界面：一页展示全流程（零登录、纯本地）。

启动后浏览器打开 http://127.0.0.1:8848
"""
from __future__ import annotations

import json
import os
import time
from typing import List

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse

from . import cards as cards_mod
from . import docx_export, draft as draft_mod, matrix as matrix_mod, selfcheck
from .config import DATA_DIR, DOCX_OUT, OUT_DIR, PDF_DIR, settings
from .literature import load_corpus, load_docs, looks_like_non_paper as _looks_like_non_paper
from .llm import today_cost

app = FastAPI(title="文脉 WenMai", docs_url=None, redoc_url=None)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# 页面版本号：每次服务启动都会变化。
# 用途：启动时用 /?v=<版本号> 打开页面，浏览器会视为新地址，从而绕开缓存的旧页面。
PAGE_VERSION = time.strftime("%m%d%H%M%S")

# /api/run 允许的阶段。未知值一律 400：曾经把非法值静默当成 "all"，
# 于是"参数写错"变成"跑了整套流程并消耗云端额度"，用户完全看不出原因。
_VALID_STAGES = {"all", "cards", "matrix", "draft", "check", "export"}

# 交付副本的只读保护（**默认关闭，由跑脚本的一方主动开启**）。
#
# 起因：桌面副本被自动化脚本改坏过两次（数据被清空、产物只剩几个文件），
# 所以在"项目位于桌面下"时拒绝改数据的请求。
# 但第一版写成了"在桌面下就自动生效"，结果**误伤了真实使用**——用户在桌面
# 副本上点「清空文献库与结果」被 409 拒绝，界面只说"清空失败"，让人以为
# 程序坏了。保护措施不能挡住用户自己要做的操作。
#
# 因此改为显式开启：只有设了 WM_READONLY=1 才拦截。跑自动化测试时主动设上，
# 日常双击启动不设，一切操作照常。
_READONLY = os.environ.get("WM_READONLY", "") in ("1", "true", "True")
_MUTATING_PATHS = ("/api/upload", "/api/delete", "/api/clear", "/api/run", "/api/refs",
                   "/api/mydraft/upload", "/api/mydraft/delete", "/api/mydraft/check",
                   "/api/find/import", "/api/privacy", "/api/privacy/save")

# 服务端异常落盘。为什么需要：实测出现过"界面报 Failed to fetch、服务已不在监听"，
# 而启动窗口没有留下任何可查的线索，只能靠猜。
# 写一份 logs/server_error.log，出问题时能直接看到时间、路径与完整堆栈。
SERVER_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "logs", "server_error.log")


def _log_server_error(kind: str, detail: str) -> None:
    try:
        os.makedirs(os.path.dirname(SERVER_LOG), exist_ok=True)
        with open(SERVER_LOG, "a", encoding="utf-8") as f:
            f.write("\n[%s] %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), kind, detail))
    except Exception:
        pass


@app.exception_handler(Exception)
async def _record_unhandled(request: Request, exc: Exception):
    """未捕获异常写日志，并给前端一个可读的回复（而不是空响应）。"""
    import traceback
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _log_server_error("未捕获异常 %s %s" % (request.method, request.url.path), tb)
    return JSONResponse({"ok": False, "error": "服务端内部错误：%s" % str(exc)[:200]},
                        status_code=500)


@app.middleware("http")
async def _guard_readonly(request: Request, call_next):
    """WM_READONLY=1 时拒绝改数据的请求（供自动化脚本使用，避免改坏交付副本）。"""
    if (_READONLY and request.method in ("POST", "PUT", "DELETE")
            and any(request.url.path.startswith(p) for p in _MUTATING_PATHS)):
        return JSONResponse(
            {"ok": False,
             "error": "当前服务以只读模式启动（WM_READONLY=1），拒绝了这个修改数据的请求。"
                      "如果这是你自己在用，请关闭该环境变量后重启服务。"},
            status_code=409)
    return await call_next(request)


@app.on_event("startup")
def _apply_saved_privacy():
    """启动时应用用户上次选择的处理方式。

    必须**双向**设置：只处理 local 会导致用户改回 cloud 后仍然卡在离线模式
    （环境变量残留 WM_OFFLINE=1，云端能力永远不恢复）。
    """
    p = _load_privacy()
    offline = (p.get("mode") == "local")
    os.environ["WM_OFFLINE"] = "1" if offline else "0"
    settings.force_offline = offline


def _read(path: str) -> str:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", response_class=HTMLResponse)
def index(skip: int = 0):
    # 隐私设置未确认时，直接返回纯 HTML 表单页（不依赖任何脚本，必定可用）
    if skip:
        _save_privacy({"mode": "cloud", "acknowledged": True,
                       "acknowledged_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    elif not _load_privacy().get("acknowledged"):
        page = _read(os.path.join(WEB_DIR, "privacy.html"))
        if page:
            return HTMLResponse(_inject_version(page), headers=_NO_CACHE)

    return _serve_app()


@app.get("/app", response_class=HTMLResponse)
def app_page():
    """直接进入主应用（跳过隐私页）。"""
    return _serve_app()


# 站点图标：用内嵌 SVG 生成，无需额外二进制文件（避免浏览器控制台 404）
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='#2563d6'/>"
    "<text x='32' y='45' font-size='38' font-family='Microsoft YaHei,sans-serif' "
    "fill='#fff' text-anchor='middle'>文</text></svg>"
)


def _favicon_png() -> bytes:
    """兼容 /favicon.ico 请求：用 PyMuPDF 把 SVG 转成 PNG 字节（失败则返回空）。"""
    try:
        import fitz
        doc = fitz.open(stream=_FAVICON_SVG.encode("utf-8"), filetype="svg")
        pix = doc[0].get_pixmap(dpi=48)
        return pix.tobytes("png")
    except Exception:
        return b""


@app.get("/favicon.svg")
def favicon_svg():
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
def favicon_ico():
    png = _favicon_png()
    if png:
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


def _inject_version(html: str) -> str:
    """在页面里写入当前版本号，便于前端自检与日志排查。"""
    return html.replace("</head>", "<meta name=\"wm-version\" content=\"%s\"></head>" % PAGE_VERSION)


def _serve_app():
    html = _read(os.path.join(WEB_DIR, "index.html"))
    if not html:
        return HTMLResponse("<h1>文脉 WenMai</h1><p>缺少 web/index.html</p>", status_code=500)
    return HTMLResponse(_inject_version(html), headers=_NO_CACHE)


@app.post("/api/privacy/save")
def privacy_save(mode: str = Form("cloud")):
    """表单提交入口（纯 HTML，无脚本依赖）。"""
    if mode not in ("cloud", "local"):
        mode = "cloud"
    _save_privacy({"mode": mode, "acknowledged": True,
                   "acknowledged_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    os.environ["WM_OFFLINE"] = "1" if mode == "local" else "0"
    try:
        settings.force_offline = (mode == "local")
    except Exception:
        pass
    return RedirectResponse("/", status_code=303)


# 本进程**启动时**加载的 app.py 的修改时间。必须在导入时取值——若在请求里
# 现读文件，拿到的永远是"当前文件时间"，任何旧进程都会自称最新，检测失效。
_LOADED_CODE_MTIME = 0.0
try:
    _LOADED_CODE_MTIME = os.path.getmtime(os.path.abspath(__file__))
except Exception:
    pass


def _code_mtime() -> float:
    """供客户端识别"这个在跑的服务是不是旧代码"。"""
    return _LOADED_CODE_MTIME


def _freshness(corpus_count: int) -> dict:
    """判断各阶段结果是否与当前文献库同步。

    注意：**题录（doc_type=ref）没有全文，不参与解析与卡片抽取**，
    因此新鲜度只对"有全文的文献"计算，否则导入题录后会永远显示"过期"。
    """
    def n_of(path: str, key: str) -> int:
        if not os.path.isfile(path):
            return -1
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return len(d.get(key) or [])
        except Exception:
            return -1

    # 只统计有全文的记录（PDF / Word），排除题录
    try:
        with open(os.path.join(DATA_DIR, "corpus.json"), encoding="utf-8") as f:
            recs = json.load(f).get("records", [])
        need = sum(1 for r in recs if (r.get("doc_type") or "pdf") != "ref")
    except Exception:
        need = corpus_count

    docs = n_of(os.path.join(DATA_DIR, "docs.json"), "docs")
    cards = n_of(os.path.join(DATA_DIR, "cards.json"), "cards")
    draft_papers = -1
    draftp = os.path.join(OUT_DIR, "draft.json")
    if os.path.isfile(draftp):
        try:
            with open(draftp, encoding="utf-8") as f:
                dj = json.load(f)
            draft_papers = int(dj.get("source_papers")
                               or len(dj.get("citations") or dj.get("refs") or []))
        except Exception:
            draft_papers = -1

    def state(done: int) -> str:
        if done < 0:
            return "missing"
        return "current" if done == need else "stale"

    return {
        "docs": state(docs), "cards": state(cards), "draft": state(draft_papers),
        "papers_with_fulltext": need,
        "refs_only": max(corpus_count - need, 0),
        "needs_run": not (docs == need == cards and draft_papers == need),
    }


@app.get("/api/state")
def state():
    corpus = load_corpus()
    docs = load_docs()
    cards_data = cards_mod.load_cards()
    matrix = matrix_mod.load_matrix()
    d = draft_mod.load_draft()
    check = {}
    cpath = os.path.join(OUT_DIR, "selfcheck.json")
    if os.path.isfile(cpath):
        with open(cpath, encoding="utf-8") as f:
            check = json.load(f)
    bench = {}
    bpath = os.path.join(OUT_DIR, "selfcheck_benchmark.json")
    if os.path.isfile(bpath):
        with open(bpath, encoding="utf-8") as f:
            bench = json.load(f)
    return JSONResponse({
        "settings": settings.describe(),
        # 本服务加载的代码是什么时候的（app.py 的 mtime）。
        # 用途：客户端可据此判断"这个已经在跑的服务是不是旧代码"——
        # 改了源码却连到旧进程，测试与调试都会得到失真结论（实测踩到过）。
        "code_mtime": _code_mtime(),
        "cost": today_cost(),
        "freshness": _freshness(corpus.get("count", 0)),
        "corpus": {"count": corpus.get("count", 0), "query": corpus.get("query", "")},
        "docs": {"count": docs.get("count", 0)},
        "cards": {"count": cards_data.get("count", 0), "llm_enabled": cards_data.get("llm_enabled")},
        "matrix": {"count": matrix.get("count", 0), "topics": matrix.get("topics", []),
                   "columns": matrix.get("columns", []), "rows": matrix.get("rows", [])},
        "draft": {"topics": d.get("topics", []), "llm_note": d.get("llm_note", ""),
                  # 降级原因要透给界面：界面据它区分"确实没配 Key"与"模型有输出但
                  # 没通过一致性检查"——原先把两者混为一谈，会显示"离线模式/
                  # 不会调用大模型"这种与事实相反的话（实测被反馈过）。
                  "fallback_reason": d.get("fallback_reason", ""),
                  "refs": d.get("refs", []), "markdown": d.get("markdown", ""),
                  "papers_used": (len(d.get("refs") or []) if d else 0)},
        "check": check.get("summary", {}),
        "check_issues": check.get("issues", [])[:60],
        "benchmark": bench,
        "has_docx": os.path.isfile(DOCX_OUT),
    })


@app.post("/api/run")
def run_pipeline(stage: str = None, stage_form: str = Form(None, alias="stage")):
    """按阶段执行。stage: cards | matrix | draft | check | export | all

    stage 同时接受查询参数与表单字段：只声明查询参数时，用表单提交会被
    静默忽略并退回默认值 "all"（实测踩到过——传 stage=matrix 却跑了全流程），
    这类"参数被悄悄丢掉"的行为对调用方极不友好，因此两种都收。
    """
    given = stage_form if stage_form is not None else stage
    # 实测：表单里的空字段（body 为 `stage=`）会被 FastAPI 解析成 None，
    # 与"完全没传"无法区分——所以不再试图区分，缺省与空值都按默认全流程走
    # （用户点"生成"时本来就是这个意思）。
    stage = "all" if given is None else str(given).strip() or "all"
    # 但**非法值必须明确拒绝**：原来未知阶段不匹配任何分支，就等于"什么都没做"
    # 却返回 200；若哪天默认值改成 all，写错参数就会静默跑一整套并消耗额度。
    if stage not in _VALID_STAGES:
        return JSONResponse(
            {"ok": False, "log": [],
             "error": "未知阶段 %r，可选：%s" % (stage, "、".join(sorted(_VALID_STAGES)))},
            status_code=400)
    log = []
    t0 = time.time()
    try:
        if stage in ("cards", "all"):
            # 关键：先解析**尚未解析或已过期**的文献，再抽卡片。
            # CLI 的 `all` 命令里有这一步，但接口此前没有——导致"上传完直接点
            # 生成"时卡片数为 0（实测：上传 6 篇后跑 cards 阶段产出 0 张，
            # 全部文献停在「待解析」）。这是主流程的致命缺陷。
            try:
                from .literature import load_corpus as _lc, load_docs as _ld, parse_all as _pa
                recs = _lc().get("records", [])
                need = sum(1 for r in recs if (r.get("doc_type") or "pdf") != "ref")
                have = len(_ld().get("docs", []))
                if have != need:
                    log.append("解析：需要全文 %d 篇 / 已解析 %d 篇，开始解析…" % (need, have))
                    pr = _pa(verbose=True)
                    n_ok = sum(1 for d in pr.get("docs", []) if not d.get("parse_error"))
                    log.append("解析完成：%d/%d 篇成功" % (n_ok, len(pr.get("docs", []))))
            except Exception as e:
                log.append("解析阶段出错（继续尝试抽卡）：%s" % str(e)[:80])
            r = cards_mod.build_cards(verbose=True)
            log.append("卡片：%d 张（模型通道 %s）" % (r["count"], r.get("llm_enabled")))
        if stage in ("matrix", "all"):
            r = matrix_mod.build_matrix(verbose=False)
            log.append("矩阵：%d 篇 × %d 列" % (r["count"], len(r.get("columns", []))))
        if stage in ("draft", "all"):
            r = draft_mod.generate(verbose=False)
            log.append("草稿：%d 条引用（%s）" % (len(r.get("refs", [])), r.get("llm_note", "")[:40]))
        if stage in ("check", "all"):
            r = selfcheck.self_check(verbose=True)
            log.append("自检：%d 处问题" % r["summary"]["total"])
            ev = (r.get("summary") or {}).get("evidence_check") or {}
            if ev:
                log.append("RAG 证据核对：核对 %d 处引用，支持率 %.2f" % (
                    ev.get("citations_located", 0), ev.get("support_rate", 0.0)))
            b = selfcheck.benchmark(verbose=False)
            log.append("埋错测试：%d/%d 检出" % (b["detected"], b["total"]))
        if stage in ("export", "all"):
            d = draft_mod.load_draft()
            chk = {}
            cpath = os.path.join(OUT_DIR, "selfcheck.json")
            if os.path.isfile(cpath):
                with open(cpath, encoding="utf-8") as f:
                    chk = json.load(f)
            docx_export.export(d, chk, verbose=False)
            docx_export.export_matrix_excel(verbose=False)
            log.append("已导出 Word 与 Excel")
        return JSONResponse({"ok": True, "log": log, "seconds": round(time.time() - t0, 1)})
    except Exception as e:
        return JSONResponse({"ok": False, "log": log, "error": str(e)[:300]},
                            status_code=500)


@app.post("/api/agent")
def run_agent_api(max_steps: int = 18):
    """让 Agent 自主审稿（模型自己决定调用哪些工具）。"""
    try:
        from .agent import run_agent, _compose_conclusion
        res = run_agent(verbose=False, max_steps=max_steps)
        report = res.get("report") or {}
        # 结论在这里合成，而不是在 agent 内部：agent 有多条早返回路径
        # （降级、快速模式、步数用尽），放在某一条里必然漏掉其余几条；
        # 接口是唯一的出口，放这里才覆盖全部情形。
        return JSONResponse({"ok": True, "status": res["status"],
                             "steps": res["steps"], "seconds": res["seconds"],
                             "tools_used": res["tools_used"],
                             "agent_summary": _compose_conclusion(
                                 res.get("agent_summary", ""), report),
                             "report": report,
                             "trace": res.get("trace", [])})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)


# ────────────────────────── 隐私与数据处理 ──────────────────────────

PRIVACY_FILE = os.path.join(DATA_DIR, "privacy.json")


def _load_privacy() -> dict:
    """读取用户的处理方式选择。默认 cloud（调用阿里云），但界面必须先告知。

    文件不存在时**把默认值落盘**（而不是只在内存里给一份默认值）。
    否则会出现"data/ 里没有 privacy.json"这种半截状态：备份/快照里也就没有它，
    一旦数据目录被重建，用户先前选过的「仅本地」就无从恢复（实测：清空数据后
    该文件消失，相关断言随之失败，排查起来像是"设置被吞了"）。
    """
    try:
        with open(PRIVACY_FILE, encoding="utf-8") as f:
            d = json.load(f)
        mode = d.get("mode", "cloud")
        # 文件可能被手工改坏或来自旧版本：非法值一律归一化为 cloud，
        # 否则界面会卡在一个无法识别的模式上（此前的 400 就是这样暴露出来的）。
        if mode not in ("cloud", "local"):
            mode = "cloud"
        return {"mode": mode,
                "acknowledged": bool(d.get("acknowledged")),
                "acknowledged_at": d.get("acknowledged_at", "")}
    except Exception:
        d = {"mode": "cloud", "acknowledged": False, "acknowledged_at": ""}
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(PRIVACY_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return d


def _save_privacy(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PRIVACY_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


@app.get("/api/privacy")
def get_privacy():
    p = _load_privacy()
    p["llm_configured"] = bool(settings.api_key)
    return JSONResponse(p)


@app.get("/api/privacy/set")
def set_privacy_get(mode: str = "cloud"):
    """用普通页面跳转也能设置（不依赖 fetch，兼容任何浏览器/代理环境）。"""
    if mode not in ("cloud", "local"):
        mode = "cloud"
    p = {"mode": mode, "acknowledged": True,
         "acknowledged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save_privacy(p)
    os.environ["WM_OFFLINE"] = "1" if mode == "local" else "0"
    try:
        settings.force_offline = (mode == "local")
    except Exception:
        pass
    label = "云端处理" if mode == "cloud" else "仅本地处理"
    # 这个页面是"fetch 走不通时的兜底路径"。以前它只给一句"可以关闭本页"
    # 和一个「返回应用」链接——用户被丢在一个陌生的独立页面上，要自己点回去，
    # 体验上像是"应用断了"（实测被明确反馈为"很乱、断开了"）。
    # 现在改成：说明清楚 + **自动返回应用**，并带上成功提示让首页弹出确认。
    return HTMLResponse(
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>已切换</title>"
        "<meta http-equiv='refresh' content='2;url=/?mode_changed=%s'></head>"
        "<body style='font-family:-apple-system,\"Microsoft YaHei\",sans-serif;"
        "background:#f5f7fa;display:flex;align-items:center;justify-content:center;"
        "height:100vh;margin:0'>"
        "<div style='background:#fff;border-radius:14px;padding:36px 44px;text-align:center;"
        "box-shadow:0 8px 28px rgba(0,0,0,.08);max-width:520px'>"
        "<div style='font-size:38px;color:#0d7a4a;margin-bottom:8px'>✓</div>"
        "<h2 style='margin:0 0 8px;font-size:19px'>已切换为「%s」</h2>"
        "<p style='color:#737d8c;font-size:14px;margin:0 0 18px'>"
        "正在自动返回应用…若没有自动跳转，请点下面的按钮。</p>"
        "<a href='/?mode_changed=%s' style='display:inline-block;background:#2563d6;color:#fff;"
        "text-decoration:none;padding:11px 26px;border-radius:10px;font-size:15px'>"
        "返回应用</a></div></body></html>" % (mode, label, mode))


@app.post("/api/privacy")
def set_privacy(mode: str = "cloud"):
    """mode: cloud（调用阿里云大模型，效果好）| local（完全本地，不联网）"""
    if mode not in ("cloud", "local"):
        return JSONResponse({"ok": False, "error": "mode 只能是 cloud 或 local"},
                            status_code=400)
    p = {"mode": mode, "acknowledged": True,
         "acknowledged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save_privacy(p)
    # local 模式通过环境变量立即生效（选择已落盘，重启后仍生效）。
    # 三条设置路径（POST 本接口 / GET /api/privacy/set / POST /api/privacy/save）
    # 必须用同一套机制，否则会出现"磁盘已改、界面没变"这种自相矛盾的状态。
    os.environ["WM_OFFLINE"] = "1" if mode == "local" else "0"
    try:
        settings.force_offline = (mode == "local")
    except Exception:
        pass
    return JSONResponse({"ok": True, **p})


@app.post("/api/delete")
async def delete_paper(request: Request,
                       local_pdf: str = None,
                       local_file: str = None,
                       local_pdf_form: str = Form(None, alias="local_pdf"),
                       local_file_form: str = Form(None, alias="local_file")):
    """删除单篇论文（含其本地文件与语料记录，支持 PDF / Word / 题录）。

    标识符同时接受**查询参数 / 表单字段 / JSON 体**三种来源。
    原先只声明成 Form(...)，而界面是用 `?local_pdf=...` 调的——FastAPI 不会
    从查询串里读 Form 参数，于是永远拿到空串、永远报「缺少文件标识」（实测：
    用户点删除，弹窗说"删除失败：缺少文件标识（local_pdf）"，而数据本身完好）。
    /api/run 早就为了同样的原因做过双收兼容，删除接口漏了。
    """
    key = ""
    for cand in (local_pdf, local_pdf_form, local_file, local_file_form):
        if cand and str(cand).strip():
            key = str(cand).strip()
            break
    if not key:
        try:
            body = await request.json()
            if isinstance(body, dict):
                key = str(body.get("local_pdf") or body.get("local_file") or "").strip()
        except Exception:
            pass
    if not key:
        return JSONResponse({"ok": False, "error": "缺少文件标识（local_pdf）"}, status_code=400)
    # 归一化：数据里存的是正斜杠（pdf/001.pdf），前端可能传反斜杠
    key = key.replace("\\", "/")
    corpus = load_corpus()
    records = list(corpus.get("records", []))
    target = [r for r in records
              if (r.get("local_pdf") or "") == key
              or (r.get("local_file") or "") == key]
    if not target:
        return JSONResponse({"ok": False, "error": "未找到该论文"}, status_code=404)

    # 只允许删除 data/pdf 或 data/docx 下的文件，防止越权路径
    name = os.path.basename(key)
    sub = "docx" if name.lower().endswith(".docx") else "pdf"
    base_dir = os.path.join(DATA_DIR, sub)
    path = os.path.join(base_dir, name)
    if os.path.abspath(path).startswith(os.path.abspath(base_dir)) and os.path.isfile(path):
        try:
            os.remove(path)
        except Exception as e:
            return JSONResponse({"ok": False, "error": "删除文件失败：%s" % str(e)[:80]},
                                status_code=500)

    # 用归一化后的 key 过滤（不能再用原始参数名：Word 记录的标识来自 local_file，
    # 只比对 local_pdf 会漏删，出现"接口说成功、列表里却还在"）
    corpus["records"] = [r for r in records
                         if (r.get("local_pdf") or "") != key
                         and (r.get("local_file") or "") != key]
    corpus["count"] = len(corpus["records"])
    with open(os.path.join(DATA_DIR, "corpus.json"), "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    # 同步 docs：删掉的那篇若仍留在 docs 里，docs 数会与"非题录篇数"不一致，
    # 新鲜度会被误判为"需要重新生成"（实测踩到过）。
    try:
        from .literature import _sync_docs
        _sync_docs()
    except Exception:
        pass
    # 解析/卡片/矩阵与文献库不再一致，界面会提示重新生成
    return JSONResponse({"ok": True, "removed": name, "total": corpus["count"]})


@app.get("/api/papers")
def list_papers():
    """列出文献库，供界面管理（含单篇删除）。

    额外返回**解析状态**：用户最需要知道的是"这篇到底读进来了没有"。
    没有它，一篇解析失败的 PDF 会让自检报告显示"该引用无法核对原文"，
    用户会误以为是自己的引用写错了——所以这里把状态摊开说清楚。
    """
    corpus = load_corpus()
    # 解析结果的匹配**按文件路径**，不按标题。
    # 为什么：上传的 Word/PDF 在语料库里记的是**文件名**，而解析后 docs 里的
    # 标题可能是**文档内标题**（实测：文件名"冒菜19秒宣传视频拍摄策划案"、
    # 文档内标题"重庆人文科技学院｜帽牌货…"），按标题精确匹配必然落空，
    # 于是那篇永远显示"待解析"、每次生成都重复解析、卡片标题与清单还对不上。
    # 路径才是稳定的身份；标题匹配只作为老数据的兜底。
    by_path, by_title = {}, {}
    for d in load_docs().get("docs", []):
        p = (d.get("local_file") or d.get("local_pdf") or "").replace("\\", "/")
        if p:
            by_path[p] = d
        t = d.get("title")
        if t:
            by_title[t] = d
    out = []
    for i, r in enumerate(corpus.get("records", []), 1):
        title = r.get("title")
        rel = (r.get("local_file") or r.get("local_pdf") or "").replace("\\", "/")
        doc = by_path.get(rel) or by_title.get(title) or {}
        kind = (r.get("doc_type") or "pdf")
        if kind == "ref":
            status, note = "题录", "只有元数据，无全文，无法核对原文"
        elif doc.get("parse_error"):
            status, note = "失败", doc.get("parse_error")[:60]
        elif not doc:
            status, note = "待解析", "尚未解析（点『开始生成综述稿』时会处理）"
        elif not (doc.get("full_text") or "").strip():
            status, note = "无正文", "解析成功但没抽到正文（可能是扫描件或纯图片 PDF）"
        elif _looks_like_non_paper(doc):
            # 论文一定有章节（摘要/引言/方法/结论…）与参考文献表。两者全无，
            # 基本可以断定传进来的不是论文（实测：用户把"宣传视频策划案"传了进来，
            # 界面上只显示"就绪"，看不出问题，还会被当成综述素材）。
            status, note = "非论文", ("读进来 %d 字，但没识别到任何论文章节与参考文献——"
                                   "这份文件看起来不是学术论文，建议删除"
                                   % (doc.get("chars_total") or 0))
        else:
            status, note = "就绪", "可参与卡片抽取与引用核对"
        out.append({
            "index": i,
            "local_pdf": r.get("local_pdf"),
            "title": title,
            "year": r.get("year"),
            "venue": r.get("venue"),
            "source": r.get("source") or "OpenAlex",
            "size_mb": round((r.get("pdf_bytes") or 0) / 1048576, 2),
            "kind": kind,
            # ↓ 解析状态（可为界面直接展示）
            "status": status,
            "status_note": note,
            "chars": doc.get("chars_total") or 0,
            "sections": len(doc.get("sections_found") or []),
            "ref_count": doc.get("ref_count") or 0,
            "ok": status == "就绪",
        })
    return JSONResponse({"papers": out, "count": len(out)})


@app.get("/api/find")
def find_paper(title: str = "", limit: str = "5"):
    """按标题在 OpenAlex 搜索候选文献（用于补齐"库里没有"的引用）。

    为什么需要它：检查稿子时会报"这条参考文献未在库中找到，无法核对原文"，
    但用户此前**没有任何下一步**——只能自己去别处找 PDF 再回来上传。
    这里直接给出候选与可下载链接，形成一个闭环。
    """
    title = (title or "").strip()
    if len(title) < 4:
        return JSONResponse({"ok": False, "error": "标题太短", "candidates": []},
                            status_code=400)
    try:
        n = max(1, min(int(str(limit) or 5), 10))
    except Exception:
        n = 5
    try:
        import urllib.parse as _up
        import urllib.request as _ur
        params = {
            "search": title,
            "filter": "has_abstract:true",
            "per-page": str(n),
            "mailto": "student@example.edu",
        }
        url = "https://api.openalex.org/works?" + _up.urlencode(params)
        req = _ur.Request(url, headers={"User-Agent": "WenMai/1.0 (mailto:student@example.edu)"})
        # 外部检索接口不稳定：实测碰到过 OpenAlex 返回 504 Gateway Timeout。
        # 单次直连会把一次远端抖动直接变成用户看到的 "HTTP Error 504"，
        # 因此重试 3 次（递增等待），失败后再退到 Crossref 取候选。
        data = None
        last_err = ""
        for attempt in range(3):
            try:
                with _ur.urlopen(req, timeout=25) as r:
                    data = json.load(r)
                break
            except Exception as e:
                last_err = str(e)[:120]
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        if data is None:
            # 备用源：Crossref（返回结构与 OpenAlex 不同，这里只映射需要的字段）
            try:
                xref = ("https://api.crossref.org/works?rows=%d&query.bibliographic=%s"
                        % (n, _up.quote(title)))
                xr = _ur.Request(xref, headers={
                    "User-Agent": "WenMai/1.0 (mailto:student@example.edu)"})
                with _ur.urlopen(xr, timeout=25) as r:
                    xd = json.load(r)
                cands = []
                for it in (xd.get("message", {}).get("items") or [])[:n]:
                    cands.append({
                        "id": it.get("DOI"),
                        "title": ((it.get("title") or [""])[0] or "").strip(),
                        "year": ((it.get("issued", {}).get("date-parts") or [[None]])[0] or [None])[0],
                        "venue": ((it.get("container-title") or [""])[0] or ""),
                        "cited_by_count": it.get("is-referenced-by-count") or 0,
                        "doi": ("https://doi.org/" + it["DOI"]) if it.get("DOI") else None,
                        "pdf_url": "",
                        "is_oa": False,
                    })
                if cands:
                    return JSONResponse({"ok": True, "query": title, "candidates": cands,
                                         "count": len(cands), "source": "Crossref",
                                         "note": "OpenAlex 暂时不可用，已改用 Crossref 检索"})
            except Exception as e2:
                last_err = "%s；备用源 Crossref 也失败：%s" % (last_err, str(e2)[:80])
            return JSONResponse({"ok": False,
                                 "error": "检索服务暂时不可用（已重试 3 次）：%s" % last_err,
                                 "hint": "这是外部文献检索接口的问题，稍后再试即可；"
                                         "也可以直接把自己的 PDF 拖进页面。",
                                 "candidates": []}, status_code=503)
        cands = []
        for w in data.get("results", [])[:n]:
            loc = w.get("best_oa_location") or {}
            src = ((w.get("primary_location") or {}).get("source") or {})
            cands.append({
                "id": w.get("id"),
                "title": (w.get("title") or "").strip(),
                "year": w.get("publication_year"),
                "venue": src.get("display_name") or "",
                "cited_by_count": w.get("cited_by_count") or 0,
                "doi": w.get("doi"),
                "pdf_url": loc.get("pdf_url") or "",
                "is_oa": bool((w.get("open_access") or {}).get("is_oa")),
            })
        return JSONResponse({"ok": True, "query": title,
                             "candidates": cands, "count": len(cands)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:150], "candidates": []},
                            status_code=500)


@app.post("/api/find/import")
def import_found(work_id: str = Form("")):
    """把搜索到的某篇文献下载并加入文献库。"""
    wid = (work_id or "").strip()
    if not wid:
        return JSONResponse({"ok": False, "error": "缺少 work_id"}, status_code=400)
    try:
        from . import literature as _lit
        rec = _lit.import_openalex_work(wid)
        if not rec:
            return JSONResponse({"ok": False,
                                 "error": "这篇没有可下载的开放获取全文"}, status_code=404)
        return JSONResponse({"ok": True, "title": rec.get("title"),
                             "year": rec.get("year"),
                             "size_mb": round((rec.get("pdf_bytes") or 0) / 1048576, 2)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=500)


# ────────────────── 我的稿子：独立存储与检查 ──────────────────
# 与"上传参考论文"分开：稿子是被检查的对象，不是生成综述的素材，
# 因此独立存放、**不进入文献库**，避免污染卡片/矩阵/引用编号。

DRAFT_DIR = os.path.join(DATA_DIR, "mydraft")
DRAFT_REPORT = os.path.join(OUT_DIR, "mydraft_report.json")


def _draft_paths():
    return (os.path.join(DRAFT_DIR, "my_draft.docx"),
            os.path.join(DRAFT_DIR, "my_draft.json"))


@app.post("/api/mydraft/upload")
async def upload_my_draft(files: List[UploadFile] = File(...)):
    """上传"我的稿子"（只保留一份，独立存放）。"""
    os.makedirs(DRAFT_DIR, exist_ok=True)
    docx_path, json_path = _draft_paths()
    got = None
    for f in files or []:
        name = os.path.basename(getattr(f, "filename", "") or "")
        data = await f.read()
        low = name.lower()
        if low.endswith(".docx") and data.startswith(b"PK\x03\x04"):
            got = (name, data)
            break
        if low.endswith(".pdf") and data.startswith(b"%PDF"):
            return JSONResponse(
                {"ok": False,
                 "error": "稿子请上传 Word（.docx）。PDF 难以可靠切分章节与参考文献区，"
                          "可在 Word 里另存为 .docx 后再上传。"}, status_code=400)
        if low.endswith(".doc") and data.startswith(b"\xd0\xcf\x11\xe0"):
            return JSONResponse({"ok": False, "error": "旧版 .doc 不支持，请另存为 .docx"},
                                status_code=400)
    if not got:
        return JSONResponse({"ok": False, "error": "没有收到可用的 .docx 文件"},
                            status_code=400)

    name, data = got
    with open(docx_path, "wb") as f:
        f.write(data)
    try:
        from .docx_parse import parse_docx
        rec = parse_docx(docx_path, title_hint=name)
        rec["original_name"] = name
        rec["uploaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        return JSONResponse({"ok": True, "name": name,
                             "title": rec.get("title"),
                             "chars": rec.get("chars_total"),
                             "ref_count": rec.get("ref_count"),
                             "sections": rec.get("sections_found")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": "解析失败：%s" % str(e)[:150]},
                            status_code=500)


@app.get("/api/mydraft")
def get_my_draft():
    """当前稿子状态 + 最近一次检查结果。"""
    docx_path, json_path = _draft_paths()
    if not os.path.isfile(json_path):
        return JSONResponse({"has_draft": False})
    try:
        with open(json_path, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return JSONResponse({"has_draft": False})
    out = {"has_draft": True,
           "name": rec.get("original_name"),
           "title": rec.get("title"),
           "chars": rec.get("chars_total"),
           "ref_count": rec.get("ref_count"),
           "sections": rec.get("sections_found") or [],
           "uploaded_at": rec.get("uploaded_at")}
    if os.path.isfile(DRAFT_REPORT):
        try:
            with open(DRAFT_REPORT, encoding="utf-8") as f:
                out["report"] = json.load(f)
        except Exception:
            pass
    return JSONResponse(out)


@app.post("/api/mydraft/delete")
def delete_my_draft():
    docx_path, json_path = _draft_paths()
    removed = []
    for p in (docx_path, json_path, DRAFT_REPORT):
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed.append(os.path.basename(p))
            except Exception:
                pass
    return JSONResponse({"ok": True, "removed": removed})


@app.post("/api/mydraft/check")
def check_my_draft():
    """对"我的稿子"做引用自检，产出针对该稿的报告。"""
    docx_path, json_path = _draft_paths()
    if not os.path.isfile(json_path):
        return JSONResponse({"ok": False, "error": "还没有上传稿子"}, status_code=400)
    try:
        from .draft_check import check_my_draft as _run
        return JSONResponse({"ok": True, **_run(verbose=False)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:250]}, status_code=500)



@app.get("/api/evidence/detail")
def evidence_detail(citation: str = "0"):
    """查看某处引用的完整证据（Agent 与自检依据的原文段落）。

    citation 先收成字符串再自行解析：若用 int 声明，传 citation=abc 会被
    FastAPI 以 422 直接拒绝，界面上只看到一个没头没尾的报错；
    这里改为宽容解析，非法值当 0 处理，由业务逻辑给出可读提示。
    """
    try:
        n = int(str(citation).strip() or 0)
    except Exception:
        n = 0
    try:
        from .selfcheck import tool_find_evidence
        return JSONResponse(tool_find_evidence(n, use_llm=False))
    except Exception as e:
        return JSONResponse({"error": str(e)[:200], "evidence": []})


@app.post("/api/refs")
def add_refs(text: str = Form(""), name: str = Form("粘贴的题录")):
    """从题录文本导入文献（支持知网/CNKI/RefWorks 导出格式）。"""
    try:
        from .refparse import parse_refs, append_to_corpus
        recs = parse_refs(text or "")
        if not recs:
            return JSONResponse({"ok": False,
                                 "error": "未能解析出题录。支持 RefWorks 标签式（A1:/T1:/JF:）与【字段】键值式。"},
                                status_code=400)
        added = append_to_corpus(recs, original_name=name or "粘贴的题录")
        return JSONResponse({"ok": True, "parsed": len(recs), "added": added,
                             "items": [{"title": r.get("title"), "authors": r.get("authors"),
                                        "year": r.get("year"), "venue": r.get("venue"),
                                        "ref_type": r.get("ref_type")} for r in recs[:20]]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:250]}, status_code=500)


@app.get("/api/missing")
def missing_citations():
    """该引未引检测结果。"""
    try:
        from .selfcheck import run_missing_citation_check
        return JSONResponse(run_missing_citation_check(verbose=False))
    except Exception as e:
        return JSONResponse({"total": 0, "suggestions": [], "error": str(e)[:200]})


@app.get("/api/download/docx")
def download_docx():
    if not os.path.isfile(DOCX_OUT):
        return JSONResponse({"ok": False, "error": "尚未生成"}, status_code=404)
    return FileResponse(DOCX_OUT, filename="文脉_综述初稿.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.get("/api/download/matrix")
def download_matrix():
    for name in ("文献对比矩阵.xlsx", "文献对比矩阵.csv"):
        p = os.path.join(OUT_DIR, name)
        if os.path.isfile(p):
            return FileResponse(p, filename=name)
    return JSONResponse({"ok": False, "error": "尚未生成"}, status_code=404)


# ────────────────────────── 用户上传自己的论文 ──────────────────────────

@app.post("/api/upload")
async def upload_papers(files: List[UploadFile] = File(...)):
    """上传论文：支持 PDF 与 Word（.docx）。

    - PDF  → 存 data/pdf，用 PyMuPDF 解析
    - Word → 存 data/docx，用 python-docx 解析（无需转 PDF）
    上传后写入语料库，可直接参与后续流程。
    """
    saved, skipped = [], []
    os.makedirs(PDF_DIR, exist_ok=True)
    docx_dir = os.path.join(DATA_DIR, "docx")
    os.makedirs(docx_dir, exist_ok=True)

    corpus = load_corpus()
    records = list(corpus.get("records", []))
    used = set()
    for r in records:
        m = os.path.basename(r.get("local_pdf") or r.get("local_file") or "")
        if m:
            used.add(m)

    for f in files or []:
        name = os.path.basename(getattr(f, "filename", "") or "")
        low = name.lower()
        data = await f.read()

        is_pdf = low.endswith(".pdf") and data.startswith(b"%PDF")
        # .docx 是 zip 包，魔数为 PK\x03\x04
        is_docx = low.endswith(".docx") and data.startswith(b"PK\x03\x04")
        is_doc_legacy = low.endswith(".doc") and data.startswith(b"\xd0\xcf\x11\xe0")

        if is_doc_legacy:
            skipped.append("%s（旧版 .doc 格式不支持，请另存为 .docx 或 PDF）" % name)
            continue
        if not (is_pdf or is_docx):
            if low.endswith((".pdf", ".docx")):
                skipped.append("%s（文件内容与扩展名不符，可能已损坏）" % name)
            else:
                skipped.append("%s（只支持 PDF 与 Word .docx）" % name)
            continue

        # 分配不冲突的文件名
        target_dir = PDF_DIR if is_pdf else docx_dir
        ext = ".pdf" if is_pdf else ".docx"
        idx = 1
        while ("%03d%s" % (idx, ext)) in used:
            idx += 1
        target = "%03d%s" % (idx, ext)
        used.add(target)
        path = os.path.join(target_dir, target)
        with open(path, "wb") as out:
            out.write(data)

        rel = ("pdf/" if is_pdf else "docx/") + target
        records.append({
            "id": None,
            "title": os.path.splitext(name)[0][:120],
            "year": None,
            "venue": "",
            "authors": [],
            "language": None,
            "cited_by_count": 0,
            "doi": None,
            "abstract": "",
            "pdf_url": "",
            "local_pdf": rel,
            "local_file": rel,
            "doc_type": "pdf" if is_pdf else "docx",
            "pdf_bytes": len(data),
            "source": "用户上传",
            "original_name": name,
        })
        saved.append(name)

    corpus["records"] = records
    corpus["count"] = len(records)
    corpus.setdefault("query", "")
    corpus["uploaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(DATA_DIR, "corpus.json"), "w", encoding="utf-8") as out:
        json.dump(corpus, out, ensure_ascii=False, indent=2)
    return JSONResponse({"ok": True, "saved": saved, "skipped": skipped,
                         "total": len(records)})


@app.get("/api/example")
def example_files():
    """列出内置的示例论文，供"一键试用"使用。"""
    corpus = load_corpus()
    out = []
    for r in corpus.get("records", []):
        out.append({"title": r.get("title"), "year": r.get("year"),
                    "venue": r.get("venue"), "source": r.get("source", "OpenAlex")})
    return JSONResponse({"files": out, "count": len(out)})


@app.post("/api/clear")
def clear_papers():
    """清空文献库，并**一并删除由它派生的全部结果**。

    原先只删 corpus/docs/cards/matrix 四个文件，后果（实测被用户发现）：
      · outputs/ 里的综述稿、自检报告、Word、Excel 全都留着，
        界面上"研究现状"照旧显示——用户点了"清空重来"却发现没清干净；
      · data/pdf 里的原文也留着，下次上传的 001.pdf 会被判为"已存在"
        而**跳过不覆盖**，新论文的内容其实是旧的。
    用户按下这个按钮的预期是"回到干净状态"，所以派生结果必须一起清掉。

    刻意**不动**的：data/mydraft（那是"我的稿子"，独立于文献库，由它自己的
    删除按钮管理）、data/privacy.json（处理方式与知情确认是系统设置，不是数据）、
    成本台账（当天花费是审计记录，清空数据不该抹掉它）。
    """
    corpus = {"query": "", "count": 0, "records": []}
    with open(os.path.join(DATA_DIR, "corpus.json"), "w", encoding="utf-8") as out:
        json.dump(corpus, out, ensure_ascii=False, indent=2)

    removed = []
    # 1) 库与中间结果
    for name in ("docs.json", "cards.json", "matrix.json"):
        p = os.path.join(DATA_DIR, name)
        if os.path.isfile(p):
            os.remove(p)
            removed.append(name)
    # 2) 派生输出（综述稿 / 自检 / 证据核对 / Word / Excel / Agent 轨迹）
    for name in ("draft.json", "draft.md", "selfcheck.json", "selfcheck_benchmark.json",
                 "evidence_check.json", "missing_citations.json", "agent_trace.json",
                 "文脉_综述初稿.docx", "文献对比矩阵.xlsx", "文献对比矩阵.csv",
                 "mydraft_report.json"):
        p = os.path.join(OUT_DIR, name)
        if os.path.isfile(p):
            os.remove(p)
            removed.append(name)
    # 3) 论文原文（PDF / Word）：留着会让下一次上传静默复用旧文件
    files = 0
    for sub in ("pdf", "docx"):
        d = os.path.join(DATA_DIR, sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    files += 1
                except Exception:
                    pass
    return JSONResponse({"ok": True, "removed": removed, "removed_files": files})
