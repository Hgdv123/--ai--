"""配置：从 .env / 环境变量读取，全部有默认值（缺失也能跑）。"""
from __future__ import annotations

import os

# ── 路径 ──────────────────────────────────────────────────────────
PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _detect_root() -> str:
    """项目根目录 = 包含 pyproject.toml 的最近上层目录；找不到则用包目录的父目录。

    这样数据目录始终是 <root>/data，而不是 <root>/wenmai/data（避免多套一层）。
    """
    cur = PKG_DIR
    for _ in range(4):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        if os.path.isfile(os.path.join(parent, "pyproject.toml")):
            return parent
        cur = parent
    return os.path.dirname(PKG_DIR)


# 项目根可用 WM_ROOT 显式指定：交付包把代码放进「代码/」子目录后，
# 数据与产物仍留在包根，靠这一个变量指回去。
# 不设时按老规则自动探测——所以单独跑源码目录也照常工作。
PROJECT_DIR = os.environ.get("WM_ROOT", "").strip() or _detect_root()
if not os.path.isabs(PROJECT_DIR):
    PROJECT_DIR = os.path.abspath(PROJECT_DIR)
DATA_DIR = os.path.join(PROJECT_DIR, "data")
PDF_DIR = os.path.join(DATA_DIR, "pdf")
OUT_DIR = os.path.join(PROJECT_DIR, "outputs")
WEB_DIR = os.path.join(PKG_DIR, "web")
# .env 同理可用 WM_ENV_FILE 指定（存放位置与项目根解耦）
ENV_FILE = os.environ.get("WM_ENV_FILE", "").strip() or os.path.join(PROJECT_DIR, ".env")

CORPUS_JSON = os.path.join(DATA_DIR, "corpus.json")
DOCS_JSON = os.path.join(DATA_DIR, "docs.json")
CARDS_JSON = os.path.join(DATA_DIR, "cards.json")
MATRIX_JSON = os.path.join(DATA_DIR, "matrix.json")
DRAFT_MD = os.path.join(OUT_DIR, "draft.md")
CHECK_JSON = os.path.join(OUT_DIR, "selfcheck.json")
DOCX_OUT = os.path.join(OUT_DIR, "文脉_综述初稿.docx")


def _load_env_file(path: str = ENV_FILE) -> None:
    """极简 .env 解析（不引入 python-dotenv 依赖）。已存在的环境变量优先。"""
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_env_file()


def key_problem(raw: str) -> str:
    """Key 不可用时返回原因（可照做的中文说明），可用或未填则返回空串。

    放在 config 而不是 llm 里：settings.llm_available 与 LLMClient 都要用它，
    两处判据必须一致——否则会出现"状态条说云端、实际走本地"的自相矛盾。

    为什么必须校验：HTTP 头由 urllib 按 latin-1 编码，Key 里只要有非 ASCII
    字符就会抛 UnicodeEncodeError。交付包 `.env` 里放的是中文占位符
    `sk-在此填入你的百炼APIKey`，不校验就会被当成可用 Key。
    """
    key = (raw or "").strip()
    if not key:
        return ""                              # 没填是正常情况，不算问题
    bad = [c for c in key if ord(c) > 126 or ord(c) < 33]
    if bad:
        return ("API Key 含非 ASCII 或空白字符（%r…），HTTP 头无法发送；"
                "请到百炼控制台复制真实的 Key 填入 .env 的 DASHSCOPE_API_KEY。"
                % "".join(bad[:4]))
    if not key.startswith("sk-"):
        return ("API Key 不像百炼的 Key（应以 sk- 开头）；"
                "请到百炼控制台复制真实的 Key。")
    return ""


def key_usable(raw: str) -> bool:
    """Key 是否真的可用（非空、纯 ASCII、形如 sk-…）。"""
    k = (raw or "").strip()
    return bool(k) and not key_problem(k)


class Settings:
    """运行时配置。所有项都有默认值——没有 .env 也能完整运行。"""

    api_key: str = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    base_url: str = os.environ.get("DASHSCOPE_BASE_URL",
                                  "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model_default: str = os.environ.get("MODEL_DEFAULT", "qwen-plus")
    model_cheap: str = os.environ.get("MODEL_CHEAP", "qwen-turbo")
    model_strong: str = os.environ.get("MODEL_STRONG", "qwen-max")

    enable_cache: bool = os.environ.get("ENABLE_CACHE", "true").lower() != "false"
    daily_cost_limit: float = float(os.environ.get("DAILY_COST_LIMIT_YUAN", "20") or 20)

    @property
    def force_offline(self) -> bool:
        """离线开关：设置 WM_OFFLINE=1 后**立即生效**。

        不能写成类属性——那会在导入时求值一次，
        之后（例如用户在界面上切换为"仅本地处理"）改环境变量将不再生效。
        """
        return os.environ.get("WM_OFFLINE", "0") == "1"

    @force_offline.setter
    def force_offline(self, value: bool) -> None:
        """兼容旧调用：设置属性等价于设置环境变量。"""
        os.environ["WM_OFFLINE"] = "1" if value else "0"

    @property
    def llm_available(self) -> bool:
        """是否具备调用云端大模型的条件（Key **可用** 且未强制离线）。

        注意必须用 key_usable() 而不是 `bool(api_key)`：
        HTTP 头由 urllib 按 latin-1 编码，中文占位符 Key 会在发请求时抛
        UnicodeEncodeError。若这里只判"非空"，交付包的占位符会被当成可用，
        状态条就会显示「云端处理（百炼增强）」，而实际全部走了本地模型——
        界面在误导用户（实测踩到，交付包 `.env` 正是中文占位符）。
        """
        return key_usable(self.api_key) and not self.force_offline

    def describe(self) -> dict:
        # 离线时区分"有本地模型"与"只能用规则引擎"——两者能力差别很大
        if self.llm_available:
            service = "百炼（增强）"
        else:
            from . import local_llm
            service = ("本地部署模型（离线）"
                       if local_llm.available("gen") else "本地引擎（无模型，规则兜底）")
        return {
            "模型服务": service,
            "默认模型": self.model_default,
            "缓存": "开" if self.enable_cache else "关",
            "单日成本上限": "%.0f 元" % self.daily_cost_limit,
            "数据目录": DATA_DIR,
            "输出目录": OUT_DIR,
        }


settings = Settings()


def ensure_dirs() -> None:
    for d in (DATA_DIR, PDF_DIR, OUT_DIR):
        os.makedirs(d, exist_ok=True)
