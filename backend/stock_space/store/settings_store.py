"""用户设置与敏感凭据存储。

安全约定(需求 8/9 的"支持手动输入"与"不回显"):

  * 敏感项(企业微信 Webhook、数据源 Cookie/Token、管理口令)落盘在
    ``data/runtime_settings.json``(权限 600, 已 gitignore), **绝不写入 config/*.toml**;
  * 读取接口一律返回**掩码**(``https://qyapi.weixin.qq.com/...key=abc***xyz``),
    仅当用户显式勾选"显示明文"或提交"验证"时才由后端内部使用真值;
  * 掩码函数对短串也安全(不会把整个串暴露出来)。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from ..config import config, load_config, save_runtime_settings
from ..core.util import now_cn
from ..paths import DATA_DIR, RUNTIME_SETTINGS_PATH

logger = logging.getLogger(__name__)

#: 需要掩码的字段路径(相对 data_sources.credentials 与 push 节)
_SECRET_KEYS = frozenset(
    {
        "wecom_webhook", "admin_key", "api_key", "token", "cookie",
        "secret", "password", "authorization", "access_token", "refresh_token",
    }
)

#: 允许整体写入的"叶子映射"路径前缀(其下所有子键都可用)
LEAF_PREFIXES: tuple[str, ...] = (
    "data_sources.locked",
    "data_sources.custom_urls",
    "data_sources.credentials",
    "app.strategy_params",
    "app.ui_prefs",
    "app.active_sources",
)


def _is_editable(path: str) -> bool:
    if path in EDITABLE_PATHS:
        return True
    # 叶子映射: 既允许整体写入(``data_sources.custom_urls``),
    # 也允许写子键(``data_sources.custom_urls.tencent.kline``)。
    for prefix in LEAF_PREFIXES:
        if path == prefix or path.startswith(prefix + "."):
            return True
    return False


#: 允许用户在网页上修改的配置路径白名单(防止越权写入任意配置)
EDITABLE_PATHS: frozenset[str] = frozenset(
    {
        "server.port", "server.cors_origins", "server.root_path",
        "app.public_base_url",
        "quotas.universe_size", "quotas.scan_kline_budget", "quotas.kline_concurrency",
        "quotas.quote_concurrency", "quotas.memory_soft_limit_mb",
        "quotas.memory_hard_limit_mb", "quotas.memory_interval_seconds",
        "refresh.idle_ttl_seconds", "refresh.auto_refresh_enabled",
        "push.enabled", "push.wecom_webhook", "push.wecom_mentioned_mobile",
        "push.max_chars", "push.dedupe_window_seconds", "push.retry", "push.schedules",
        "push.min_signal_score", "push.events.source_down", "push.events.limit_up_surge",
        "push.events.strategy_signal", "push.events.memory_warning",
        "news.enabled", "news.retention_days", "news.poll_interval_seconds",
        "scheduler.enabled", "scheduler.trading_interval_seconds",
        "scheduler.idle_interval_seconds", "scheduler.daily_job_hour",
        "scheduler.daily_job_minute",
        "data_sources.mode",
        "auth.required", "auth.admin_key",
        "app.strategy_params",
        "app.ui_prefs",
        "app.active_sources",
        "app.dashboard_prefs",
    }
)


def mask_secret(value: str, *, keep_head: int = 8, keep_tail: int = 4) -> str:
    """把敏感串转成掩码。空串返回空串; 短串只保留首尾各 1~2 位。"""
    text = str(value or "")
    if not text:
        return ""
    length = len(text)
    if length <= keep_head + keep_tail:
        if length <= 4:
            return "*" * length
        return f"{text[:2]}{'*' * (length - 3)}{text[-1:]}"
    return f"{text[:keep_head]}{'*' * 8}{text[-keep_tail:]}"


def is_secret_path(path: str) -> bool:
    tail = path.rsplit(".", 1)[-1].lower()
    return tail in _SECRET_KEYS or tail.endswith(("_key", "_token", "_secret", "_cookie", "_webhook"))


class SettingsStore:
    """把「配置 + 敏感凭据 + 策略参数」封装成对前端友好的读写接口。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------ 路径 ------------------------------
    @property
    def runtime_path(self) -> Path:
        return RUNTIME_SETTINGS_PATH

    def ensure_secure(self) -> None:
        """把设置文件权限收紧到 600(仅 POSIX 生效)。"""
        if os.name == "nt":
            return
        try:
            if self.runtime_path.exists():
                os.chmod(self.runtime_path, 0o600)
        except OSError:
            pass

    # ------------------------------ 读 ------------------------------
    def raw(self) -> dict[str, Any]:
        if not self.runtime_path.exists():
            return {}
        try:
            data = json.loads(self.runtime_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            logger.warning("运行期设置损坏, 已回退为空")
            return {}

    def get(self, path: str, default: Any = None) -> Any:
        return config().get(path, default)

    def public_snapshot(self) -> dict[str, Any]:
        """给前端的完整设置视图 —— 所有敏感字段已掩码。"""
        cfg = config()
        raw = self.raw()

        credentials = cfg.credentials()
        safe_credentials: dict[str, Any] = {}
        for name, payload in credentials.items():
            if isinstance(payload, Mapping):
                safe_credentials[name] = {
                    key: (mask_secret(str(val)) if is_secret_path(key) else val)
                    for key, val in payload.items()
                }
            else:
                safe_credentials[name] = mask_secret(str(payload))

        return {
            "server": {
                "host": cfg.get("server.host"),
                "port": cfg.get("server.port"),
                "root_path": cfg.get("server.root_path", ""),
                "cors_origins": cfg.get("server.cors_origins", []),
                "docs_enabled": bool(cfg.get("server.docs_enabled", True)),
            },
            "app": {
                "title": cfg.get("app.title"),
                "public_base_url": cfg.get("app.public_base_url", ""),
                "disclaimer": cfg.get("app.disclaimer"),
                "timezone": cfg.get("app.timezone"),
            },
            "quotas": cfg.section("quotas"),
            "refresh": cfg.section("refresh"),
            "push": {
                **cfg.section("push"),
                "wecom_webhook": mask_secret(str(cfg.get("push.wecom_webhook", ""))),
                "wecom_webhook_configured": bool(cfg.get("push.wecom_webhook", "")),
                "wecom_mentioned_mobile": mask_secret(
                    str(cfg.get("push.wecom_mentioned_mobile", "")), keep_head=3, keep_tail=2
                ),
            },
            "news": cfg.section("news"),
            "scheduler": cfg.section("scheduler"),
            "data_sources": {
                "mode": cfg.source_mode,
                "locked": cfg.locked_sources(),
                "custom_urls": cfg.custom_urls(),
                "credentials": safe_credentials,
                "credential_providers": sorted(credentials.keys()),
            },
            "auth": {
                "required": bool(cfg.get("auth.required", False)),
                "admin_key": mask_secret(str(cfg.get("auth.admin_key", ""))),
                "admin_key_configured": bool(cfg.get("auth.admin_key", "")),
            },
            "editable_paths": sorted(EDITABLE_PATHS),
            "runtime_file": self.runtime_path.name,
            "raw_keys": sorted(raw.keys()),
        }

    # ------------------------------ 写 ------------------------------
    def update(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        """按白名单写入设置。返回被拒绝的路径列表。"""
        flat = _flatten(patch)
        accepted: dict[str, Any] = {}
        rejected: list[str] = []
        for path, value in flat.items():
            if _is_editable(path):
                accepted[path] = value
            else:
                rejected.append(path)

        if accepted:
            nested: dict[str, Any] = {}
            for path, value in accepted.items():
                _assign(nested, path, value)
            with self._lock:
                save_runtime_settings(nested)
                self.ensure_secure()
        return {"accepted": sorted(accepted.keys()), "rejected": sorted(rejected)}
    def set_secret(self, path: str, value: str) -> None:
        """写入敏感项(不做掩码判断, 直接落盘)。"""
        if path not in EDITABLE_PATHS and not path.startswith("data_sources.credentials."):
            raise ValueError(f"不允许写入的配置项: {path}")
        nested: dict[str, Any] = {}
        _assign(nested, path, value)
        with self._lock:
            save_runtime_settings(nested)
            self.ensure_secure()

    def clear_secret(self, path: str) -> None:
        self.set_secret(path, "")

    # ------------------------------ 策略参数 ------------------------------
    def strategy_params(self) -> dict[str, Any]:
        value = config().get("app.strategy_params", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def save_strategy_params(self, strategy: str, params: Mapping[str, Any]) -> None:
        nested = {"app": {"strategy_params": {strategy: dict(params)}}}
        with self._lock:
            save_runtime_settings(nested)

    def all_strategy_params(self) -> dict[str, Any]:
        return copy.deepcopy(self.strategy_params())

    # ------------------------------ UI 偏好 ------------------------------
    def ui_prefs(self) -> dict[str, Any]:
        value = config().get("app.ui_prefs", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def save_ui_prefs(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        nested = {"app": {"ui_prefs": dict(patch)}}
        save_runtime_settings(nested)
        return self.ui_prefs()

    # ------------------------------ 生效源 ------------------------------
    def active_sources(self) -> dict[str, Any]:
        value = config().get("app.active_sources", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def save_active_sources(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        save_runtime_settings({"app": {"active_sources": dict(patch)}})
        return self.active_sources()

    # ------------------------------ 导出/导入 ------------------------------
    def export(self, *, include_secrets: bool = False) -> dict[str, Any]:
        cfg = config()
        raw = self.raw()
        if not include_secrets:
            return _mask_tree(raw)
        return {"runtime": raw, "effective": cfg.data}

    def import_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = payload.get("runtime") if isinstance(payload.get("runtime"), Mapping) else payload
        result = self.update(dict(data))
        return result

    def backup_to(self, target: Path) -> Path:
        target.write_text(
            json.dumps(self.export(include_secrets=True), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    # ------------------------------ 元信息 ------------------------------
    def info(self) -> dict[str, Any]:
        return {
            "runtime_file_present": self.runtime_path.exists(),
            "data_dir_name": DATA_DIR.name,
            "updated_hint": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "editable_count": len(EDITABLE_PATHS),
        }


def _flatten(payload: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套设置拍平成 ``a.b.c`` 路径表。

    ``_LEAF_MAPPINGS`` 下的映射(凭据/自定义地址/锁定表/策略参数)**整体作为值保留**,
    不继续展开 —— 因为它们内部的键名由用户输入决定(数据源名、能力名),
    展开后再写回会与"允许写子键"的白名单语义打架。
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and not _is_leaf_mapping(path):
            out.update(_flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


#: 这些映射整体作为值写入, 不展开(用户自定义的键名不可控)
_LEAF_MAPPINGS = ("data_sources.credentials", "data_sources.custom_urls", "data_sources.locked",
                  "app.strategy_params", "app.ui_prefs", "app.active_sources")


def _is_leaf_mapping(path: str) -> bool:
    return any(path == leaf or path.startswith(f"{leaf}.") for leaf in _LEAF_MAPPINGS)


def _assign(target: dict[str, Any], path: str, value: Any) -> None:
    """把 ``a.b.c = value`` 写进嵌套字典。

    对 ``_LEAF_MAPPINGS`` 下的映射(凭据/自定义地址/锁定表/策略参数)做**键级合并**,
    而不是整体替换 —— 否则"保存一个源的自定义地址"会把其它源的覆盖一起抹掉。
    """
    parts = path.split(".")
    for leaf in _LEAF_MAPPINGS:
        leaf_parts = leaf.split(".")
        if parts[: len(leaf_parts)] != leaf_parts:
            continue
        cursor = target
        for part in leaf_parts:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        remaining = parts[len(leaf_parts):]
        if not remaining:
            # 整体写入某个叶子映射(如 {"data_sources": {"custom_urls": {...}}})
            cursor.update(dict(value) if isinstance(value, Mapping) else {})
        else:
            key = remaining[0]
            if isinstance(value, Mapping) and not isinstance(value, list):
                bucket = cursor.get(key)
                if not isinstance(bucket, dict):
                    bucket = {}
                # 空字典 = 显式清空该项
                cursor[key] = {} if len(value) == 0 else {**bucket, **dict(value)}
            else:
                cursor[key] = value
        return

    cursor = target
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _mask_tree(node: Any, key: str = "") -> Any:
    if isinstance(node, Mapping):
        return {k: _mask_tree(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_tree(v, key) for v in node]
    if is_secret_path(key) and isinstance(node, str):
        return mask_secret(node)
    return node


settings_store = SettingsStore()


__all__ = ["SettingsStore", "settings_store", "mask_secret", "is_secret_path", "EDITABLE_PATHS"]
