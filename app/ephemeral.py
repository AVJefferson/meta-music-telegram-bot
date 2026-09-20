from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import EphemeralMessageParameters, InlineKeyboardMarkup, Message

from app.errors import AppError

log = logging.getLogger(__name__)


def message_ref(message: object | None) -> dict | None:
    if message is None:
        return None
    chat = getattr(message, "chat", None)
    eph = getattr(message, "ephemeral_message_id", None)
    mid = getattr(message, "message_id", None)
    chat_id = getattr(chat, "id", None)
    if not eph and not mid:
        return None
    return {
        "chat_id": chat_id,
        "message_id": mid,
        "ephemeral_message_id": int(eph) if eph else None,
    }


def _message_from_ref(ref: dict | None) -> SimpleNamespace | None:
    if not isinstance(ref, dict):
        return None
    eph = ref.get("ephemeral_message_id")
    mid = ref.get("message_id")
    if not eph and not mid:
        return None
    return SimpleNamespace(
        chat=SimpleNamespace(id=ref.get("chat_id")),
        message_id=mid or 0,
        ephemeral_message_id=eph,
    )


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
    """PM: ordinary message. Group/channel: editable ephemeral (Drive/personal), else DM fallback."""
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
    thread_id: int | None = None,
) -> Message | None:
    if message is None:
        return await send_private(
            ctx,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            thread_id=thread_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
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
            return message
        except Exception:
            log.info("ephemeral edit failed; sending new private message")
            return await send_private(
                ctx,
                chat_id=chat_id,
                user_id=user_id,
                text=text,
                thread_id=thread_id,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
    try:
        await ctx.bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return message
    except Exception:
        return await send_private(
            ctx,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            thread_id=thread_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )


async def send_or_edit_private(
    ctx: Any,
    *,
    chat_id: int,
    user_id: int,
    text: str,
    previous: dict | None = None,
    thread_id: int | None = None,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> dict | None:
    existing = _message_from_ref(previous)
    if existing:
        sent = await edit_private_text(
            ctx,
            chat_id=chat_id,
            user_id=user_id,
            message=existing,  # type: ignore[arg-type]
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            thread_id=thread_id,
        )
        return message_ref(sent) or previous
    sent = await send_private(
        ctx,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        thread_id=thread_id,
        parse_mode=parse_mode,
        reply_markup=reply_markup,
    )
    return message_ref(sent)


async def edit_private_markup(
    ctx: Any,
    *,
    chat_id: int,
    user_id: int,
    message: Message | None,
    reply_markup: InlineKeyboardMarkup | None = None,
    text: str | None = None,
    thread_id: int | None = None,
) -> None:
    if message is None:
        if text:
            await send_private(
                ctx, chat_id=chat_id, user_id=user_id, text=text, thread_id=thread_id, reply_markup=reply_markup
            )
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
                await send_private(
                    ctx, chat_id=chat_id, user_id=user_id, text=text, thread_id=thread_id, reply_markup=reply_markup
                )
            return
    try:
        await ctx.bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=reply_markup,
        )
    except Exception:
        if text:
            await send_private(
                ctx, chat_id=chat_id, user_id=user_id, text=text, thread_id=thread_id, reply_markup=reply_markup
            )
