"""技术指标计算。

全部基于 ``numpy`` 的一维数组实现, 输入 ``float`` 列表或数组, 输出等长数组
(不足窗口的位置用 ``NaN`` 填充)。**不使用 pandas 的 rolling**, 原因:
  * 策略计算发生在事件循环之外的线程里, numpy 的确定性更好;
  * 显式实现便于单元测试与逐位比对。

口径约定:
  * 所有均线为**简单移动平均**(与各项目原始口径一致);
  * ATR 使用 Wilder 平滑(与主流软件一致);
  * RSI 使用 Wilder 平滑;
  * MACD(12, 26, 9) / KDJ(9, 3, 3) / BOLL(20, 2)。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

__all__ = [
    "to_array",
    "sma",
    "ema",
    "wilder",
    "ma_series",
    "slope",
    "atr",
    "rsi",
    "macd",
    "kdj",
    "boll",
    "max_drawdown",
    "annualized_volatility",
    "pct_change",
    "safe_float",
    "last_valid",
    "rolling_max",
    "rolling_min",
    "cross_over",
    "tag_indicators",
]


def to_array(values: Sequence[float] | np.ndarray) -> np.ndarray:
    """转成 float64 数组; NaN/None 保留为 NaN。"""
    if isinstance(values, np.ndarray):
        array = values.astype(np.float64, copy=False)
    else:
        array = np.asarray(list(values), dtype=np.float64)
    return array


def safe_float(value: Any, default: float = 0.0) -> float:
    """把可能是 NaN/None 的值转成有限 float。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def last_valid(array: np.ndarray, default: float = 0.0) -> float:
    """取最后一个有效值(从尾部向前跳过 NaN)。"""
    if array is None or len(array) == 0:
        return default
    for index in range(len(array) - 1, -1, -1):
        value = array[index]
        if not math.isnan(value):
            return float(value)
    return default


def sma(values: Sequence[float], window: int) -> np.ndarray:
    """简单移动平均。"""
    array = to_array(values)
    out = np.full(array.shape, np.nan, dtype=np.float64)
    if window <= 0 or len(array) < window:
        return out
    cumsum = np.cumsum(np.insert(array, 0, 0.0))
    out[window - 1:] = (cumsum[window:] - cumsum[:-window]) / window
    return out


def ema(values: Sequence[float], span: int) -> np.ndarray:
    """指数移动平均(alpha = 2/(span+1)), 首值用第一个有效值初始化。"""
    array = to_array(values)
    out = np.full(array.shape, np.nan, dtype=np.float64)
    if span <= 0 or len(array) == 0:
        return out
    alpha = 2.0 / (span + 1.0)
    prev = array[0]
    out[0] = prev
    for index in range(1, len(array)):
        prev = alpha * array[index] + (1 - alpha) * prev
        out[index] = prev
    return out


def wilder(values: Sequence[float], period: int) -> np.ndarray:
    """Wilder 平滑(RSI/ATR 使用)。首值取前 ``period`` 个的均值。"""
    array = to_array(values)
    out = np.full(array.shape, np.nan, dtype=np.float64)
    if period <= 0 or len(array) < period:
        return out
    seed = float(np.nanmean(array[:period]))
    out[period - 1] = seed
    prev = seed
    for index in range(period, len(array)):
        prev = (prev * (period - 1) + array[index]) / period
        out[index] = prev
    return out


def ma_series(values: Sequence[float], windows: Sequence[int] = (5, 10, 20, 60, 120)) -> dict[str, np.ndarray]:
    return {f"ma{window}": sma(values, window) for window in windows}


