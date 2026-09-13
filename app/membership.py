from __future__ import annotations

import logging
import time
from typing import Any

from app.errors import AppError
from app.rate_limit import check_rate

log = logging.getLogger(__name__)

CACHE_TTL = 60.0
FAILURE_GRACE = 300.0
MAX_ENTRIES = 2048

_ALLOWED = {"creator", "administrator", "member"}
_cache: dict[tuple[int, int], tuple[float, bool]] = {}


def member_status(member: Any) -> str:
    status = member.status
    return str(getattr(status, "value", status))


def status_allows(member: Any) -> bool:
    status = member_status(member)
    if status in _ALLOWED:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


def clear_cache() -> None:
    _cache.clear()


def is_admin(ctx: Any, user_id: int | None) -> bool:
    if not user_id:
        return False
    admin_id = int(getattr(ctx.settings, "admin_telegram_user_id", 0) or 0)
    return bool(admin_id) and int(user_id) == admin_id


def _same_id(left: Any, right: Any) -> bool:
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


def user_is_bot(user: Any, bot: Any | None = None) -> bool:
    """True for Telegram bots, including this bot (self-echo / bot-to-bot)."""
    if user is None:
        return False
    if bool(getattr(user, "is_bot", False)):
        return True
    bot_id = getattr(bot, "id", None) if bot is not None else None
    user_id = getattr(user, "id", None)
    return bot_id is not None and user_id is not None and _same_id(user_id, bot_id)


def message_from_bot(message: Any, bot: Any | None = None) -> bool:
    if message is None:
        return False
    if user_is_bot(getattr(message, "from_user", None), bot):
        return True
    via = getattr(message, "via_bot", None)
    via_id = getattr(via, "id", None) if via is not None else None
    bot_id = getattr(bot, "id", None) if bot is not None else None
    if _same_id(via_id, bot_id):
        # Inline @bot search posts text as via_bot; allow that. Drop media so FLACs cannot echo.
        if _message_has_media(message):
            return True
        return False
    return getattr(message, "sender_business_bot", None) is not None


def _message_has_media(message: Any) -> bool:
    return any(
        getattr(message, name, None) is not None
        for name in ("document", "audio", "voice", "video", "video_note", "sticker")
    )


def ignore_bot_update(update: Any, bot: Any | None = None) -> bool:
    """Drop bot-originated traffic that can echo FLACs/search. Keep my_chat_member."""
    if update is None:
        return False
    if getattr(update, "my_chat_member", None) is not None and getattr(update, "message", None) is None:
        return False
    message = (
        getattr(update, "message", None)
        or getattr(update, "edited_message", None)
        or getattr(update, "channel_post", None)
        or getattr(update, "edited_channel_post", None)
        or getattr(update, "business_message", None)
    )
    if message is not None:
        return message_from_bot(message, bot)
    callback = getattr(update, "callback_query", None)
    if callback is not None:
        return user_is_bot(getattr(callback, "from_user", None), bot)
    reaction = getattr(update, "message_reaction", None)
    if reaction is not None:
        return user_is_bot(getattr(reaction, "user", None), bot)
    return False


def _dm_requires_known(ctx: Any) -> bool:
    return bool(getattr(ctx.settings, "dm_requires_known_chat", True))


async def _get_chat_member(ctx: Any, chat_id: int, user_id: int) -> bool:
    key = (int(chat_id), int(user_id))
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    try:
        member = await ctx.bot.get_chat_member(chat_id, user_id)
    except Exception:
        log.warning("membership check failed chat=%s user=%s", chat_id, user_id)
        if cached is not None and now < cached[0] + FAILURE_GRACE:
            return cached[1]
        _cache.pop(key, None)
        return False
    allowed = status_allows(member)
    if len(_cache) >= MAX_ENTRIES:
        for stale in [k for k, (expires, _) in _cache.items() if now >= expires + FAILURE_GRACE]:
            _cache.pop(stale, None)
        if len(_cache) >= MAX_ENTRIES:
            _cache.clear()
    _cache[key] = (now + CACHE_TTL, allowed)
    return allowed


async def is_member_of_known_chat(ctx: Any, user_id: int) -> bool:
    if is_admin(ctx, user_id):
        return True
    ids = ctx.catalog.list_known_chat_ids()
    for chat_id in ids[:64]:
        if ctx.catalog.is_chat_blacklisted(chat_id):
            continue
        if await _get_chat_member(ctx, chat_id, user_id):
            return True
    return False


def chat_is_known(ctx: Any, chat_id: int) -> bool:
    row = ctx.catalog.get_chat(chat_id)
    return bool(row and row.active)


async def user_is_blocked(ctx: Any, user_id: int | None) -> bool:
    if not user_id:
        return True
    if is_admin(ctx, user_id):
        return False
    return bool(ctx.catalog.is_user_blacklisted(int(user_id)))


async def allow_from_callback(ctx: Any, callback: Any) -> bool:
    user = getattr(callback, "from_user", None)
    if user is None or user_is_bot(user, getattr(ctx, "bot", None)):
        return False
    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None) if message is not None else None
    return await allow_user(ctx, user.id, chat)


async def allow_user(ctx: Any, user_id: int | None, chat: Any | None = None) -> bool:
    if not user_id:
        return False
    user_id = int(user_id)
    if await user_is_blocked(ctx, user_id):
        return False
    chat_id = int(getattr(chat, "id", 0) or 0) if chat is not None else 0
    chat_type = str(getattr(chat, "type", "") or "") if chat is not None else ""
    if chat_id < 0:
        if ctx.catalog.is_chat_blacklisted(chat_id):
            return False
        if not chat_is_known(ctx, chat_id):
            title = str(getattr(chat, "title", None) or getattr(chat, "full_name", None) or "")
            ctx.catalog.upsert_chat(chat_id, type=chat_type, title=title, active=True)
        return True
    # Private DM, or no chat on the event (callbacks / membership alias).
    if chat is None or chat_type == "private" or chat_id > 0:
        if not _dm_requires_known(ctx):
            return True
        return await is_member_of_known_chat(ctx, user_id)
    return False


async def is_forum_member(ctx: Any, user_id: int | None) -> bool:
    """Kept name: user may interact with the bot (DM / callbacks)."""
    return await allow_user(ctx, user_id)


async def require_user(ctx: Any, user_id: int | None, chat: Any | None = None) -> int:
    if not await allow_user(ctx, user_id, chat):
        raise AppError("forbidden")
    return int(user_id or 0)


def touch(ctx: Any, user_id: int | None) -> None:
    if user_id:
        ctx.catalog.touch_user(int(user_id))


def check_search_rate(ctx: Any, user_id: int) -> None:
    check_rate("search", user_id, int(getattr(ctx.settings, "rate_search_per_minute", 8) or 8))


def check_upload_rate(ctx: Any, user_id: int) -> None:
    check_rate("upload", user_id, int(getattr(ctx.settings, "rate_upload_per_minute", 12) or 12))


def check_api_rate(ctx: Any, user_id: int) -> None:
    check_rate("api", user_id, int(getattr(ctx.settings, "rate_api_per_minute", 60) or 60))
