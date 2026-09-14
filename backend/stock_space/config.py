"""配置系统: TOML 文件 + 环境变量 + 运行期设置 三层合并。

优先级(高 → 低):
  1. 运行期设置 ``data/runtime_settings.json`` —— 用户在网页「设置」页保存的内容
     (API Key、企业微信 Webhook、手工输入的上游地址等), 不入版本库;
  2. 环境变量 —— ``SS_`` 前缀, 嵌套用双下划线, 例如 ``SS_SERVER__PORT=9000``;
  3. 配置文件 ``config/app.toml``(可选) → ``config/sources.toml``(源目录)。

刻意不引入 pydantic-settings 之类的重依赖: 纯标准库 + tomli 兜底, 便于离线部署。
"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from .paths import CONFIG_DIR, DATA_DIR

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - 3.10 环境
    try:
        import tomli as _toml  # type: ignore
    except ModuleNotFoundError:  # 极端兜底: 完全没有 TOML 解析器
        _toml = None  # type: ignore

ENV_PREFIX = "SS_"
_ENV_NESTED_SEP = "__"


# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #
DEFAULTS: dict[str, Any] = {
    "server": {
        # 监听地址与端口均可被环境变量覆盖; 0.0.0.0 是为了容器/云服务器可达
        "host": "0.0.0.0",
        "port": 8770,
        "root_path": "",
        "workers": 1,
        "cors_origins": [],          # 空 = 只允许同源; 需要跨域时填白名单
        "trusted_hosts": [],
        "docs_enabled": True,
    },
    "app": {
        "title": "StockSpace 股票决策平台",
        "timezone": "Asia/Shanghai",
        # 推送消息里的“点击查看详情”链接前缀。留空则由请求 Host 自动推导,
        # 因此不需要写死任何域名。
        "public_base_url": "",
        "disclaimer": "本平台所有数据、评分、信号与回测结果仅供研究学习, 不构成任何投资建议。",
    },
    "quotas": {
        # 全市场样本量: 0 = 全部 A 股(约 5400+)。取正整数则按成交额截取, 便于小内存机器。
        "universe_size": 0,
        # 单次扫描允许发起的日线请求上限(保护上游、避免被封禁)
        "scan_kline_budget": 1200,
        "kline_concurrency": 16,
        "quote_concurrency": 8,
        # 内存阈值(MB): 超过软上限压缩缓存, 超过硬上限清空缓存
        "memory_soft_limit_mb": 800,
        "memory_hard_limit_mb": 1400,
        "memory_interval_seconds": 10,
    },
    "refresh": {
        # 非交易时段缓存 TTL
        "idle_ttl_seconds": 300,
        # 数据源模式下, 用户手动切换后的覆盖(见 data/runtime_settings.json)
        "auto_refresh_enabled": True,
    },
    "push": {
        "enabled": False,
        "wecom_webhook": "",
        "wecom_mentioned_mobile": "",
        "max_chars": 3800,
        "dedupe_window_seconds": 1800,
        "retry": 2,
        # 定时推送时刻(北京时间, HH:MM), 逗号分隔
        "schedules": "09:00,11:35,15:05",
        "events": {
            "source_down": True,
            "limit_up_surge": True,
            "strategy_signal": True,
            "memory_warning": True,
        },
        "min_signal_score": 75.0,
        # 参与"新增信号推送"的策略(留空则用内置默认: 潜涨/涨停回调/趋势)
        "signal_strategies": [],
    },
    "news": {
        "enabled": True,
        "retention_days": 15,
        "poll_interval_seconds": 120,
        "important_keywords": [
            "停牌", "复牌", "立案", "问询", "业绩预告", "预增", "预减", "亏损",
            "重组", "并购", "中标", "涨停", "跌停", "回购", "增持", "减持",
            "政策", "降准", "降息", "涨价", "订单", "解禁",
        ],
    },
    "scheduler": {
        "enabled": True,
        # 交易时段主循环间隔(秒) —— 前端轮询间隔由 /api/market/clock 下发
        "trading_interval_seconds": 30,
        "idle_interval_seconds": 300,
        "daily_job_hour": 15,
        "daily_job_minute": 10,
        "weekly_optimize_weekday": 5,   # 周五
    },
    "data_sources": {
        # 每个能力的候选顺序 —— 未列出的能力自动使用全部支持的源
        "mode": "auto",                # auto | real | synthetic
        # 顺序 = 容灾优先级。实测(2026-09): 东财主域名在部分网络被 TLS 阻断但 delay 镜像可达;
        # 腾讯 newfqkline 入口可达而 fqkline 返回 501; 新浪列表会 456 限流。
        #: money_flow 原先只有 eastmoney 一个候选（单点），已补入 sina 兜底 ——
        #: 注意 sina 是**日度**口径（最新为上一交易日），东财是盘中实时，
        #: 所以东财仍排第一，sina 只在前者失败时接管。
        "order": {
            "snapshot": ["eastmoney", "sina", "tencent", "akshare"],
            "quote": ["tencent", "sina", "eastmoney", "akshare", "ashare"],
            "indices": ["eastmoney"],
            "kline": ["tencent", "sina", "ths", "eastmoney", "akshare", "ashare"],
            "minute": ["eastmoney", "tencent", "ths"],
            "rank": ["eastmoney", "sina", "tencent"],
            "sector": ["eastmoney", "tencent", "sina", "ths"],
            "sector_members": ["eastmoney", "tencent", "sina", "ths"],
            "money_flow": ["eastmoney", "sina"],
            "sector_flow": ["eastmoney"],
            "limit_up_pool": ["eastmoney", "ths"],
            "breadth": ["eastmoney", "sina"],
            "code_list": ["eastmoney", "sina", "tencent", "akshare"],
            "attention": ["eastmoney", "xueqiu"],
            "news_flash": ["sina", "akshare", "xueqiu", "weibo"],
            "announcement": ["cninfo"],
            "finance": ["eastmoney"],
        },
        # 手工"锁定"某个能力到指定源(用户在数据源页操作), 空 = 全部自动
        "locked": {},
        # 用户手工新增/覆盖的上游地址模板
        "custom_urls": {},
        # 登录态凭据(由「数据源」页手工输入后加密落盘, 接口只回显掩码)
        "credentials": {},
    },
    "auth": {
        # 默认免登录(单机自用模型)。设为 true 后写操作需要口令。
        "required": False,
        "admin_key": "",
    },
    "log": {
        "level": "INFO",
    },
}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并字典(override 覆盖 base), 返回新字典。

    **空字典视为"显式清空"**: ``{"credentials": {"xueqiu": {}}}`` 应当把该源的凭据整体抹掉,
    而不是因为"没有键可合并"而保留旧值。这一点直接决定"清除凭据"按钮是否真的生效。
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            if len(value) == 0:
                out[key] = {}
            else:
                out[key] = _deep_merge(dict(out[key]), value)  # type: ignore[arg-type]
        else:
            out[key] = copy.deepcopy(value)
    return out


def _coerce(text: str) -> Any:
    """把环境变量字符串转成合适的 Python 类型。"""
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on", "1"):
        return True
    if lowered in ("false", "no", "off", "0"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    try:
        if text.strip() and all(ch.isdigit() or ch in "+-" for ch in text.strip()):
            return int(text)
    except (ValueError, TypeError):
        pass
    try:
        return float(text)
    except (ValueError, TypeError):
        pass
    stripped = text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        import json

        try:
            return json.loads(stripped)
        except ValueError:
            pass
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    return text


def _env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """把 ``SS_A__B__C=value`` 形式的变量还原成嵌套字典。"""
    source = environ if environ is not None else os.environ
    out: dict[str, Any] = {}
    for key, value in source.items():
        if not key.startswith(ENV_PREFIX) or key in ("SS_HOME",):
            continue
        path = key[len(ENV_PREFIX):]
        if not path or path.startswith("_"):
            continue
        parts = [p.lower() for p in path.split(_ENV_NESTED_SEP) if p]
        if not parts:
            continue
        cursor = out
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = _coerce(value)
    return out


def load_toml(path: Path) -> dict[str, Any]:
    if _toml is None or not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = _toml.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:  # TOML 语法错误不应导致启动失败
        import logging

        logging.getLogger(__name__).warning("配置文件解析失败 %s: %s", path.name, exc)
        return {}


# --------------------------------------------------------------------------- #
# 配置容器
# --------------------------------------------------------------------------- #
class Config:
    """点号取值的配置容器(``cfg.get('server.port', 8770)``)。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = data or copy.deepcopy(DEFAULTS)

    # ------------------------------ 读取 ------------------------------
    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, path: str, default: Any = None) -> Any:
        cursor: Any = self._data
        for part in path.split("."):
            if isinstance(cursor, Mapping) and part in cursor:
                cursor = cursor[part]
            else:
                return default
        return cursor

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return dict(value) if isinstance(value, Mapping) else {}

    # ------------------------------ 写入 ------------------------------
    def set(self, path: str, value: Any) -> None:
        parts = path.split(".")
        cursor = self._data
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value

    def update(self, patch: Mapping[str, Any]) -> None:
        self._data = _deep_merge(self._data, patch)

    def replace(self, data: dict[str, Any]) -> None:
        self._data = data

    # ------------------------------ 便捷属性 ------------------------------
    @property
    def universe_size(self) -> int:
        try:
            return max(0, int(self.get("quotas.universe_size", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @property
    def memory_soft_limit_mb(self) -> int:
        return int(self.get("quotas.memory_soft_limit_mb", 800) or 800)

    @property
    def memory_hard_limit_mb(self) -> int:
        return int(self.get("quotas.memory_hard_limit_mb", 1400) or 1400)

    @property
    def source_mode(self) -> str:
        mode = str(self.get("data_sources.mode", "auto") or "auto").strip().lower()
        return mode if mode in ("auto", "real", "synthetic") else "auto"

    @property
    def synthetic_allowed(self) -> bool:
        """只有显式开启 synthetic 模式才允许合成数据兜底。"""
        return self.source_mode == "synthetic"

    def capability_order(self, capability: str) -> list[str]:
        order = self.get(f"data_sources.order.{capability}", [])
        if isinstance(order, str):
            order = [x.strip() for x in order.split(",") if x.strip()]
        return [str(x).strip().lower() for x in (order or []) if str(x).strip()]

    def locked_sources(self) -> dict[str, str]:
        raw = self.get("data_sources.locked", {})
        if not isinstance(raw, Mapping):
            return {}
        return {str(k): str(v) for k, v in raw.items() if v}

    def custom_urls(self) -> dict[str, Any]:
        raw = self.get("data_sources.custom_urls", {})
        return dict(raw) if isinstance(raw, Mapping) else {}

    def credentials(self) -> dict[str, Any]:
        raw = self.get("data_sources.credentials", {})
        return dict(raw) if isinstance(raw, Mapping) else {}


_DEFAULT_APP_TOML = """# StockSpace 应用配置(可选)
# 本文件不存在时使用内置默认值; 环境变量 SS_ 前缀优先级更高,
# 例如 SS_SERVER__PORT=9000 等价于 [server] port = 9000。
#
# [server]
# host = "0.0.0.0"
# port = 8770
#
# [quotas]
# universe_size = 0            # 0 = 全 A 股
# memory_soft_limit_mb = 800
#
# [push]
# enabled = true
# schedules = "09:00,11:35,15:05"
"""


_config_lock = threading.RLock()
_current: Config | None = None


def load_config(*, reload: bool = False) -> Config:
    """加载配置(带缓存)。``reload=True`` 用于网页保存设置后热更新。"""
    global _current
    with _config_lock:
        if _current is not None and not reload:
            return _current

        data = copy.deepcopy(DEFAULTS)
        # 1) config/app.toml（不存在则落一份带注释的模板）
        app_toml = CONFIG_DIR / "app.toml"
        if not app_toml.exists():
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                app_toml.write_text(_DEFAULT_APP_TOML, encoding="utf-8")
            except OSError:
                pass
        data = _deep_merge(data, load_toml(app_toml))

        # 2) config/app.local.toml（本机覆盖, 不入版本库）
        data = _deep_merge(data, load_toml(CONFIG_DIR / "app.local.toml"))

        # 3) data/runtime_settings.json（网页设置页保存的内容）
        runtime = _load_runtime_settings()
        if runtime:
            data = _deep_merge(data, runtime)

        # 4) 环境变量(最高优先级)
        data = _deep_merge(data, _env_overrides())

        _current = Config(data)
        return _current


def _load_runtime_settings() -> dict[str, Any]:
    import json

    path = DATA_DIR / "runtime_settings.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        import logging

        logging.getLogger(__name__).warning("运行期设置文件损坏, 已忽略")
        return {}


def save_runtime_settings(patch: Mapping[str, Any]) -> Config:
    """把用户在网页上的改动持久化, 并热重载配置。"""
    import json

    path = DATA_DIR / "runtime_settings.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _load_runtime_settings()
        merged = _deep_merge(current, patch)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # 原子替换, 避免半截文件
    except OSError as exc:
        import logging

        logging.getLogger(__name__).error("保存运行期设置失败: %s", exc)
    return load_config(reload=True)


def config() -> Config:
    return load_config()


__all__ = [
    "Config",
    "DEFAULTS",
    "config",
    "load_config",
    "load_toml",
    "save_runtime_settings",
]