def slope(values: Sequence[float], window: int = 5) -> float:
    """最近 ``window`` 个有效值的线性回归斜率(归一化为"每日涨跌百分比")。"""
    array = to_array(values)
    valid = array[~np.isnan(array)]
    if len(valid) < 2:
        return 0.0
    segment = valid[-max(2, window):]
    x = np.arange(len(segment), dtype=np.float64)
    denominator = float(np.sum((x - x.mean()) ** 2))
    if denominator == 0:
        return 0.0
    beta = float(np.sum((x - x.mean()) * (segment - segment.mean())) / denominator)
    base = float(np.mean(segment))
    if base == 0:
        return 0.0
    return beta / base * 100.0  # 百分比/日


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], period: int = 14) -> np.ndarray:
    """平均真实波幅(Wilder 平滑)。"""
    high_arr, low_arr, close_arr = to_array(high), to_array(low), to_array(close)
    length = min(len(high_arr), len(low_arr), len(close_arr))
    if length == 0:
        return np.array([], dtype=np.float64)
    high_arr, low_arr, close_arr = high_arr[:length], low_arr[:length], close_arr[:length]
    true_range = np.full(length, np.nan, dtype=np.float64)
    true_range[0] = high_arr[0] - low_arr[0]
    for index in range(1, length):
        prev_close = close_arr[index - 1]
        true_range[index] = max(
            high_arr[index] - low_arr[index],
            abs(high_arr[index] - prev_close),
            abs(low_arr[index] - prev_close),
        )
    return wilder(true_range, period)


