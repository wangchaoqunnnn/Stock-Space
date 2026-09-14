"""上游地址目录 —— **全项目唯一允许出现第三方域名的地方**。

地址来源(按优先级从低到高):
  1. ``config/sources.toml``           内置候选地址(随仓库分发)
  2. ``config/sources.local.toml``     本机覆盖(不入版本库)
  3. ``data/runtime_settings.json`` 的 ``data_sources.custom_urls``
                                       用户在网页「数据源」页面手工输入
  4. 环境变量 ``SS_SOURCES__<源>__URLS__<能力>``(逗号分隔)

因此"不得引用静态地址/固定地址"落到实处: 所有上游地址都可被替换,
且每个能力都是**候选列表**, 天然具备换源能力。

占位符约定(在 URL 模板里使用 ``{name}``):
  {code} {market} {secid} {symbol} {keyword} {sector_code} {sector_name} {kind} {limit} {page} {days}
"""

from __future__ import annotations

import logging
from typing import Any, Mapping
from urllib.parse import quote as urlquote

from ..config import config, load_toml
from ..core.http import http_client
from ..paths import SOURCES_DEFAULT_PATH, SOURCES_OVERRIDE_PATH

logger = logging.getLogger(__name__)


class EndpointDirectory:
    """树形结构: ``urls[provider][capability] -> list[str]``。"""

    def __init__(self) -> None:
        self._tree: dict[str, dict[str, list[str]]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._custom: dict[str, dict[str, list[str]]] = {}
        self.reload()

    # ------------------------------ 加载 ------------------------------
    def reload(self) -> None:
        tree: dict[str, dict[str, list[str]]] = {}
        meta: dict[str, dict[str, Any]] = {}

        for path in (SOURCES_DEFAULT_PATH, SOURCES_OVERRIDE_PATH):
            data = load_toml(path)
            providers = data.get("providers") if isinstance(data, dict) else None
            if not isinstance(providers, Mapping):
                continue
            for name, section in providers.items():
                if not isinstance(section, Mapping):
                    continue
                key = str(name).strip().lower()
                urls = section.get("urls")
                bucket = tree.setdefault(key, {})
                if isinstance(urls, Mapping):
                    for capability, value in urls.items():
                        entries = _as_list(value)
                        if entries:
                            # 后面的文件覆盖前面的同名能力
                            bucket[str(capability).strip().lower()] = entries
                info = meta.setdefault(key, {})
                for field in ("label", "priority", "enabled", "requires_login", "note", "homepage"):
                    if field in section:
                        info[field] = section[field]

        # 用户手工输入的地址
        custom = _custom_urls_from_settings()
        for provider, caps in custom.items():
            bucket = tree.setdefault(provider, {})
            for capability, value in caps.items():
                entries = _as_list(value)
                if entries:
                    bucket[capability] = entries

        # 环境变量覆盖
        for provider, caps in _custom_urls_from_env().items():
            bucket = tree.setdefault(provider, {})
            for capability, value in caps.items():
                entries = _as_list(value)
                if entries:
                    bucket[capability] = entries

        self._tree = tree
        self._meta = meta
        self._custom = {
            provider: {cap: list(urls) for cap, urls in caps.items()}
            for provider, caps in custom.items()
        }

    # ------------------------------ 查询 ------------------------------
    def urls(self, provider: str, capability: str) -> list[str]:
        return list(self._tree.get(provider, {}).get(capability, []))

    def first(self, provider: str, capability: str) -> str | None:
        urls = self.urls(provider, capability)
        return urls[0] if urls else None

    def meta(self, provider: str) -> dict[str, Any]:
        return dict(self._meta.get(provider, {}))

    def all_providers(self) -> list[str]:
        return sorted(self._tree.keys())

    def capabilities_of(self, provider: str) -> list[str]:
        return sorted(self._tree.get(provider, {}).keys())

    def is_customized(self, provider: str, capability: str | None = None) -> bool:
        caps = self._custom.get(provider)
        if not caps:
            return False
        return capability is None or capability in caps

    def custom_urls(self) -> dict[str, dict[str, list[str]]]:
        return {
            provider: {cap: list(urls) for cap, urls in caps.items()}
            for provider, caps in self._custom.items()
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "providers": {
                provider: {
                    "capabilities": {
                        cap: {"count": len(urls), "customized": self.is_customized(provider, cap)}
                        for cap, urls in caps.items()
                    },
                    "meta": self.meta(provider),
                }
                for provider, caps in sorted(self._tree.items())
            },
            "customized": list(self._custom.keys()),
        }

    # ------------------------------ 渲染 ------------------------------
    def render(self, provider: str, capability: str, variables: Mapping[str, Any]) -> list[str]:
        """把模板里的占位符替换成实际值。未提供的占位符原样保留(便于排查)。"""
        safe = {key: urlquote(str(value), safe="") if key in _QUOTE_KEYS else str(value)
                for key, value in variables.items()}
        rendered: list[str] = []
        for template in self.urls(provider, capability):
            try:
                rendered.append(template.format(**safe))
            except (KeyError, IndexError, ValueError):
                # 模板里可能出现未提供的键 —— 用空串兜底而不是整体失败
                filled = template
                for key, value in safe.items():
                    filled = filled.replace("{" + key + "}", value)
                rendered.append(filled)
        return rendered


#: 这些占位符需要 URL 编码(可能含中文/特殊字符)
_QUOTE_KEYS = frozenset({"keyword", "sector_name", "sector_code", "name"})


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _custom_urls_from_settings() -> dict[str, dict[str, list[str]]]:
    raw = config().custom_urls()
    out: dict[str, dict[str, list[str]]] = {}
    for provider, caps in raw.items():
        if not isinstance(caps, Mapping):
            continue
        bucket: dict[str, list[str]] = {}
        for capability, value in caps.items():
            entries = _as_list(value)
            if entries:
                bucket[str(capability).strip().lower()] = entries
        if bucket:
            out[str(provider).strip().lower()] = bucket
    return out


def _custom_urls_from_env() -> dict[str, dict[str, list[str]]]:
    """``SS_SOURCES__<provider>__URLS__<capability>=url1,url2``"""
    import os

    out: dict[str, dict[str, list[str]]] = {}
    for key, value in os.environ.items():
        if not key.startswith("SS_SOURCES__"):
            continue
        parts = key[len("SS_SOURCES__"):].split("__")
        if len(parts) < 3 or parts[1].upper() != "URLS":
            continue
        provider = parts[0].strip().lower()
        capability = parts[2].strip().lower()
        entries = _as_list(value)
        if entries:
            out.setdefault(provider, {})[capability] = entries
    return out


#: 全局单例
endpoints = EndpointDirectory()


__all__ = ["EndpointDirectory", "endpoints"]
