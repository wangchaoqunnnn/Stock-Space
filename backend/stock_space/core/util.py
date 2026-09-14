"""A 股代码/板块/时间 工具。

集中处理三件容易出错的事:
  1. **代码 ↔ 符号**: ``600519`` / ``sh600519`` / ``600519.SH`` 三种写法互转 ——
     北交所 ``920xxx`` 必须识别为 ``bj`` 而非 ``sz``, 否则涨跌停幅度会按 10% 算(实际 30%)。
  2. **板块归属与涨跌停幅度**: 主板 10% / 创业板 20% / 科创板 20% / 北交所 30% / ST 5%。
  3. **交易时段**: 北京时间(UTC+8 固定, 与服务器时区无关)下的集合竞价、连续竞价、休市判定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

#: 北京时间固定偏移 —— 云服务器时区常常是 UTC, 必须显式换算
CN_TZ = timezone(timedelta(hours=8))

_CODE_RE = re.compile(r"^\d{6}$")

#: 交易所前缀
PREFIX_TO_MARKET = {"sh": "sh", "sz": "sz", "bj": "bj"}
MARKET_LABELS = {"sh": "上交所", "sz": "深交所", "bj": "北交所"}


# --------------------------------------------------------------------------- #
# 代码 / 符号
# --------------------------------------------------------------------------- #
def normalize_code(raw: str) -> str:
    """把任意写法的代码规整为 6 位数字。无法识别时抛出 ``ValueError``。"""
    text = str(raw or "").strip().upper()
    if not text:
        raise ValueError("代码为空")
    # 去掉市场前缀/后缀与常见分隔符
    text = text.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    text = text.replace("SH", "").replace("SZ", "").replace("BJ", "")
    text = text.replace("_", "").replace("-", "").replace(" ", "")
    digits = re.sub(r"\D", "", text)
    if not _CODE_RE.match(digits):
        raise ValueError(f"无法识别的证券代码: {raw!r}")
    return digits


def detect_market(code: str) -> str:
    """根据代码段判定交易所 (``sh`` / ``sz`` / ``bj``)。"""
    code = normalize_code(code)
    head2 = code[:2]
    head3 = code[:3]

    # 北交所: 920 新号段 + 43x/83x/87x/88x/889 存量 + 4/8 开头的其他号段
    if head3 in ("920", "889") or head2 in ("43", "83", "87", "88"):
        return "bj"
    # 上交所: 主板 60x, 科创板 688/689, B 股 900
    if head2 in ("60", "68", "90", "50", "51", "52", "56", "58"):
        return "sh"
    if head3 in ("688", "689", "900"):
        return "sh"
    # 深交所: 主板 000/001/002/003, 创业板 300/301/302, B 股 200, 基金 15/16/18
    if head3 in ("000", "001", "002", "003", "300", "301", "302", "200", "159", "150", "160", "180"):
        return "sz"
    if head2 in ("00", "30", "20", "15", "16", "18"):
        return "sz"
    # 无法判定时按深市处理(覆盖 0/3 段), 其余默认沪市
    return "sz" if head2.startswith(("0", "3")) else "sh"


def to_symbol(code: str, *, upper: bool = False) -> str:
    """``600519`` -> ``sh600519``(小写) 或 ``SH600519``。"""
    market = detect_market(code)
    symbol = f"{market}{normalize_code(code)}"
    return symbol.upper() if upper else symbol


def from_symbol(symbol: str) -> tuple[str, str]:
    """``sh600519`` -> ``('sh', '600519')``。"""
    text = str(symbol or "").strip().lower()
    if len(text) > 6 and text[:2] in PREFIX_TO_MARKET:
        return text[:2], normalize_code(text[2:])
    code = normalize_code(text)
    return detect_market(code), code


def secid(code: str) -> str:
    """东方财富 secid 格式: ``1.600519`` / ``0.000001`` / ``0.920001``。"""
    market = detect_market(code)
    prefix = {"sh": "1", "sz": "0", "bj": "0"}[market]
    return f"{prefix}.{normalize_code(code)}"


def tencent_symbol(code: str) -> str:
    """腾讯行情代码格式: ``sh600519`` / ``sz000001`` / ``bj920001``。"""
    return to_symbol(code)


def sina_symbol(code: str) -> str:
    return to_symbol(code)


# --------------------------------------------------------------------------- #
# 板块与涨跌停
# --------------------------------------------------------------------------- #
def board_of(code: str) -> str:
    """返回板块中文名。"""
    code = normalize_code(code)
    market = detect_market(code)
    head3 = code[:3]
    if market == "bj":
        return "北交所"
    if head3 in ("688", "689"):
        return "科创板"
    if head3 in ("300", "301", "302"):
        return "创业板"
    if code.startswith("900") or code.startswith("200"):
        return "B股"
    return "主板"


def limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度(百分数)。ST 5%, 北交所 30%, 创业板/科创板 20%, 主板 10%。"""
    upper = (name or "").upper().replace(" ", "")
    if "ST" in upper:
        return 5.0
    board = board_of(code)
    if board == "北交所":
        return 30.0
    if board in ("创业板", "科创板"):
        return 20.0
    return 10.0