def rsi(close: Sequence[float], period: int = 14) -> np.ndarray:
    """相对强弱指标(Wilder)。"""
    array = to_array(close)
    out = np.full(array.shape, np.nan, dtype=np.float64)
    if len(array) < period + 1:
        return out
    delta = np.diff(array)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = wilder(gain, period)
    avg_loss = wilder(loss, period)
    for index in range(period, len(array)):
        g = avg_gain[index - 1] if index - 1 < len(avg_gain) else np.nan
        l = avg_loss[index - 1] if index - 1 < len(avg_loss) else np.nan
        if math.isnan(g) or math.isnan(l):
            continue
        if l == 0:
            out[index] = 100.0
        else:
            rs = g / l
            out[index] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(
    close: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD: 返回 ``(dif, dea, hist)``。"""
    array = to_array(close)
    if len(array) == 0:
        empty = np.array([], dtype=np.float64)
        return empty, empty, empty
    dif = ema(array, fast) - ema(array, slow)
    dea = ema(dif, signal)
    return dif, dea, (dif - dea) * 2.0


def kdj(
    high: Sequence[float], low: Sequence[float], close: Sequence[float],
    period: int = 9, k_period: int = 3, d_period: int = 3,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """KDJ(9,3,3)。"""
    high_arr, low_arr, close_arr = to_array(high), to_array(low), to_array(close)
    length = min(len(high_arr), len(low_arr), len(close_arr))
    k = np.full(length, np.nan, dtype=np.float64)
    d = np.full(length, np.nan, dtype=np.float64)
    j = np.full(length, np.nan, dtype=np.float64)
    if length < period:
        return k, d, j
    prev_k, prev_d = 50.0, 50.0
    for index in range(length):
        if index < period - 1:
            continue
        highest = float(np.max(high_arr[index - period + 1:index + 1]))
        lowest = float(np.min(low_arr[index - period + 1:index + 1]))
        rsv = 50.0 if highest == lowest else (close_arr[index] - lowest) / (highest - lowest) * 100.0
        prev_k = (prev_k * (k_period - 1) + rsv) / k_period
        prev_d = (prev_d * (d_period - 1) + prev_k) / d_period
        k[index], d[index], j[index] = prev_k, prev_d, 3 * prev_k - 2 * prev_d
    return k, d, j


def boll(
    close: Sequence[float], period: int = 20, multiplier: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """布林带: 返回 ``(upper, mid, lower)``。"""
    array = to_array(close)
    mid = sma(array, period)
    std = np.full(array.shape, np.nan, dtype=np.float64)
    if len(array) >= period:
        for index in range(period - 1, len(array)):
            std[index] = float(np.std(array[index - period + 1:index + 1], ddof=0))
    return mid + multiplier * std, mid, mid - multiplier * std


def max_drawdown(closes: Sequence[float]) -> float:
    """最大回撤(正数百分值, 例如 12.5 表示 -12.5%)。"""
    array = to_array(closes)
    array = array[~np.isnan(array)]
    if len(array) < 2:
        return 0.0
    peak = np.maximum.accumulate(array)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.where(peak > 0, (peak - array) / peak, 0.0)
    return float(np.max(drawdown) * 100.0)


def annualized_volatility(closes: Sequence[float], periods: int = 252) -> float:
    """年化波动率(百分值)。"""
    array = to_array(closes)
    if len(array) < 3:
        return 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(array) / array[:-1]
    returns = returns[np.isfinite(returns)]
    if len(returns) < 2:
        return 0.0
    return float(np.std(returns, ddof=1) * math.sqrt(periods) * 100.0)


def pct_change(closes: Sequence[float], periods: int = 1) -> float:
    """``periods`` 个周期前的涨跌幅(百分值)。"""
    array = to_array(closes)
    array = array[~np.isnan(array)]
    if len(array) <= periods or array[-periods - 1] == 0:
        return 0.0
    return float((array[-1] / array[-periods - 1] - 1.0) * 100.0)


def rolling_max(values: Sequence[float], window: int) -> np.ndarray:
    array = to_array(values)
    out = np.full(array.shape, np.nan, dtype=np.float64)
    if window <= 0 or len(array) < window:
        return out
    for index in range(window - 1, len(array)):
        out[index] = float(np.max(array[index - window + 1:index + 1]))
    return out


def rolling_min(values: Sequence[float], window: int) -> np.ndarray:
    array = to_array(values)
    out = np.full(array.shape, np.nan, dtype=np.float64)
    if window <= 0 or len(array) < window:
        return out
    for index in range(window - 1, len(array)):
        out[index] = float(np.min(array[index - window + 1:index + 1]))
    return out


def cross_over(fast: Sequence[float], slow: Sequence[float]) -> bool:
    """``fast`` 在最后一根上穿 ``slow``。"""
    fast_arr, slow_arr = to_array(fast), to_array(slow)
    if len(fast_arr) < 2 or len(slow_arr) < 2:
        return False
    return bool(
        fast_arr[-2] <= slow_arr[-2] and fast_arr[-1] > slow_arr[-1]
        and not math.isnan(fast_arr[-2]) and not math.isnan(slow_arr[-2])
    )


# --------------------------------------------------------------------------- #
# 指标打包(给前端画图/给策略复用)
# --------------------------------------------------------------------------- #
def tag_indicators(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], volumes: Sequence[float]
) -> dict[str, list[float | None]]:
    """一次性算出前端需要的全部指标, 并把 NaN 转成 ``None``(JSON 友好)。"""
    closes_arr = to_array(closes)
    ma20 = sma(closes_arr, 20)
    ma60 = sma(closes_arr, 60)
    ma5 = sma(closes_arr, 5)
    ma10 = sma(closes_arr, 10)
    dif, dea, hist = macd(closes_arr)
    k, d, j = kdj(highs, lows, closes_arr)
    upper, mid, lower = boll(closes_arr)
    rsi14 = rsi(closes_arr, 14)
    atr14 = atr(highs, lows, closes_arr, 14)
    vol_ma5 = sma(volumes, 5)
    vol_ma20 = sma(volumes, 20)

    def clean(array: np.ndarray) -> list[float | None]:
        return [None if (value is None or math.isnan(value)) else round(float(value), 4) for value in array]

    return {
        "ma5": clean(ma5), "ma10": clean(ma10), "ma20": clean(ma20), "ma60": clean(ma60),
        "dif": clean(dif), "dea": clean(dea), "macd": clean(hist),
        "k": clean(k), "d": clean(d), "j": clean(j),
        "boll_upper": clean(upper), "boll_mid": clean(mid), "boll_lower": clean(lower),
        "rsi14": clean(rsi14), "atr14": clean(atr14),
        "vol_ma5": clean(vol_ma5), "vol_ma20": clean(vol_ma20),
    }
