"""统一 HTTP 客户端: 重试 / 编码自适应 / 限速 / 熔断 / 健康度。

免费行情源对请求频率非常敏感(实测新浪会返回 HTTP 456 封禁约 10 分钟),
所以这里把"礼貌"做成默认行为:

  * **每源最小间隔**(``min_interval``): 同一数据源两次请求之间强制排队;
  * **指数退避重试**: 只对网络异常与 5xx/429 重试, 4xx 立即失败;
  * **熔断器**: 连续失败达到阈值后冷却一段时间, 冷却期内直接跳过, 不浪费超时等待;
  * **健康度评分**: ``成功率 * 0.7 + 延迟得分 * 0.3``, 供注册中心动态排序数据源;
  * **切换事件**: 注册中心记录每次"主源 → 备源"的真实切换, 供前端可观测面板展示。

编码处理: 新浪/腾讯的部分接口返回 GBK/GB18030, 这里先按 Content-Type 判断,
再用严格 UTF-8 试探, 最后回退 GB18030, 避免出现乱码股名。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import httpx

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """数据源返回了不可用结果(包括"不报错但数据不全"的情况)。"""

    def __init__(self, message: str, *, source: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.source = source
        self.detail = detail


class ProviderBlocked(ProviderError):
    """被上游限流/封禁(429/456/503 反爬页)。"""


class NetworkError(ProviderError):
    """网络层异常(超时、连接失败)。"""


# --------------------------------------------------------------------------- #
# 健康度
# --------------------------------------------------------------------------- #
@dataclass
class SourceMetric:
    """单个"源×能力"入口的运行指标。"""

    alias: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    last_latency_ms: float = 0.0
    last_ok_ts: float = 0.0
    last_error: str = ""
    last_error_ts: float = 0.0
    consecutive_failures: int = 0
    breaker_open_until: float = 0.0
    manual_disabled: bool = False
    blocked_count: int = 0
    _recent: deque[bool] = field(default_factory=lambda: deque(maxlen=50))

    @property
    def success_rate(self) -> float:
        return round(self.successes / self.attempts, 4) if self.attempts else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return round(self.total_latency_ms / self.successes, 1) if self.successes else 0.0

    def is_cooling(self) -> bool:
        return self.breaker_open_until > time.time()

    def cooling_remaining(self) -> float:
        return max(0.0, self.breaker_open_until - time.time())

    def health_score(self) -> float:
        """0~100。无样本时给 60(中性), 避免新源永远排在末尾。"""
        if self.attempts == 0:
            base = 60.0
        else:
            base = 100.0 * self.success_rate
        # 近期滑动窗口更能反映"当前"状态
        if self._recent:
            recent_rate = sum(1 for ok in self._recent if ok) / len(self._recent)
            base = base * 0.5 + 100.0 * recent_rate * 0.5
        if self.avg_latency_ms:
            # 300ms 内满分, 3s 以上 0 分
            latency_score = max(0.0, min(1.0, (3000.0 - self.avg_latency_ms) / 2700.0)) * 100.0
            base = base * 0.7 + latency_score * 0.3
        if self.is_cooling():
            base *= 0.2
        if self.manual_disabled:
            base = 0.0
        return round(base, 2)

    def record(self, ok: bool, latency_ms: float = 0.0, error: str = "") -> None:
        self.attempts += 1
        self._recent.append(ok)
        if ok:
            self.successes += 1
            self.total_latency_ms += latency_ms
            self.last_latency_ms = latency_ms
            self.last_ok_ts = time.time()
            self.consecutive_failures = 0
        else:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = error[:300]
            self.last_error_ts = time.time()

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.success_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "last_latency_ms": round(self.last_latency_ms, 1),
            "health_score": self.health_score(),
            "consecutive_failures": self.consecutive_failures,
            "cooling": self.is_cooling(),
            "cooling_remaining_seconds": round(self.cooling_remaining(), 1),
            "manual_disabled": self.manual_disabled,
            "blocked_count": self.blocked_count,
            "last_error": self.last_error,
            "last_ok_ago_seconds": round(time.time() - self.last_ok_ts, 1) if self.last_ok_ts else None,
        }


@dataclass
class SwitchEvent:
    ts: float
    category: str
    from_alias: str
    to_alias: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 1),
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "category": self.category,
            "from": self.from_alias,
            "to": self.to_alias,
            "reason": self.reason[:200],
        }


class MetricsBoard:
    """所有数据源入口指标的登记处。"""

    def __init__(self) -> None:
        self._metrics: dict[str, SourceMetric] = {}
        self._switches: deque[SwitchEvent] = deque(maxlen=200)
        self._lock = asyncio.Lock()
        self._blocked_hosts: dict[str, float] = {}

    def get(self, alias: str) -> SourceMetric:
        metric = self._metrics.get(alias)
        if metric is None:
            metric = SourceMetric(alias=alias)
            self._metrics[alias] = metric
        return metric

    def all(self) -> list[SourceMetric]:
        return list(self._metrics.values())

    def record_switch(self, category: str, from_alias: str, to_alias: str, reason: str) -> None:
        self._switches.append(
            SwitchEvent(time.time(), category, from_alias, to_alias, reason or "")
        )

    def recent_switches(self, limit: int = 20) -> list[dict[str, Any]]:
        return [e.as_dict() for e in list(self._switches)[-limit:]][::-1]

    def reset(self) -> int:
        """复位所有熔断器与失败计数(手动"重新探测"时调用)。"""
        count = 0
        for metric in self._metrics.values():
            if metric.is_cooling():
                count += 1
            metric.breaker_open_until = 0.0
            metric.consecutive_failures = 0
        return count

    # ------------------------- 上游整体封禁标记 -------------------------
    def mark_blocked(self, host: str, seconds: float) -> None:
        self._blocked_hosts[host] = time.time() + seconds

    def blocked_remaining(self, host: str) -> float:
        until = self._blocked_hosts.get(host, 0.0)
        return max(0.0, until - time.time())

    def blocked_hosts(self) -> dict[str, float]:
        now = time.time()
        return {h: round(v - now, 1) for h, v in self._blocked_hosts.items() if v > now}


metrics_board = MetricsBoard()


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
def decode_body(raw: bytes, content_type: str = "") -> str:
    """按 Content-Type 与字节特征自适应解码(处理 GBK 股名乱码)。"""
    lowered = (content_type or "").lower()
    for encoding, markers in (
        ("utf-8", ("charset=utf-8", "charset=utf8")),
        ("gb18030", ("charset=gbk", "charset=gb2312", "charset=gb18030")),
    ):
        if any(m in lowered for m in markers):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                break
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_json_payload(text: str, *, source: str = "") -> Any:
    """把上游文本解析成 JSON, 兼容三种常见包装。

    实测上游会返回下列任意一种(同一个接口在不同入口甚至不同时间都可能变):

    1. 纯 JSON:               ``{"data": ...}``
    2. 函数式 JSONP:          ``cb({...})``
    3. **变量式 JSONP**:      ``kline_day={...}``  ← 腾讯 ``_var`` 参数就是这种

    第 3 种曾经让"腾讯 K 线入口看起来全部失败"(实际 HTTP 200), 因为只按第 2 种解析会失败。
    这里统一按"定位第一个 ``{`` 或 ``[`` 到最后一个对应闭合符"来提取, 对三种都成立。
    """
    import json

    raw = (text or "").strip()
    if not raw:
        raise ProviderError("上游返回空内容", source=source)
    # 去掉可能存在的 BOM
    if raw.startswith("\ufeff"):
        raw = raw[1:].lstrip()

    try:
        return json.loads(raw)
    except ValueError:
        pass

    # 1) 函数式 JSONP: cb({...}) 或 cb([...]);
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        if start < 0:
            continue
        end = raw.rfind(closer)
        if end > start:
            try:
                return json.loads(raw[start:end + 1])
            except ValueError:
                continue

    # 2) 变量式 JSONP: kline_day={...};  —— 上面的分支已能覆盖, 这里再兜一次分号结尾
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        if start < 0:
            continue
        end = raw.rfind(closer)
        if end > start:
            candidate = raw[start:end + 1].rstrip(";")
            try:
                return json.loads(candidate)
            except ValueError:
                continue

    raise ProviderError(
        "上游返回非 JSON 内容", source=source, detail=raw[:200]
    )


class HttpClient:
    """异步 HTTP 客户端(带重试 / 限速 / 熔断 / 指标)。"""
    def __init__(
        self,
        *,
        timeout: float = 12.0,
        retries: int = 2,
        min_interval: float = 0.12,
        breaker_threshold: int = 5,
        breaker_seconds: float = 60.0,
        user_agent: str = "",
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.min_interval = max(0.0, min_interval)
        self.breaker_threshold = max(1, breaker_threshold)
        self.breaker_seconds = max(1.0, breaker_seconds)
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        self._client: httpx.AsyncClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_call: dict[str, float] = {}
        self._lock_guard = asyncio.Lock()
        self._closed = False

    # ------------------------------ 生命周期 ------------------------------
    async def start(self) -> None:
        if self._client is None or self._closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(6.0, self.timeout)),
                follow_redirects=True,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Connection": "keep-alive",
                },
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=24),
                trust_env=True,
            )
            self._closed = False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._closed = True

    async def _ensure(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.start()
        assert self._client is not None
        return self._client

    # ------------------------------ 限速 ------------------------------
    async def _throttle(self, key: str) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
        async with lock:
            last = self._last_call.get(key, 0.0)
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[key] = time.monotonic()

    # ------------------------------ 请求 ------------------------------
    async def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        alias: str = "",
        throttle_key: str | None = None,
        timeout: float | None = None,
        encoding: str | None = None,
    ) -> str:
        response = await self._request(
            "GET", url, params=params, headers=headers, alias=alias,
            throttle_key=throttle_key, timeout=timeout,
        )
        if encoding:
            return response.content.decode(encoding, errors="replace")
        return decode_body(response.content, response.headers.get("content-type", ""))

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        alias: str = "",
        throttle_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        text = await self.get_text(
            url, params=params, headers=headers, alias=alias,
            throttle_key=throttle_key, timeout=timeout,
        )
        return parse_json_payload(text, source=alias)

    async def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        alias: str = "",
        throttle_key: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        response = await self._request(
            "GET", url, params=params, headers=headers, alias=alias,
            throttle_key=throttle_key, timeout=timeout,
        )
        return response.content

    async def post_json(
        self,
        url: str,
        *,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        alias: str = "",
        throttle_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self._request(
            "POST", url, json_body=json_body, data=data, headers=headers,
            alias=alias, throttle_key=throttle_key, timeout=timeout,
        )
        text = decode_body(response.content, response.headers.get("content-type", ""))
        return parse_json_payload(text, source=alias)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        alias: str = "",
        throttle_key: str | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        client = await self._ensure()
        metric = metrics_board.get(alias) if alias else None
        key = throttle_key or url.split("?")[0]
        host = httpx.URL(url).host or key

        blocked_for = metrics_board.blocked_remaining(host)
        if blocked_for > 0:
            if metric:
                metric.blocked_count += 1
            raise ProviderBlocked(
                f"上游 {host} 处于限流冷却中(剩余 {blocked_for:.0f}s)",
                source=alias,
            )

        attempts = self.retries + 1
        last_error: Exception | None = None
        started = time.perf_counter()

        for attempt in range(attempts):
            await self._throttle(key)
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    headers=dict(headers) if headers else None,
                    timeout=timeout or self.timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError, httpx.NetworkError) as exc:
                last_error = NetworkError(f"网络异常: {type(exc).__name__}: {exc}", source=alias)
            else:
                status = response.status_code
                if status == 200:
                    latency = (time.perf_counter() - started) * 1000.0
                    if metric:
                        metric.record(True, latency)
                    return response
                if status in (456, 429, 403, 418):
                    # 反爬封禁: 标记该 host 冷却, 不再重试避免加重封禁
                    cooldown = 600.0 if status == 456 else 120.0
                    metrics_board.mark_blocked(host, cooldown)
                    if metric:
                        metric.blocked_count += 1
                    err = ProviderBlocked(
                        f"上游限流/反爬 HTTP {status}(已冷却 {cooldown:.0f}s)",
                        source=alias,
                        detail=response.text[:160],
                    )
                    if metric:
                        metric.record(False, error=str(err))
                    raise err
                if status in (500, 502, 503, 504) or status == 408:
                    last_error = ProviderError(
                        f"上游服务异常 HTTP {status}", source=alias, detail=response.text[:160]
                    )
                else:
                    err = ProviderError(
                        f"上游返回 HTTP {status}", source=alias, detail=response.text[:160]
                    )
                    if metric:
                        metric.record(False, error=str(err))
                    raise err

            if attempt < attempts - 1:
                await asyncio.sleep(min(2.0, 0.35 * (2 ** attempt)))

        latency = (time.perf_counter() - started) * 1000.0
        final = last_error or NetworkError("未知网络错误", source=alias)
        if metric:
            metric.record(False, latency, error=str(final))
            if metric.consecutive_failures >= self.breaker_threshold:
                metric.breaker_open_until = time.time() + self.breaker_seconds
                logger.warning(
                    "数据源 %s 连续失败 %d 次, 熔断 %.0fs",
                    alias, metric.consecutive_failures, self.breaker_seconds,
                )
        raise final

    # ------------------------------ 并发工具 ------------------------------
    async def gather_limited(
        self,
        coros: Iterable[Any],
        *,
        limit: int = 12,
        return_exceptions: bool = True,
    ) -> list[Any]:
        """受并发上限约束的 ``asyncio.gather``(避免一次性打满上游)。"""
        semaphore = asyncio.Semaphore(max(1, limit))

        async def runner(coro: Any) -> Any:
            async with semaphore:
                return await coro

        return await asyncio.gather(
            *(runner(c) for c in coros), return_exceptions=return_exceptions
        )


#: 进程级共享客户端(由 main.py 启动/关闭)
http_client = HttpClient()


__all__ = [
    "HttpClient",
    "http_client",
    "ProviderError",
    "ProviderBlocked",
    "NetworkError",
    "MetricsBoard",
    "SourceMetric",
    "metrics_board",
    "decode_body",
    "parse_json_payload",
]
