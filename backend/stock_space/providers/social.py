"""巨潮资讯网(公告)与登录态数据源(雪球/微博)。

这两个源按需求 8「如果源需要登录，请设置后接入接口，支持手动输入」实现:
  * 未配置凭据时 ``usable`` 为 False, 不参与调度, 也不计入失败统计;
  * 凭据由用户在网页「数据源」页面手工粘贴, 落盘到 ``data/runtime_settings.json``,
    接口只回显掩码;
  * 提供 ``probe()`` 让用户点一下"验证"就知道 Cookie 是否还有效。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

import httpx

from ..core.http import ProviderError
from ..core.util import normalize_code
from ..models import NewsItem
from .base import CAP_ANNOUNCEMENT, CAP_ATTENTION, CAP_NEWS_FLASH, CAP_QUOTE, Provider
from .endpoints import endpoints

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").replace("&nbsp;", " ").strip()


class CninfoProvider(Provider):
    """巨潮资讯网 —— 交易所官方公告检索(无需登录)。

    实测(2026-09): 检索接口是 **POST 表单**, 且部分网络/时段需要先访问一次
    列表页建立会话(Cookie)才能成功, 否则返回 HTTP 500。因此这里做两步:
    先 GET 一次引导页(忽略结果), 再 POST 检索。
    """

    name = "cninfo"
    label = "巨潮资讯（公告）"
    priority = 65
    capabilities = frozenset({CAP_ANNOUNCEMENT})
    note = "交易所官方公告检索, 返回标题与 PDF 原文链接; 无需登录, 首次请求会自动建立会话。"
    homepage = "http://www.cninfo.com.cn/"

    _client: httpx.AsyncClient | None = None
    _warmed: float = 0.0

    async def _session(self) -> httpx.AsyncClient:
        """带 Cookie 的独立会话(与通用 HttpClient 分开, 避免污染其它源的请求头)。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={
                    "User-Agent": self.http.user_agent,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            self._warmed = 0.0
        # 12 小时重新引导一次会话, 避免 Cookie 过期
        if time.time() - self._warmed > 12 * 3600:
            home = endpoints.first(self.name, "home")
            if home:
                try:
                    await self._client.get(home, headers={"Referer": home})
                    self._warmed = time.time()
                except Exception as exc:  # noqa: BLE001 - 引导失败仍尝试直接检索
                    logger.debug("巨潮会话引导失败: %s", exc)
        return self._client

    async def fetch_announcements(self, limit: int = 50) -> list[NewsItem]:
        base = endpoints.first(self.name, CAP_ANNOUNCEMENT)
        if not base:
            raise ProviderError("未配置公告地址", source=self.name)
        client = await self._session()
        try:
            response = await client.post(
                base,
                data={
                    "pageNum": 1, "pageSize": max(10, min(100, limit)),
                    "column": "szse", "tabName": "fulltext", "plate": "",
                    "stock": "", "searchkey": "", "secid": "",
                    "category": "", "trade": "", "seDate": "",
                    "sortName": "", "sortType": "", "isHLtitle": "true",
                },
                headers={
                    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成 ProviderError 触发换源
            raise ProviderError(f"巨潮检索请求失败: {type(exc).__name__}: {exc}",
                                source=self.name) from exc
        if response.status_code != 200:
            self._warmed = 0.0   # 会话可能已失效, 下次重新引导
            raise ProviderError(f"巨潮返回 HTTP {response.status_code}", source=self.name)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"巨潮返回非 JSON: {exc}", source=self.name) from exc

        rows = payload.get("announcements") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ProviderError("公告列表为空", source=self.name)
        out: list[NewsItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = _strip_tags(str(row.get("announcementTitle") or ""))
            if not title:
                continue
            adjunct = str(row.get("adjunctUrl") or "")
            url = f"http://static.cninfo.com.cn/{adjunct}" if adjunct else ""
            code = str(row.get("secCode") or "").strip()
            out.append(
                NewsItem(
                    id=f"cninfo-{row.get('announcementId') or abs(hash(title)) % (10 ** 12)}",
                    title=title[:200],
                    summary=f"{_strip_tags(str(row.get('secName') or ''))} {title}"[:400],
                    url=url,
                    source=self.name,
                    channel="announcement",
                    published_at=_cninfo_time(row.get("announcementTime")),
                    related_codes=[code] if re.fullmatch(r"\d{6}", code) else [],
                    important=any(k in title for k in ("停牌", "复牌", "立案", "重组", "业绩预告")),
                )
            )
        if not out:
            raise ProviderError("公告解析为空", source=self.name)
        return out


def _cninfo_time(value: Any) -> str:
    """巨潮时间戳单位是毫秒。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 1e11:
        number /= 1000.0
    if number <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(number))


class XueqiuProvider(Provider):
    """雪球 —— 热帖/关注度。**需要登录态 Cookie**(acw_* 参数), 未配置时不参与调度。"""

    name = "xueqiu"
    label = "雪球（需登录 Cookie）"
    priority = 40
    capabilities = frozenset({CAP_ATTENTION, CAP_NEWS_FLASH, CAP_QUOTE})
    requires_login = True
    login_required_for_use = True
    note = "热帖/关注度需要登录态 Cookie。实测受阿里云 WAF 影响, 失败时会如实显示原因。"
    homepage = "https://xueqiu.com/"

    def _headers(self) -> dict[str, str]:
        cookie = self.credential("cookie")
        headers = {"Referer": "https://xueqiu.com/"}
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def probe(self) -> dict[str, Any]:
        started = time.perf_counter()
        if not self.has_credentials():
            return {"ok": False, "latency_ms": 0.0,
                    "message": "未配置 Cookie —— 请在「数据源」页面粘贴雪球 Cookie 后再验证"}
        try:
            await self.fetch_attention()
        except Exception as exc:  # noqa: BLE001
            latency = (time.perf_counter() - started) * 1000.0
            return {"ok": False, "latency_ms": round(latency, 1),
                    "message": f"Cookie 无效或被 WAF 拦截: {exc}"}
        latency = (time.perf_counter() - started) * 1000.0
        return {"ok": True, "latency_ms": round(latency, 1), "message": "Cookie 有效, 抓取成功"}

    async def fetch_attention(self) -> dict[str, int]:
        base = endpoints.first(self.name, CAP_ATTENTION)
        if not base:
            raise ProviderError("未配置雪球地址", source=self.name)
        payload = await self.http.get_json(
            base,
            params={"since_id": -1, "max_id": -1, "size": 30, "ext": "last_share"},
            headers=self._headers(),
            alias=self.alias,
            throttle_key=f"{self.name}:hot",
        )
        items = (payload or {}).get("items") if isinstance(payload, dict) else None
        if not items:
            raise ProviderError("雪球热帖为空(通常是 Cookie 失效或被 WAF 拦截)", source=self.name)
        ranking: dict[str, int] = {}
        rank = 0
        for item in items:
            data = item.get("data") if isinstance(item, dict) else None
            symbol = str((data or {}).get("symbol") or "")
            code = symbol.split("$")[-1][:6] if "$" in symbol else symbol[:6]
            if re.fullmatch(r"\d{6}", code):
                rank += 1
                ranking[code] = rank
        if not ranking:
            raise ProviderError("雪球热帖未解析出股票代码", source=self.name)
        return ranking

    async def fetch_news_flash(self, limit: int = 50) -> list[NewsItem]:
        base = endpoints.first(self.name, CAP_ATTENTION)
        if not base:
            raise ProviderError("未配置雪球地址", source=self.name)
        payload = await self.http.get_json(
            base,
            params={"since_id": -1, "max_id": -1, "size": max(10, min(50, limit)), "ext": "last_share"},
            headers=self._headers(),
            alias=self.alias,
            throttle_key=f"{self.name}:hot",
        )
        items = (payload or {}).get("items") if isinstance(payload, dict) else None
        if not items:
            raise ProviderError("雪球热帖为空", source=self.name)
        out: list[NewsItem] = []
        for item in items:
            data = item.get("data") if isinstance(item, dict) else None
            if not isinstance(data, dict):
                continue
            title = _strip_tags(str(data.get("title") or data.get("text") or ""))[:120]
            if not title:
                continue
            symbol = str(data.get("symbol") or "")
            code = symbol.split("$")[-1][:6] if "$" in symbol else ""
            out.append(
                NewsItem(
                    id=f"xueqiu-{data.get('id') or abs(hash(title)) % (10 ** 12)}",
                    title=title,
                    summary=_strip_tags(str(data.get("description") or data.get("text") or ""))[:400],
                    url=f"https://xueqiu.com{data.get('target') or ''}",
                    source=self.name,
                    channel="rumor",
                    published_at=str(data.get("created_at") or ""),
                    related_codes=[code] if re.fullmatch(r"\d{6}", code) else [],
                )
            )
        if not out:
            raise ProviderError("雪球热帖解析为空", source=self.name)
        return out


class WeiboProvider(Provider):
    """微博热搜 —— **需要 SUB/SUBP Cookie**。"""

    name = "weibo"
    label = "微博热搜（需登录 Cookie）"
    priority = 35
    capabilities = frozenset({CAP_NEWS_FLASH})
    requires_login = True
    login_required_for_use = True
    note = "需提供新鲜的 SUB/SUBP Cookie; 仅取财经相关热搜。"
    homepage = "https://m.weibo.cn/"

    def _headers(self) -> dict[str, str]:
        cookie = self.credential("cookie")
        headers = {"Referer": "https://weibo.com/"}
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def probe(self) -> dict[str, Any]:
        started = time.perf_counter()
        if not self.has_credentials():
            return {"ok": False, "latency_ms": 0.0,
                    "message": "未配置 Cookie —— 请在「数据源」页面粘贴微博 Cookie 后再验证"}
        try:
            await self.fetch_news_flash(limit=10)
        except Exception as exc:  # noqa: BLE001
            latency = (time.perf_counter() - started) * 1000.0
            return {"ok": False, "latency_ms": round(latency, 1),
                    "message": f"Cookie 无效或未登录: {exc}"}
        latency = (time.perf_counter() - started) * 1000.0
        return {"ok": True, "latency_ms": round(latency, 1), "message": "Cookie 有效"}

    async def fetch_news_flash(self, limit: int = 50) -> list[NewsItem]:
        base = endpoints.first(self.name, CAP_NEWS_FLASH) or endpoints.first(self.name, "hot")
        if not base:
            raise ProviderError("未配置微博地址", source=self.name)
        payload = await self.http.get_json(
            base,
            headers=self._headers(),
            alias=self.alias,
            throttle_key=f"{self.name}:hot",
        )
        rows = ((payload or {}).get("data") or {}).get("realtime") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ProviderError("微博热搜为空(通常是 Cookie 失效)", source=self.name)
        out: list[NewsItem] = []
        for row in rows[: max(10, min(50, limit))]:
            word = str((row or {}).get("word") or "").strip()
            if not word:
                continue
            out.append(
                NewsItem(
                    id=f"weibo-{hashlib.md5(word.encode('utf-8')).hexdigest()[:12]}",
                    title=word,
                    summary=f"微博热搜(未经证实): {word}",
                    url="https://s.weibo.com/weibo?q=" + word,
                    source=self.name,
                    channel="rumor",
                    published_at="",
                )
            )
        if not out:
            raise ProviderError("微博热搜解析为空", source=self.name)
        return out


__all__ = ["CninfoProvider", "XueqiuProvider", "WeiboProvider"]
