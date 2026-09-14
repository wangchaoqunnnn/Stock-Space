"""缩量回调后温和放量 —— 逻辑验证（不依赖外网）。

用构造的 K 线直接喂给策略，验证两件事：
  1. 符合心法的形态能入选（缩量回调 + 温和放量阳线）；
  2. **放量下跌必须被一票否决**（"放量下跌是出货"）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from stock_space.engines.base import build_series  # noqa: E402
from stock_space.engines import get as get_strategy  # noqa: E402
from stock_space.models import Bar, KLine, Quote  # noqa: E402


def make_bars(closes, volumes, opens=None):
    bars = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = opens[i] if opens else (prev if i else c)
        hi = max(o, c) * 1.004
        lo = min(o, c) * 0.996
        bars.append(Bar(
            date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
            open=round(o, 2), high=round(hi, 2), low=round(lo, 2), close=round(c, 2),
            volume=float(volumes[i]), amount=float(volumes[i]) * c,
        ))
        prev = c
    return bars


def build(closes, volumes, opens=None):
    bars = make_bars(closes, volumes, opens)
    quote = Quote(code="600519", name="测试股", price=bars[-1].close,
                  prev_close=bars[-2].close, board="主板")
    kline = KLine(code="600519", name="测试股", period="day", bars=bars, source="test")
    return build_series(quote, kline)


def scenario_shrink_pullback():
    """符合心法：温和上涨 → 缩量阴线回调（跌幅收窄）→ 温和放量阳线。

    上涨斜率刻意取温和值(0.15%/日)：若用陡峭上涨(0.4%/日)，价格会远离 MA20，
    回调还没触及均线就算"守住支撑"，反而不符合"回调到均线附近"的真实形态。
    """
    closes, vols = [], []
    price = 10.0
    for i in range(95):
        price *= 1.0030
        closes.append(price)
        vols.append(1_000_000)
    # 回调 3 日：缩量阴线，跌幅逐日收窄
    for drop, v in ((-0.030, 0.45), (-0.015, 0.42), (-0.007, 0.40)):
        price = price * (1 + drop)
        closes.append(price)
        vols.append(1_000_000 * v)
    # 温和放量阳线：量 ≈ 1.5× 回调期均量，涨幅 +2.2%
    price = price * 1.022
    closes.append(price)
    vols.append(1_000_000 * 0.62)
    return closes, vols


def scenario_dump_pullback():
    """反面：回调期出现放量下跌 —— 必须被一票否决。"""
    closes, vols = [], []
    price = 10.0
    for i in range(95):
        price *= 1.004
        closes.append(price)
        vols.append(1_000_000)
    for drop, v in ((-0.030, 0.45), (-0.055, 2.20), (-0.006, 0.40)):   # 中间一根放量 2.2×
        price = price * (1 + drop)
        closes.append(price)
        vols.append(1_000_000 * v)
    price = price * 1.022
    closes.append(price)
    vols.append(1_000_000 * 0.62)
    return closes, vols


def scenario_no_rebound():
    """反面：缩量回调后继续阴线（没有温和放量阳线）。"""
    closes, vols = [], []
    price = 10.0
    for i in range(95):
        price *= 1.004
        closes.append(price)
        vols.append(1_000_000)
    for drop, v in ((-0.030, 0.45), (-0.016, 0.42), (-0.020, 0.40)):
        price = price * (1 + drop)
        closes.append(price)
        vols.append(1_000_000 * v)
    return closes, vols


def show(name, closes, vols):
    strategy = get_strategy("volume_shrink_rebound")
    series = build(closes, vols)
    signal = strategy.evaluate(series, {})
    print("=" * 78)
    print(f"{name}")
    print(f"  score={signal.score}  passed={signal.passed}  "
          f"stop={signal.stop_loss}  take={signal.take_profit}")
    print(f"  note={signal.note or '(无否决)'}")
    for cond in signal.reasons:
        flag = "PASS" if cond.passed else "FAIL"
        print(f"    [{flag}] {cond.name}: {cond.value}  ({cond.threshold})")
    key_metrics = {k: v for k, v in signal.metrics.items()
                   if k in ("pullback_days", "pullback_drop_pct", "pullback_vol_ratio",
                            "max_pullback_vol_ratio", "rebound_pct", "rebound_vol_ratio",
                            "dump_volume", "support_held", "trend_gain_pct", "atr")}
    print(f"  关键指标: {key_metrics}")
    return signal


def main() -> int:
    ok = True

    good = show("场景A: 缩量回调 + 温和放量阳线（期望入选）",
                *scenario_shrink_pullback())
    if not good.passed:
        print("  !! 期望入选但被拒绝")
        ok = False
    if not (0 < good.stop_loss < good.take_profit):
        print("  !! 止损/止盈价格异常")
        ok = False
    # 止损应≈2×ATR
    if good.metrics.get("stop_distance_pct", 0) <= 0:
        print("  !! 止损距离未计算")
        ok = False

    dump = show("场景B: 回调中期放量下跌（期望否决）", *scenario_dump_pullback())
    if dump.passed:
        print("  !! 放量下跌竟然入选 —— 心法红线失效")
        ok = False
    if "放量下跌" not in (dump.note or ""):
        print("  !! 否决理由未说明是放量下跌")
        ok = False

    no_reb = show("场景C: 缩量回调但无放量阳线（期望不入选）", *scenario_no_rebound())
    if no_reb.passed:
        print("  !! 没有放量阳线竟然入选")
        ok = False

    print("=" * 78)
    print("结论:", "全部符合预期" if ok else "存在不符合预期的行为")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