#: 常用指数(名称, 代码, 交易所) —— 供首页指数卡片使用
MAJOR_INDICES: tuple[tuple[str, str, str], ...] = (
    ("上证指数", "000001", "sh"),
    ("深证成指", "399001", "sz"),
    ("创业板指", "399006", "sz"),
    ("科创50", "000688", "sh"),
    ("沪深300", "000300", "sh"),
    ("北证50", "899050", "bj"),
    ("中证500", "000905", "sh"),
    ("中证1000", "000852", "sh"),
)

#: 主要指数对应的东方财富 secid(指数前缀规则与个股不同, 需显式给出)
INDEX_SECIDS: dict[str, str] = {
    "000001": "1.000001",
    "399001": "0.399001",
    "399006": "0.399006",
    "000688": "1.000688",
    "000300": "1.000300",
    "899050": "0.899050",
    "000905": "1.000905",
    "000852": "1.000852",
}


def index_secid(code: str) -> str:
    """指数的东方财富 secid(优先查表, 未收录时按规则推导)。"""
    code = normalize_code(code)
    return INDEX_SECIDS.get(code) or secid(code)


# --------------------------------------------------------------------------- #
# 交易时段
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionInfo:
    phase: str           # pre_open / call_auction / trading / lunch_break / closed / weekend
    label: str           # 中文说明
    is_trading: bool     # 是否处于连续竞价(需要高频刷新)
    is_trading_day: bool
    should_poll: bool    # 前端是否应轮询
    interval_seconds: int
    session_date: date
    next_open: datetime | None
    now_cn: datetime

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "label": self.label,
            "is_trading": self.is_trading,
            "is_trading_day": self.is_trading_day,
            "should_poll": self.should_poll,
            "interval_seconds": self.interval_seconds,
            "session_date": self.session_date.isoformat(),
            "next_open": self.next_open.isoformat() if self.next_open else None,
            "now_cn": self.now_cn.strftime("%Y-%m-%d %H:%M:%S"),
        }


#: 简化节假日表(仅覆盖法定休市日; 缺失的年份按"周一~周五即交易日"处理)。
#: 需要精确日历时可通过 ``config/rules.toml`` 的 ``[calendar] holidays`` 追加。
_DEFAULT_HOLIDAYS: frozenset[str] = frozenset()


def _phase_of(now: dtime) -> tuple[str, str, bool, bool, int]:
    """返回 (phase, label, is_trading, should_poll, interval_seconds)。"""
    t = (now.hour, now.minute)
    if (9, 15) <= t < (9, 25):
        return "call_auction", "集合竞价", False, True, 60
    if (9, 25) <= t < (9, 30):
        return "pre_open", "开盘前撮合", False, True, 60
    if (9, 30) <= t < (11, 30):
        return "trading", "连续竞价(上午)", True, True, 30
    if (11, 30) <= t < (13, 0):
        return "lunch_break", "午间休市", False, True, 60
    if (13, 0) <= t < (15, 0):
        return "trading", "连续竞价(下午)", True, True, 30
    return "closed", "已收盘", False, False, 300


def market_session(
    now: datetime | None = None,
    *,
    holidays: frozenset[str] | set[str] | None = None,
) -> SessionInfo:
    """判定当前中国市场时段。时间一律按北京时间计算。"""
    now_cn = (now or datetime.now(timezone.utc)).astimezone(CN_TZ)
    holidays = holidays if holidays is not None else _DEFAULT_HOLIDAYS
    today = now_cn.date()
    is_trading_day = today.weekday() < 5 and today.isoformat() not in holidays

    if not is_trading_day:
        phase = "weekend" if today.weekday() >= 5 else "holiday"
        return SessionInfo(
            phase=phase,
            label="周末休市" if phase == "weekend" else "节假日休市",
            is_trading=False,
            is_trading_day=False,
            should_poll=False,
            interval_seconds=600,
            session_date=today,
            next_open=_next_open(today, holidays),
            now_cn=now_cn,
        )

    phase, label, is_trading, should_poll, interval = _phase_of(now_cn.time())
    return SessionInfo(
        phase=phase,
        label=label,
        is_trading=is_trading,
        is_trading_day=True,
        should_poll=should_poll,
        interval_seconds=interval,
        session_date=today,
        next_open=None if should_poll else _next_open(today, holidays),
        now_cn=now_cn,
    )


def _next_open(from_date: date, holidays: frozenset[str] | set[str], open_time: dtime = dtime(9, 30)) -> datetime:
    cursor = from_date
    for _ in range(30):
        cursor = cursor + timedelta(days=1)
        if cursor.weekday() < 5 and cursor.isoformat() not in holidays:
            return datetime.combine(cursor, open_time, tzinfo=CN_TZ)
    return datetime.combine(from_date + timedelta(days=1), open_time, tzinfo=CN_TZ)


def today_str() -> str:
    return datetime.now(CN_TZ).date().isoformat()


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


__all__ = [
    "CN_TZ",
    "SessionInfo",
    "normalize_code",
    "detect_market",
    "to_symbol",
    "from_symbol",
    "secid",
    "tencent_symbol",
    "sina_symbol",
    "board_of",
    "limit_pct",
    "market_session",
    "today_str",
    "now_cn",
]
