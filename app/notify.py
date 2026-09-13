from __future__ import annotations

import logging
from typing import Any

from app.errors import AppError, log_error, redact, to_app_error

log = logging.getLogger(__name__)


async def notify_user(
    ctx: Any,
    user_id: int,
    text: str,
    *,
    chat_id: int | None = None,
    thread_id: int | None = None,
    parse_mode: str | None = None,
) -> None:
    from app.ephemeral import send_private

    try:
        await send_private(
            ctx,
            chat_id=chat_id or user_id,
            user_id=user_id,
            text=text,
            thread_id=thread_id,
            parse_mode=parse_mode,
        )
    except Exception:
        log.warning("notify_user failed user_id=%s", user_id, exc_info=True)


async def notify_error(
    ctx: Any,
    user_id: int | None,
    exc: BaseException,
    *,
    chat_id: int | None = None,
    pending_id: int | None = None,
) -> AppError:
    err = to_app_error(exc)
    log_error(err.code, user_id=user_id, chat_id=chat_id, pending_id=pending_id, exc=exc)
    if user_id:
        await notify_user(ctx, user_id, err.user_message, chat_id=chat_id)
    return err


async def alert_admin(ctx: Any, text: str) -> None:
    admin_id = int(getattr(ctx.settings, "admin_telegram_user_id", 0) or 0)
    if not admin_id:
        log.error("admin alert (no admin id): %s", redact(text))
        return
    try:
        await ctx.bot.send_message(admin_id, redact(text)[:3500])
    except Exception as exc:
        log.error("admin alert failed: %s", redact(str(exc)))
