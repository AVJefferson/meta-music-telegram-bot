from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

CACHE_TTL = 60.0
# How long a previously granted answer may be reused when Telegram is unreachable.
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


async def is_forum_member(ctx: Any, user_id: int | None) -> bool:
    """True if the user is currently in the configured forum group.

    Cached briefly because this is called on every private message and every
    button press, and Telegram rate-limits getChatMember.
    """
    if not user_id:
        return False
    chat_id = int(ctx.settings.allowed_chat_id)
    key = (chat_id, int(user_id))
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    try:
        member = await ctx.bot.get_chat_member(chat_id, user_id)
    except Exception:
        log.warning("membership check failed user=%s", user_id)
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
