"""企业微信推送。

需求 10「如果有消息推送，请做好推送功能，推送到企业微信上」。

实现要点(综合了两个原始项目最稳的部分):
  * **markdown 消息** + ``<font color="comment">`` 次要信息, 长度按 UTF-8 字节截断
    (markdown 限 4096 字节, 这里保守取 ``push.max_chars``);
  * **以 ``errcode == 0`` 判定成功**, 而不是 HTTP 200 —— 实测 HTTP 200 也可能带错误码;
  * **指数退避重试**(2s → 4s), 次数可配;
  * **内容指纹去重**: 同一指纹在 ``dedupe_window_seconds`` 内只推一次, 避免刷屏;
  * **落库审计**: 每次推送写入 ``push_log``, 页面可查看成功/失败与错误码;
  * Webhook 由用户在「设置」页手工输入, 接口只回显掩码。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import httpx

from ..config import config
from ..core.util import now_cn
from ..store.settings_store import settings_store

logger = logging.getLogger(__name__)

#: 企业微信 markdown 内容上限约 4096 字节, 保守留出余量
MARKDOWN_BYTE_LIMIT = 3800
TEXT_BYTE_LIMIT = 1900

#: 企业微信常见错误码 → 可读原因
ERROR_HINTS = {
    "93000": "Webhook 地址无效或机器人已被移除",
    "45009": "接口调用超过限制(每个机器人每分钟 20 条)",
    "40001": "Webhook key 不正确",
    "40014": "access_token 不合法",
    "41001": "缺少 access_token",
    "301002": "无权限操作该机器人",
}


def clip_bytes(text: str, limit: int) -> str:
    """按 UTF-8 字节安全截断(不会把多字节字符切坏)。"""
    raw = str(text or "")
    if len(raw.encode("utf-8")) <= limit:
        return raw
    # 逐字符累加, 保证不截断半个汉字
    out: list[str] = []
    size = 0
    for char in raw:
        char_size = len(char.encode("utf-8"))
        if size + char_size > limit - 3:
            break
        out.append(char)
        size += char_size
    return "".join(out) + "..."


@dataclass
class PushResult:
    ok: bool
    attempts: int = 0
    skipped: bool = False
    error: str = ""
    errcode: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "attempts": self.attempts, "skipped": self.skipped,
            "error": self.error, "errcode": self.errcode, "detail": self.detail,
        }


@dataclass
class PushMessage:
    """一条待推送消息。"""

    title: str
    body: str
    kind: str = "notice"
    url: str = ""
    mentions: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def to_markdown(self) -> str:
        lines: list[str] = []
        if self.title:
            lines.append(f"## {self.title}")
        if self.body:
            lines.append(self.body)
        if self.url:
            lines.append(f"[📄 点击查看详情]({self.url})")
            lines.append('<font color="comment">如无法直接打开，请复制链接到浏览器</font>')
        lines.append('<font color="comment">自动推送 · 数据仅供研究参考，不构成投资建议</font>')
        return "\n".join(line for line in lines if line)

    def to_text(self) -> str:
        parts = [self.title, self.body]
        if self.url:
            parts.append(f"详情: {self.url}")
        return "\n".join(part for part in parts if part)


class WeComPusher:
    """企业微信机器人推送器。"""

    def __init__(self) -> None:
        self._recent: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    @property
    def webhook(self) -> str:
        return str(config().get("push.wecom_webhook", "") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(config().get("push.enabled", False)) and bool(self.webhook)

    @property
    def mentioned_mobile(self) -> str:
        return str(config().get("push.wecom_mentioned_mobile", "") or "").strip()

    # ------------------------------------------------------------------ #
    def _is_duplicate(self, fingerprint: str) -> bool:
        if not fingerprint:
            return False
        window = float(config().get("push.dedupe_window_seconds", 1800) or 1800)
        last = self._recent.get(fingerprint)
        now = time.time()
        # 顺手清理过期指纹, 防止字典无限增长
        if len(self._recent) > 2000:
            self._recent = {k: v for k, v in self._recent.items() if now - v < window}
        if last is not None and (now - last) < window:
            return True
        self._recent[fingerprint] = now
        return False

    @staticmethod
    def fingerprint_of(title: str, body: str, kind: str) -> str:
        raw = f"{kind}|{title}|{body}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:20]

    # ------------------------------------------------------------------ #
    async def send(self, message: PushMessage, *, force: bool = False) -> PushResult:
        """发送一条消息。``force=True`` 跳过开关与去重(用于"发送测试推送")。"""
        webhook = self.webhook
        if not webhook:
            return PushResult(ok=False, error="未配置企业微信机器人 Webhook")
        if not force and not config().get("push.enabled", False):
            return PushResult(ok=False, skipped=True, error="推送开关未开启")

        fingerprint = message.fingerprint or self.fingerprint_of(
            message.title, message.body, message.kind
        )
        if not force and self._is_duplicate(fingerprint):
            self._log(message, ok=0, error="重复内容, 已跳过", fingerprint=fingerprint)
            return PushResult(ok=True, skipped=True, detail="内容指纹重复, 已跳过")

        limit = int(config().get("push.max_chars", MARKDOWN_BYTE_LIMIT) or MARKDOWN_BYTE_LIMIT)
        content = clip_bytes(message.to_markdown(), limit)
        payload: dict[str, Any] = {"msgtype": "markdown", "markdown": {"content": content}}
        mentions = list(message.mentions)
        mobile = self.mentioned_mobile
        if mobile and mobile not in mentions:
            mentions.append(mobile)
        if mentions:
            payload["markdown"]["mentioned_mobile_list"] = mentions

        retries = max(1, int(config().get("push.retry", 2) or 2) + 1)
        last_error = ""
        last_code: int | None = None

        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(webhook, json=payload)
                body: dict[str, Any] = {}
                if response.content:
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                code = body.get("errcode")
                if response.status_code == 200 and code == 0:
                    self._log(message, ok=1, error="", fingerprint=fingerprint)
                    return PushResult(ok=True, attempts=attempt, errcode=0)
                last_code = code if isinstance(code, int) else None
                hint = ERROR_HINTS.get(str(code), "")
                last_error = (
                    f"HTTP {response.status_code} errcode={code} "
                    f"errmsg={body.get('errmsg', '')} {hint}".strip()
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 10))

        self._log(message, ok=0, error=last_error, fingerprint=fingerprint)
        return PushResult(ok=False, attempts=retries, error=last_error, errcode=last_code)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _log(message: PushMessage, *, ok: int, error: str, fingerprint: str) -> None:
        from ..store.db import db

        try:
            db.execute(
                "INSERT INTO push_log(kind, title, body, ok, error, fingerprint, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (message.kind, message.title[:200], clip_bytes(message.body, 2000),
                 ok, error[:500], fingerprint, time.time()),
            )
        except Exception:  # noqa: BLE001 - 日志失败不能影响推送结果
            pass

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        from ..store.db import db

        rows = db.query(
            "SELECT * FROM push_log ORDER BY id DESC LIMIT ?", (max(1, min(200, limit)),)
        )
        return [
            {
                "kind": row["kind"], "title": row["title"],
                "ok": bool(row["ok"]), "error": row["error"],
                "fingerprint": row["fingerprint"],
                "created_at": row["created_at"],
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["created_at"])),
            }
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        from ..store.db import db

        total = db.query_one("SELECT COUNT(*) AS n FROM push_log")
        ok = db.query_one("SELECT COUNT(*) AS n FROM push_log WHERE ok=1")
        last = db.query_one("SELECT created_at, ok, error FROM push_log ORDER BY id DESC LIMIT 1")
        return {
            "configured": bool(self.webhook),
            "enabled": bool(config().get("push.enabled", False)),
            "total": int(total["n"]) if total else 0,
            "success": int(ok["n"]) if ok else 0,
            "last_at": last["created_at"] if last else None,
            "last_ok": bool(last["ok"]) if last else None,
            "last_error": (last["error"] if last else "") or "",
        }

    # ------------------------------------------------------------------ #
    async def send_text(self, text: str, *, title: str = "通知", kind: str = "notice",
                        force: bool = False) -> PushResult:
        return await self.send(PushMessage(title=title, body=text, kind=kind), force=force)


pusher = WeComPusher()


# --------------------------------------------------------------------------- #
# 业务消息构造
# --------------------------------------------------------------------------- #
def signal_message(
    *, strategy_name: str, items: Iterable[Mapping[str, Any]], base_url: str = "",
    limit: int = 10, extra: str = "",
) -> PushMessage:
    """策略信号推送(如"潜涨观察池新增 N 只")。"""
    rows = list(items)[:limit]
    lines = [f"**策略**：{strategy_name}", f"**入选**：{len(rows)} 只", ""]
    for index, item in enumerate(rows, start=1):
        code = item.get("code", "")
        name = item.get("name", "")
        score = item.get("score", 0)
        reasons = item.get("reasons") or []
        passed = [r.get("name") for r in reasons if r.get("passed")][:3]
        lines.append(
            f"{index}. {name}（{code}） 评分 **{score}**"
            + (f"｜{'/'.join(str(p) for p in passed)}" if passed else "")
        )
    if extra:
        lines.extend(["", extra])
    lines.append("")
    lines.append(f"推送时间：{now_cn().strftime('%Y-%m-%d %H:%M')}")
    return PushMessage(
        title=f"{strategy_name} · 信号提醒",
        body="\n".join(lines),
        kind="strategy_signal",
        url=base_url,
    )


def market_message(*, emotion: Mapping[str, Any], cycle: Mapping[str, Any],
                   breadth: Mapping[str, Any] | None = None,
                   base_url: str = "") -> PushMessage:
    """市场情绪推送。"""
    lines = [
        f"**情绪分**：{emotion.get('score', 0)}（{emotion.get('level', '')}）",
        f"**周期阶段**：{cycle.get('label', '')}｜置信度 {cycle.get('confidence', 0)}",
        f"**操作建议**：{cycle.get('advice', '')}",
    ]
    if breadth:
        lines.append(
            f"**涨跌家数**：涨 {breadth.get('up', 0)} / 跌 {breadth.get('down', 0)}"
            f" / 涨停 {breadth.get('limit_up', 0)} / 跌停 {breadth.get('limit_down', 0)}"
        )
    metrics = emotion.get("metrics") or {}
    if metrics:
        lines.append(
            f"**炸板率**：{metrics.get('broken_rate', 0)}%｜"
            f"**最高连板**：{metrics.get('max_consecutive', 0)} 板"
        )
    lines.append("")
    lines.append(f"推送时间：{now_cn().strftime('%Y-%m-%d %H:%M')}")
    return PushMessage(
        title="市场情绪与周期提醒",
        body="\n".join(lines),
        kind="market_emotion",
        url=base_url,
    )


def source_alert_message(*, capability: str, errors: list[tuple[str, str]], base_url: str = "") -> PushMessage:
    """数据源异常推送。"""
    lines = [f"**能力**：{capability}", "**失败详情**："]
    for name, message in errors[:5]:
        lines.append(f"- {name}：{message[:120]}")
    lines.append("")
    lines.append("请到「数据源」页面检查连通性，或临时锁定到可用源。")
    return PushMessage(
        title="数据源异常提醒", body="\n".join(lines), kind="source_down", url=base_url,
    )


def memory_alert_message(*, report: Mapping[str, Any], base_url: str = "") -> PushMessage:
    process = report.get("process") or {}
    limits = report.get("limits") or {}
    lines = [
        f"**当前 RSS**：{process.get('rss_mb', 0)} MB（峰值 {process.get('peak_rss_mb', 0)} MB）",
        f"**软上限**：{limits.get('soft_limit_mb', 0)} MB（占用 {limits.get('soft_usage_pct', 0)}%）",
        f"**最后动作**：{(report.get('guard') or {}).get('last_action', '')}",
        "",
        "系统已自动压缩缓存；若持续触发请在「设置」页面调大内存阈值或缩小扫描范围。",
    ]
    return PushMessage(
        title="内存占用告警", body="\n".join(lines), kind="memory_warning", url=base_url,
    )


def test_message() -> PushMessage:
    return PushMessage(
        title="StockSpace 推送测试",
        body=(
            "如果你看到这条消息，说明企业微信机器人配置正确。\n\n"
            f"- 服务器时间：{now_cn().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 数据源模式：{config().source_mode}\n"
            "- 该消息由「设置 → 推送」页面的测试按钮触发"
        ),
        kind="test",
    )


__all__ = [
    "WeComPusher", "pusher", "PushMessage", "PushResult",
    "clip_bytes", "signal_message", "market_message",
    "source_alert_message", "memory_alert_message", "test_message",
]
