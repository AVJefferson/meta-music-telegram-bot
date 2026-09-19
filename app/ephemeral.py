from __future__ import annotations

import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import EphemeralMessageParameters, InlineKeyboardMarkup, Message

from app.errors import AppError

log = logging.getLogger(__name__)


async def send_private(
    ctx: Any,
    *,
    chat_id: int,
    user_id: int,
    text: str,
    thread_id: int | None = None,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool | None = True,
) -> Message | None:
    """DM, or group ephemeral, or DM fallback. Never posts OAuth URLs to the public group."""
    if chat_id > 0:
        return await ctx.bot.send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    try:
        return await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=user_id),
            message_thread_id=thread_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except (TelegramBadRequest, TelegramForbiddenError, Exception) as exc:
        log.info("ephemeral send failed chat=%s user=%s: %s", chat_id, user_id, type(exc).__name__)
        try:
            return await ctx.bot.send_message(
                user_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            try:
                await ctx.bot.send_message(
                    chat_id,
                    "Open a private chat with this bot (/start) to continue.",
                    message_thread_id=thread_id,
                )
            except Exception:
                log.warning("public fallback failed chat=%s", chat_id)
            raise AppError("ephemeral_unsupported") from exc


async def edit_private_text(
    ctx: Any,
    *,
    chat_id: int,
    user_id: int,
    message: Message | None,
    text: str,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if message is None:
        await send_private(ctx, chat_id=chat_id, user_id=user_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    eph_id = getattr(message, "ephemeral_message_id", None)
    if eph_id:
        try:
            await ctx.bot.edit_ephemeral_message_text(
                text=text,
                chat_id=chat_id,
                receiver_user_id=user_id,
                ephemeral_message_id=int(eph_id),
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            log.info("ephemeral edit failed; sending new private message")
            await send_private(ctx, chat_id=chat_id, user_id=user_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
    try:
        await ctx.bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except Exception:
        await send_private(ctx, chat_id=chat_id, user_id=user_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)


async def edit_private_markup(
    ctx: Any,
    *,
    chat_id: int,
    user_id: int,
    message: Message | None,
    reply_markup: InlineKeyboardMarkup | None = None,
    text: str | None = None,
) -> None:
    if message is None:
        if text:
            await send_private(ctx, chat_id=chat_id, user_id=user_id, text=text, reply_markup=reply_markup)
        return
    eph_id = getattr(message, "ephemeral_message_id", None)
    if eph_id:
        try:
            await ctx.bot.edit_ephemeral_message_reply_markup(
                chat_id=chat_id,
                receiver_user_id=user_id,
                ephemeral_message_id=int(eph_id),
                reply_markup=reply_markup,
            )
            return
        except Exception:
            log.info("ephemeral markup edit failed; sending new private message")
            if text:
                await send_private(ctx, chat_id=chat_id, user_id=user_id, text=text, reply_markup=reply_markup)
            return
    try:
        await ctx.bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=reply_markup,
        )
    except Exception:
        if text:
            await send_private(ctx, chat_id=chat_id, user_id=user_id, text=text, reply_markup=reply_markup)
