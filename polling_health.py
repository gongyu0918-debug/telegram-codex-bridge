from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


POLLING_PROBE_TYPES: set[type] = set()
POLLING_PROBE_APPLICATIONS: dict[int, Any] = {}


@dataclass
class PollingHealth:
    last_ok_at: float | None = None
    last_conflict_at: float | None = None
    last_error_text: str | None = None
    alerted_conflict: bool = False
    last_disconnect_at: float | None = None
    last_recovered_at: float | None = None
    reconnect_notice_pending: bool = False
    last_reconnect_notice_for: float | None = None


def format_elapsed(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def mark_poll_ok(
    application: Any,
    complete_reconnect_notice: Callable[[Any, float, float], Awaitable[bool]],
) -> None:
    health: PollingHealth | None = application.bot_data.get("health")
    if not health:
        return
    previous_disconnect_at = health.last_disconnect_at
    health.last_ok_at = time.time()
    health.last_error_text = None
    if health.reconnect_notice_pending and previous_disconnect_at:
        asyncio.create_task(
            complete_reconnect_notice(application, previous_disconnect_at, health.last_ok_at)
        )


def install_polling_probe(
    bot_instance: Any,
    application: Any,
    mark_poll_ok_callback: Callable[[Any], None],
) -> None:
    bot_type = type(bot_instance)
    POLLING_PROBE_APPLICATIONS[id(bot_instance)] = application
    if bot_type in POLLING_PROBE_TYPES:
        return
    original_get_updates = bot_type.get_updates

    async def wrapped_get_updates(self, *args, **kwargs):
        updates = await original_get_updates(self, *args, **kwargs)
        bound_application = POLLING_PROBE_APPLICATIONS.get(id(self))
        if bound_application:
            mark_poll_ok_callback(bound_application)
        return updates

    bot_type.get_updates = wrapped_get_updates
    POLLING_PROBE_TYPES.add(bot_type)


def reconnect_notice_chat_ids(application: Any) -> list[int]:
    settings = application.bot_data["settings"]
    store = application.bot_data["store"]
    if settings.allowed_chat_ids:
        return sorted(settings.allowed_chat_ids)
    prefs = store.data.get("chat_prefs", {})
    if not isinstance(prefs, dict):
        return []
    chat_ids: list[int] = []
    for raw in prefs.keys():
        try:
            chat_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return sorted(set(chat_ids))


async def send_reconnect_notice(
    application: Any,
    disconnected_at: float,
    recovered_at: float,
) -> bool:
    duration = max(0, int(recovered_at - disconnected_at))
    text = (
        "Telegram 连接已恢复。\n"
        f"断线时长: {format_elapsed(duration)}\n"
        "断线期间的积压消息会继续拉取处理。"
    )
    delivered = False
    for chat_id in reconnect_notice_chat_ids(application):
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
            delivered = True
        except Exception:
            continue
    return delivered


async def complete_reconnect_notice(
    application: Any,
    disconnected_at: float,
    recovered_at: float,
) -> bool:
    health: PollingHealth | None = application.bot_data.get("health")
    if not health:
        return False
    if not health.reconnect_notice_pending:
        return False
    if health.last_reconnect_notice_for == disconnected_at:
        return False
    delivered = await send_reconnect_notice(application, disconnected_at, recovered_at)
    if not delivered and reconnect_notice_chat_ids(application):
        return False
    health.last_recovered_at = recovered_at
    health.reconnect_notice_pending = False
    health.last_reconnect_notice_for = disconnected_at
    return True


async def monitor_bridge_health(context: Any) -> None:
    application = context.application
    settings = application.bot_data["settings"]
    health: PollingHealth = application.bot_data["health"]
    if health.reconnect_notice_pending and health.last_disconnect_at:
        if health.last_ok_at and health.last_ok_at >= health.last_disconnect_at:
            await complete_reconnect_notice(
                application,
                health.last_disconnect_at,
                health.last_ok_at,
            )
    if not health.last_conflict_at:
        return
    if health.alerted_conflict:
        return
    if health.last_ok_at and health.last_ok_at > health.last_conflict_at:
        return
    text = (
        "检测到 Telegram 轮询冲突。\n"
        "当前 token 可能同时被别的实例占用。\n"
        "建议检查其他机器、测试脚本或旧进程。"
    )
    for chat_id in settings.allowed_chat_ids:
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            continue
    health.alerted_conflict = True

