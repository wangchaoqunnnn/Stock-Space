"""数据源层。

对外只有两个入口:
  * ``registry``    —— 能力路由与故障转移(上层业务只应该用它);
  * ``endpoints``   —— 上游地址目录(唯一允许出现第三方域名的地方)。

    from ..providers import registry
    quotes = await registry.snapshot()

新增一个数据源的步骤:
  1. 在 ``providers/`` 下新建模块, 继承 ``base.Provider`` 并声明 ``capabilities``;
  2. 在 ``registry.ProviderRegistry.build()`` 的元组里登记该类;
  3. 在 ``config/sources.toml`` 里补 ``[providers.<name>]`` 与 ``[providers.<name>.urls]``;
  4. 在 ``data_sources.order.<能力>`` 里安排优先级(设置页或 config/app.toml)。
"""

from __future__ import annotations

from .base import ALL_CAPABILITIES, CAPABILITY_SPECS, Provider
from .endpoints import EndpointDirectory, endpoints
from .registry import AllProvidersFailed, CallOutcome, ProviderRegistry, registry

__all__ = [
    "Provider",
    "ProviderRegistry",
    "registry",
    "AllProvidersFailed",
    "CallOutcome",
    "EndpointDirectory",
    "endpoints",
    "ALL_CAPABILITIES",
    "CAPABILITY_SPECS",
]
