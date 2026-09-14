"""Ashare 数据源(库型, 可选安装)。

`Ashare <https://github.com/mpquant/Ashare>`_ 是一个极轻量的单文件行情库,
底层走腾讯通道, 提供 ``get_price`` / ``get_bars`` 两个函数。

工程约束与 AKShare 相同: 未安装时 ``installed()`` 返回 False 且不参与调度;
同步调用全部通过 ``asyncio.to_thread`` 投递, 不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Iterable

from ..core.http import ProviderError
from ..core.util import board_of, detect_market, limit_pct, normalize_code, tencent_symbol
from ..models import Bar, KLine, Quote
from .base import CAP_KLINE, CAP_QUOTE, Provider
from .endpoints import endpoints

logger = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "-", "--"):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    try:
        import math

        if math.isnan(number):
            return default
    except (TypeError, ValueError):
        pass
    return number


def _to_frame(records: list[dict[str, Any]]) -> Any:
    """把记录列表转成 DataFrame(Ashare 内部用 pandas)。"""
    import pandas as pd

    return pd.DataFrame(records)


class AshareProvider(Provider):
    name = "ashare"
    label = "Ashare"
    priority = 45
    capabilities = frozenset({CAP_KLINE, CAP_QUOTE})
    note = "轻量单文件行情库(可选安装), 底层走腾讯通道。未安装时不参与调度。"
    homepage = "https://github.com/mpquant/Ashare"

    def installed(self) -> bool:
        try:
            import Ashare  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    async def _call(self, func_name: str, /, *args: Any, **kwargs: Any) -> Any:
        if not self.installed():
            raise ProviderError("Ashare 未安装", source=self.name)

        def runner() -> Any:
            import Ashare  # 延迟导入

            func = getattr(Ashare, func_name, None)
            if func is None:
                raise ProviderError(f"Ashare 缺少接口 {func_name}", source=self.name)
            return func(*args, **kwargs)

        try:
            return await asyncio.to_thread(runner)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Ashare 调用失败: {type(exc).__name__}: {exc}", source=self.name) from exc

    # ------------------------------------------------------------------ #
    async def fetch_kline(self, code: str, days: int = 260, period: str = "day") -> KLine:
        code = normalize_code(code)
        # Ashare 的 get_price 接受腾讯代码(sh600519)与周期字符串
        freq = {"day": "1d", "week": "1w", "month": "1M"}.get(period, "1d")
        frame = await self._call(
            "get_price", tencent_symbol(code), end_date=time.strftime("%Y-%m-%d"),
            count=max(30, min(2000, days)), frequency=freq,
        )
        if frame is None or len(frame) == 0:
            raise ProviderError("Ashare K线为空", source=self.name)

        columns = {str(c).lower().strip(): c for c in getattr(frame, "columns", [])}
        bars: list[Bar] = []
        prev_close = 0.0
        for _, row in frame.iterrows():
            def col(*names: str, default: Any = None) -> Any:
                for name in names:
                    key = columns.get(name.lower())
                    if key is not None:
                        return row[key]
                return default

            raw_date = col("date", "day", "index", default="")
            date_text = str(raw_date)[:10]
            if not re.match(r"\d{4}-\d{2}-\d{2}", date_text):
                continue
            close = _f(col("close"))
            change_pct = ((close - prev_close) / prev_close * 100.0) if prev_close else 0.0
            prev_close = close or prev_close
            bars.append(
                Bar(
                    date=date_text,
                    open=_f(col("open")), high=_f(col("high")),
                    low=_f(col("low")), close=close,
                    # Ashare 的 volume 单位为"手"(腾讯口径), 统一换算为"股"
                    volume=_f(col("volume")) * 100.0,
                    amount=_f(col("amount")),
                    change_pct=round(change_pct, 3),
                )
            )
        if len(bars) < 5:
            raise ProviderError(f"Ashare K线不足({len(bars)} 根)", source=self.name)
        return KLine(code=code, period=period, bars=bars[-days:], source=self.name,
                     fetched_at=time.time())

    async def fetch_quotes(self, codes: Iterable[str]) -> list[Quote]:
        """Ashare 无批量接口, 逐只取最新价(K 线末根), 因此只作为末位备源。"""
        targets = [normalize_code(c) for c in list(codes)[:10]]
        out: list[Quote] = []
        for code in targets:
            try:
                kline = await self.fetch_kline(code, days=5)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Ashare 行情 %s 失败: %s", code, exc)
                continue
            if not kline.bars:
                continue
            last = kline.bars[-1]
            prev_close = kline.bars[-2].close if len(kline.bars) > 1 else last.open
            change = last.close - prev_close if prev_close else 0.0
            out.append(
                Quote(
                    code=code, market=detect_market(code), board=board_of(code),
                    price=last.close, prev_close=prev_close, open=last.open,
                    high=last.high, low=last.low,
                    change=round(change, 3),
                    change_pct=round((change / prev_close * 100.0) if prev_close else 0.0, 3),
                    volume=last.volume, amount=last.amount,
                    limit_up=round(prev_close * (1 + limit_pct(code) / 100.0), 2) if prev_close else 0.0,
                    limit_down=round(prev_close * (1 - limit_pct(code) / 100.0), 2) if prev_close else 0.0,
                    source=self.name, ts=time.time(),
                )
            )
        if not out:
            raise ProviderError("Ashare 行情全部失败", source=self.name)
        return out


__all__ = ["AshareProvider"]
