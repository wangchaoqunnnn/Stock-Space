"""pytest 公共夹具。

关键设计: **测试全程不依赖外网**。
  * 通过环境变量强制 ``synthetic`` 模式, 数据源层只使用内置确定性行情剧本;
  * 通过 ``ASGITransport`` 直接调用 FastAPI 应用, 不需要启动真实服务器;
  * 数据库与数据目录指向临时目录, 每个测试会话独立, 不会污染开发数据。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# 环境准备必须在导入 stock_space 之前完成
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_TMP = Path(tempfile.mkdtemp(prefix="stock-space-test-"))
os.environ["SS_DATA_DIR"] = str(_TMP / "data")
os.environ["SS_LOG_DIR"] = str(_TMP / "logs")
os.environ["SS_CACHE_DIR"] = str(_TMP / "data" / "cache")
os.environ["SS_DATA_SOURCES__MODE"] = "synthetic"
os.environ["SS_SCHEDULER__ENABLED"] = "false"
os.environ["SS_LOG__LEVEL"] = "WARNING"
os.environ["SS_QUOTAS__SCAN_KLINE_BUDGET"] = "20000"
os.environ["SS_QUOTAS__UNIVERSE_SIZE"] = "0"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import httpx  # noqa: E402

from stock_space.config import load_config  # noqa: E402
from stock_space.core.http import http_client  # noqa: E402
from stock_space.core.memory import memory_guard  # noqa: E402
from stock_space.paths import ensure_dirs  # noqa: E402
from stock_space.providers.registry import registry  # noqa: E402
from stock_space.store.db import db  # noqa: E402
from stock_space.store.kline_store import kline_store  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():  # noqa: ANN201 - pytest-asyncio 兼容
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def app():
    """构造应用(完成数据库/数据源初始化)。"""
    ensure_dirs()
    load_config(reload=True)
    db.init()
    registry.build(force=True)
    from stock_space.main import create_app

    return create_app()


@pytest.fixture(scope="session")
async def client(app):
    """ASGI 直连客户端 —— 不经过网络, 因此不需要端口。"""
    ensure_dirs()
    load_config(reload=True)
    db.init()
    registry.build(force=True)
    await http_client.start()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    await http_client.close()


@pytest.fixture(scope="session")
async def warm_market(client):
    """预热: 拉一次快照并写入日线缓存, 让后续用例快且稳定。"""
    quotes = await registry.snapshot(force=True)
    for quote in sorted(quotes, key=lambda q: -q.amount)[:40]:
        try:
            kline = await registry.kline(quote.code, 260)
            if kline:
                kline_store.put(kline, source=kline.source)
        except Exception:  # noqa: BLE001 - 预热失败不应让测试报错
            pass
    return quotes


def unwrap(response: httpx.Response) -> object:
    """解包统一响应信封并断言业务成功。"""
    assert response.status_code == 200, f"HTTP {response.status_code}: {response.text[:300]}"
    payload = response.json()
    assert isinstance(payload, dict), f"非信封响应: {response.text[:200]}"
    assert payload.get("code") == 0, f"业务失败: {payload.get('message')}"
    return payload.get("data")


@pytest.fixture(scope="session")
def cleanup_tmp():
    yield
