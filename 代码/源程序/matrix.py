"""对比矩阵：把文献卡片拼成一张横向对比表。

同时产出：
- 结构化矩阵（JSON / Excel 导出用）
- 主题聚类分组（按关键词共现，无需外部模型）
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict

from . import textutil
from .cards import load_cards
from .config import MATRIX_JSON

COLUMNS = [
    ("index", "#"),
    ("title", "文献"),
    ("year", "年份"),
    ("cited_by_count", "被引"),
    ("problem", "解决的问题"),
    ("method", "方法"),
    ("datasets", "数据集"),
    ("metrics", "关键结果"),
    ("limitation", "局限"),
]


def _cell(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, list):
        return "；".join(str(x) for x in v) if v else "—"
    s = str(v).strip()
    return s if s else "—"


def build_matrix(verbose: bool = True) -> dict:
    data = load_cards()
    cards = data.get("cards", [])
    rows = []
    for c in cards:
        f = c.get("fields", {})
        row = {
            "index": c.get("index"),
            "title": (c.get("title") or "")[:110],
            "year": c.get("year"),
            "cited_by_count": c.get("cited_by_count"),
            "problem": f.get("problem"),
            "method": f.get("method"),
            "datasets": f.get("datasets"),
            "metrics": f.get("metrics"),
            "limitation": f.get("limitation"),
            "takeaway": f.get("takeaway"),
            "source": c.get("source"),
        }
        rows.append(row)

    topics = cluster_topics(cards)
    out = {"count": len(rows), "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "columns": [{"key": k, "label": lbl} for k, lbl in COLUMNS],
           "rows": rows, "topics": topics}
    with open(MATRIX_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if verbose:
        print("[矩阵] %d 篇 × %d 列 → %s" % (len(rows), len(COLUMNS), MATRIX_JSON))
        for t in topics:
            print("   主题「%s」：%d 篇" % (t["name"], len(t["members"])))
    return out


def cluster_topics(cards: list, top_k: int = 4) -> list:
    """关键词共现聚类：先统计高频词，再把文献分到词频最高的主题。

    会过滤分词残片（如 "classi"、"recognitio" 这类被截断的词）与过泛词，
    避免产出"主题：classi"这种无意义的分组名。
    """
    if not cards:
        return []
    doc_tokens = []
    global_cnt = Counter()
    for c in cards:
        f = c.get("fields", {})
        blob = " ".join(filter(None, [
            (c.get("title") or ""), (f.get("method") or ""),
            (f.get("problem") or ""), " ".join(f.get("datasets") or [])]))
        toks = textutil.tokenize(blob)
        doc_tokens.append(set(toks))
        global_cnt.update(toks)

    generic = {"learning", "model", "models", "method", "methods", "approach", "network",
               "networks", "image", "images", "classification", "results", "dataset",
               "datasets", "training", "features", "based", "using", "proposed", "paper",
               "结果", "方法", "模型", "研究", "问题", "数据", "分析", "实验", "本文", "工作"}

    def usable(w: str) -> bool:
        if w in generic or len(w) < 4:
            return False
        # 分词残片：以辅音字母或孤立 i 结尾的英文长词，多半是被截断的
        if re.fullmatch(r"[a-z]{4,}[bcdfghjklmnpqrstvwxz]", w):
            return False
        if re.fullmatch(r"[a-z]{4,}i", w):
            return False
        return True

    cands = [w for w, _ in global_cnt.most_common(80) if usable(w)][:top_k * 4]

    chosen = []
    for w in cands:
        if any(w in c or c in w for c in chosen):
            continue
        chosen.append(w)
        if len(chosen) >= top_k:
            break

    topics = []
    for w in chosen:
        members = [i for i, toks in enumerate(doc_tokens) if w in toks]
        if len(members) >= 2:
            topics.append({"name": w, "members": members,
                           "titles": [(cards[i].get("title") or "")[:70] for i in members[:6]]})
    topics.sort(key=lambda t: -len(t["members"]))
    return topics


def to_excel(path: str) -> str:
    """导出 Excel（缺 openpyxl 时退化为 CSV）。"""
    data = _load_matrix()
    rows, cols = data.get("rows", []), data.get("columns", [])
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "文献对比矩阵"
        ws.append([c["label"] for c in cols])
        for r in rows:
            ws.append([_cell(r.get(c["key"])) for c in cols])
        for i, c in enumerate(cols, 1):
            width = 14 if c["key"] in ("index", "year", "cited_by_count") else 40
            ws.column_dimensions[chr(64 + i)].width = width
        ws.freeze_panes = "A2"
        wb.save(path)
        return path
    except Exception:
        import csv
        csv_path = os.path.splitext(path)[0] + ".csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([c["label"] for c in cols])
            for r in rows:
                w.writerow([_cell(r.get(c["key"])) for c in cols])
        return csv_path


def _load_matrix() -> dict:
    if not os.path.isfile(MATRIX_JSON):
        return {"rows": [], "columns": []}
    with open(MATRIX_JSON, encoding="utf-8") as f:
        return json.load(f)


def load_matrix() -> dict:
    return _load_matrix()
