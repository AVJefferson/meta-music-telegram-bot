from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.ephemeral import send_private
from app.membership import is_admin, touch
from app.models import Ctx, user_display_name
from app.util import html_esc

log = logging.getLogger(__name__)


def _since_iso(ctx: Ctx) -> str:
    months = int(getattr(ctx.settings, "user_inactive_months", 3) or 3)
    return (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat(timespec="seconds")


def _handle(value: object) -> str:
    return str(value or "").strip().lstrip("@")


def format_user_line(user) -> str:
    bits = [f"<code>{user.telegram_user_id}</code>"]
    username = _handle(getattr(user, "username", None))
    if username:
        bits.append(f"@{html_esc(username)}")
    name = user_display_name(getattr(user, "first_name", None), getattr(user, "last_name", None))
    if name:
        bits.append(html_esc(name))
    bits.append(f"since {html_esc(format_since_date(user.first_seen_at))}")
    return " ".join(bits)


def format_chat_label(chat) -> str:
    title = html_esc(getattr(chat, "title", None) or "")
    username = _handle(getattr(chat, "username", None))
    if username:
        handle = f"@{html_esc(username)}"
        return f"{title} {handle}".strip()
    return title


def format_since_date(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "?"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10]
    return dt.strftime("%y-%m-%d")


def _admin_kb_users(ctx: Ctx, rows) -> InlineKeyboardMarkup:
    admin_id = int(ctx.settings.admin_telegram_user_id)
    buttons: list[list[InlineKeyboardButton]] = []
    for user in rows[:30]:
        uid = user.telegram_user_id
        blocked = ctx.catalog.is_user_blacklisted(uid)
        if uid == admin_id:
            label = f"{uid} (admin)"
            buttons.append([InlineKeyboardButton(text=label, callback_data="ad:noop")])
            continue
        action = "uu" if blocked else "bu"
        verb = "Unblock" if blocked else "Block"
        buttons.append(
            [InlineKeyboardButton(text=f"{verb} {uid}", callback_data=f"ad:{action}:{uid}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons or [[InlineKeyboardButton(text="none", callback_data="ad:noop")]])


def _admin_kb_chats(rows, *, blocked_fn, prefix: str) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for chat in rows[:30]:
        cid = chat.chat_id
        blocked = blocked_fn(cid)
        action = f"u{prefix}" if blocked else f"b{prefix}"
        verb = "Unblock" if blocked else "Block"
        buttons.append(
            [InlineKeyboardButton(text=f"{verb} {cid}", callback_data=f"ad:{action}:{cid}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons or [[InlineKeyboardButton(text="none", callback_data="ad:noop")]])


_ADMIN_PAGES: dict[str, dict] = {}
_PAGE_SIZE = 8


def _ttl(ctx: Ctx) -> int:
    return int(getattr(ctx.settings, "pagination_ttl_seconds", 300) or 300)


def _remember(ctx: Ctx, kind: str) -> int:
    key = {
        "users": "max_recent_users_to_remember",
        "groups": "max_recent_groups_to_remember",
        "channels": "max_recent_channels_to_remember",
    }[kind]
    return max(1, int(getattr(ctx.settings, key, 8) or 8))


def _store_ids(ctx: Ctx, kind: str, ids: list[int]) -> str:
    token = secrets.token_urlsafe(6)
    _ADMIN_PAGES[token] = {"kind": kind, "ids": ids, "exp": time.time() + _ttl(ctx)}
    return token


def _page_keyboard(ctx: Ctx, kind: str, ids: list[int], page: int, label_fn) -> InlineKeyboardMarkup:
    token = _store_ids(ctx, kind, ids)
    pages = max(1, (len(ids) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _PAGE_SIZE
    rows = []
    for item_id in ids[start : start + _PAGE_SIZE]:
        code = _toggle_code(ctx, kind, item_id)
        rows.append(
            [
                InlineKeyboardButton(
                    text=label_fn(item_id)[:64],
                    callback_data=f"ad:{code}:{item_id}",
                )
            ]
        )
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"ad:pg:{token}:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"ad:pg:{token}:{page + 1}"))
    if nav:
        rows.append(nav)
    if not rows:
        rows = [[InlineKeyboardButton(text="none", callback_data="ad:noop")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _toggle_code(ctx: Ctx, kind: str, item_id: int) -> str:
    if kind == "users":
        return "uu" if ctx.catalog.is_user_blacklisted(item_id) else "bu"
    blocked = ctx.catalog.is_chat_blacklisted(item_id)
    if kind == "groups":
        return "ug" if blocked else "bg"
    return "uc" if blocked else "bc"


def build_admin_router() -> Router:
    router = Router()

    async def _require_admin(message: Message, ctx: Ctx) -> bool:
        return bool(message.from_user and is_admin(ctx, message.from_user.id))

    @router.message(Command("listusers"))
    async def listusers(message: Message, ctx: Ctx) -> None:
        if not await _require_admin(message, ctx):
            return
        touch(ctx, message.from_user.id, message.from_user)
        admin_id = int(ctx.settings.admin_telegram_user_id)
        rows = [user for user in ctx.catalog.list_active_users(_since_iso(ctx)) if user.telegram_user_id != admin_id]
        recent = rows[: _remember(ctx, "users")]
        lines = [f"Users: {len(rows)}", "", "Recent Users:"]
        for user in recent:
            handle = f"@{_handle(user.username)}" if _handle(user.username) else str(user.telegram_user_id)
            lines.append(f"{html_esc(handle)} ({user.telegram_user_id})")
        text = "\n".join(lines) if rows else "Users: 0"
        ids = [user.telegram_user_id for user in rows]

        def _label(uid: int) -> str:
            user = next((item for item in rows if item.telegram_user_id == uid), None)
            name = f"@{_handle(user.username)}" if user and _handle(getattr(user, "username", None)) else str(uid)
            verb = "Unblock" if ctx.catalog.is_user_blacklisted(uid) else "Block"
            return f"{verb} {name} ({uid})"

        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=text,
            parse_mode="HTML",
            reply_markup=_page_keyboard(ctx, "users", ids, 0, _label) if ids else None,
        )

    @router.message(Command("listgroups"))
    async def listgroups(message: Message, ctx: Ctx) -> None:
        if not await _require_admin(message, ctx):
            return
        touch(ctx, message.from_user.id, message.from_user)
        chats = [
            c
            for c in ctx.catalog.list_chats()
            if c.type in {"group", "supergroup"} or (c.chat_id < 0 and c.type != "channel")
        ]
        chats.sort(key=lambda chat: chat.last_active_at or "", reverse=True)
        await _send_chat_list(message, ctx, chats, "Groups", "groups")

    @router.message(Command("listchannels"))
    async def listchannels(message: Message, ctx: Ctx) -> None:
        if not await _require_admin(message, ctx):
            return
        touch(ctx, message.from_user.id, message.from_user)
        chats = [c for c in ctx.catalog.list_chats() if c.type == "channel"]
        chats.sort(key=lambda chat: chat.last_active_at or "", reverse=True)
        await _send_chat_list(message, ctx, chats, "Channels", "channels")

    @router.message(Command("block", "blockuser"))
    async def blockuser(message: Message, ctx: Ctx, command: CommandObject) -> None:
        if not await _require_admin(message, ctx):
            return
        await _apply_access(message, ctx, command, unblock=False)

    @router.message(Command("unblock", "unblockuser"))
    async def unblockuser(message: Message, ctx: Ctx, command: CommandObject) -> None:
        if not await _require_admin(message, ctx):
            return
        await _apply_access(message, ctx, command, unblock=True)

    @router.callback_query(F.data.startswith("ad:"))
    async def admin_cb(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.from_user or not is_admin(ctx, callback.from_user.id):
            await callback.answer("Not allowed.", show_alert=True)
            return
        data = callback.data or ""
        parts = data.split(":")
        if len(parts) < 2:
            await callback.answer()
            return
        action = parts[1]
        if action == "pg":
            await _admin_page(callback, ctx, parts)
            return
        if action == "noop":
            await callback.answer()
            return
        if len(parts) < 3:
            await callback.answer()
            return
        try:
            target = int(parts[2])
        except ValueError:
            await callback.answer("bad id", show_alert=True)
            return
        if action == "bu":
            if is_admin(ctx, target):
                await callback.answer("Cannot block self.", show_alert=True)
                return
            ctx.catalog.blacklist_user(target)
            await callback.answer("blocked")
            from app.notify import send_notice

            await send_notice(ctx, target, "You have been blocked by the admin", level="info")
        elif action == "uu":
            ctx.catalog.unblacklist_user(target)
            await callback.answer("unblocked")
            from app.notify import send_notice

            await send_notice(ctx, target, "You have been unblocked by the admin", level="info")
        elif action in {"bg", "bc"}:
            ctx.catalog.blacklist_chat(target)
            await callback.answer("chat blocked")
        elif action in {"ug", "uc"}:
            ctx.catalog.unblacklist_chat(target)
            await callback.answer("chat unblocked")
        else:
            await callback.answer()

    return router


async def _send_chat_list(message: Message, ctx: Ctx, chats, heading: str, kind: str) -> None:
    refreshed = [await _refresh_blank_chat(ctx, chat) for chat in chats]
    chats = refreshed
    recent = chats[: _remember(ctx, kind)]
    lines = [f"{heading}: {len(chats)}", "", f"Recent {heading}:"]
    for chat in recent:
        handle = f"@{_handle(chat.username)}" if _handle(getattr(chat, "username", None)) else ""
        title = (chat.title or "").strip()
        label = " ".join(part for part in (title, handle) if part) or str(chat.chat_id)
        lines.append(f"{html_esc(label)} ({chat.chat_id})")
    text = "\n".join(lines) if chats else f"{heading}: 0"

    def _label(cid: int) -> str:
        chat = next((item for item in chats if item.chat_id == cid), None)
        name = f"@{_handle(chat.username)}" if chat and _handle(getattr(chat, "username", None)) else str(cid)
        verb = "Unblock" if ctx.catalog.is_chat_blacklisted(cid) else "Block"
        return f"{verb} {name} ({cid})"

    ids = [chat.chat_id for chat in chats]
    await send_private(
        ctx,
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        text=text,
        parse_mode="HTML",
        reply_markup=_page_keyboard(ctx, kind, ids, 0, _label) if ids else None,
    )


async def _admin_page(callback: CallbackQuery, ctx: Ctx, parts: list[str]) -> None:
    if len(parts) < 4:
        await callback.answer()
        return
    token, page_s = parts[2], parts[3]
    row = _ADMIN_PAGES.get(token)
    now = time.time()
    if not row or row.get("exp", 0) <= now:
        _ADMIN_PAGES.pop(token, None)
        await callback.answer("Expired.", show_alert=True)
        return
    try:
        page = int(page_s)
    except ValueError:
        await callback.answer()
        return
    kind = str(row.get("kind") or "")
    ids = [int(item) for item in row.get("ids") or []]

    def _label(item_id: int) -> str:
        return str(item_id)

    markup = _page_keyboard(ctx, kind, ids, page, _label)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            log.debug("admin page edit failed", exc_info=True)
    await callback.answer()


def _tokens(command: CommandObject) -> list[str]:
    return [part for part in (command.args or "").split() if part.strip()]


def _lookup_user(ctx: Ctx, token: str) -> int | None:
    text = token.strip()
    if text.lstrip("-").isdigit() and not text.startswith("-"):
        return int(text)
    if text.startswith("@"):
        user = ctx.catalog.find_user_by_username(text)
        return user.telegram_user_id if user else None
    return None


def _lookup_chat(ctx: Ctx, token: str) -> int | None:
    text = token.strip()
    if text.lstrip("-").isdigit() and (text.startswith("-") or int(text) < 0):
        return int(text)
    if text.startswith("@"):
        chat = ctx.catalog.find_chat_by_username(text)
        return chat.chat_id if chat else None
    return None


async def _apply_access(message: Message, ctx: Ctx, command: CommandObject, *, unblock: bool) -> None:
    from app.notify import send_notice

    tokens = _tokens(command)
    chat_type = message.chat.type
    in_room = chat_type in {"group", "supergroup", "channel"}
    user_id = None
    chat_id = None
    scoped = False
    if not tokens and in_room:
        chat_id = message.chat.id
    elif len(tokens) == 1 and in_room:
        user_id = _lookup_user(ctx, tokens[0])
        if user_id is not None:
            chat_id = message.chat.id
            scoped = True
        else:
            chat_id = _lookup_chat(ctx, tokens[0])
    elif len(tokens) == 1:
        user_id = _lookup_user(ctx, tokens[0])
        if user_id is None:
            chat_id = _lookup_chat(ctx, tokens[0])
    elif len(tokens) >= 2:
        chat_id = _lookup_chat(ctx, tokens[0])
        user_id = _lookup_user(ctx, tokens[1])
        scoped = user_id is not None and chat_id is not None
    if user_id is None and chat_id is None:
        await send_notice(
            ctx,
            message.from_user.id,
            "User or group or channel not found",
            level="error",
            chat_id=message.chat.id,
        )
        return
    if user_id is not None and is_admin(ctx, user_id):
        await send_private(
            ctx, chat_id=message.chat.id, user_id=message.from_user.id, text="Cannot block self."
        )
        return
    verb = "Unblocked" if unblock else "Blocked"
    already = "already unblocked" if unblock else "already blocked"
    if scoped and user_id is not None and chat_id is not None:
        if ctx.catalog.is_user_blacklisted(user_id):
            note = (
                "User is blocked, cannot unblock for only a particular channel/group"
                if unblock
                else "User is already blocked, cannot block for only a particular channel/group"
            )
            await send_notice(ctx, message.from_user.id, note, level="error", chat_id=message.chat.id)
            return
        exists = ctx.catalog.is_user_blocked_in_chat(user_id, chat_id)
        if unblock:
            if not exists:
                await send_notice(
                    ctx,
                    message.from_user.id,
                    "User or group or channel is already unblocked",
                    level="info",
                    chat_id=message.chat.id,
                )
                return
            ctx.catalog.unblock_user_in_chat(user_id, chat_id)
        else:
            if exists:
                await send_notice(
                    ctx,
                    message.from_user.id,
                    "User or group or channel is already blocked",
                    level="info",
                    chat_id=message.chat.id,
                )
                return
            ctx.catalog.block_user_in_chat(user_id, chat_id)
        label = _user_label(ctx, user_id)
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=f"{verb} {label}",
        )
        await send_notice(
            ctx,
            user_id,
            "You have been unblocked by the admin" if unblock else "You have been blocked by the admin",
            level="info",
        )
        return
    if user_id is not None:
        exists = ctx.catalog.is_user_blacklisted(user_id)
        if exists == (not unblock):
            await send_notice(
                ctx,
                message.from_user.id,
                f"User or group or channel is {already}",
                level="info",
                chat_id=message.chat.id,
            )
            return
        if unblock:
            ctx.catalog.unblacklist_user(user_id)
        else:
            ctx.catalog.blacklist_user(user_id)
        label = _user_label(ctx, user_id)
        await send_private(
            ctx, chat_id=message.chat.id, user_id=message.from_user.id, text=f"{verb} {label}"
        )
        await send_notice(
            ctx,
            user_id,
            "You have been unblocked by the admin" if unblock else "You have been blocked by the admin",
            level="info",
        )
        return
    if chat_id is None:
        await send_notice(
            ctx,
            message.from_user.id,
            "User or group or channel not found",
            level="error",
            chat_id=message.chat.id,
        )
        return
    exists = ctx.catalog.is_chat_blacklisted(chat_id)
    if exists == (not unblock):
        await send_notice(
            ctx,
            message.from_user.id,
            f"User or group or channel is {already}",
            level="info",
            chat_id=message.chat.id,
        )
        return
    if unblock:
        ctx.catalog.unblacklist_chat(chat_id)
    else:
        ctx.catalog.blacklist_chat(chat_id)
    label = _chat_label(ctx, chat_id)
    await send_private(
        ctx, chat_id=message.chat.id, user_id=message.from_user.id, text=f"{verb} {label}"
    )


def _user_label(ctx: Ctx, user_id: int) -> str:
    user = ctx.catalog.get_user(user_id)
    handle = _handle(getattr(user, "username", None)) if user else ""
    if handle:
        return f"@{handle} ({user_id})"
    return str(user_id)


def _chat_label(ctx: Ctx, chat_id: int) -> str:
    chat = ctx.catalog.get_chat(chat_id)
    handle = _handle(getattr(chat, "username", None)) if chat else ""
    if handle:
        return f"@{handle} ({chat_id})"
    title = getattr(chat, "title", None) if chat else None
    if title:
        return f"{title} ({chat_id})"
    return str(chat_id)


def _target_user(message: Message, command: CommandObject) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return int(message.reply_to_message.from_user.id)
    for entity in message.entities or []:
        user = getattr(entity, "user", None)
        if user:
            return int(user.id)
    args = (command.args or "").strip()
    if args.lstrip("-").isdigit():
        return int(args)
    return None


def _remote_type(remote) -> str:
    raw = getattr(remote, "type", None)
    return str(getattr(raw, "value", raw) or "")


async def _refresh_blank_chat(ctx: Ctx, chat):
    if (chat.title or "").strip() or (getattr(chat, "username", None) or "").strip():
        return chat
    try:
        remote = await ctx.bot.get_chat(chat.chat_id)
    except Exception:
        log.debug("get_chat failed chat=%s", chat.chat_id, exc_info=True)
        return chat
    title = str(getattr(remote, "title", None) or getattr(remote, "full_name", None) or "")
    username = str(getattr(remote, "username", None) or "")
    chat_type = _remote_type(remote)
    if not title and not username and not chat_type:
        return chat
    ctx.catalog.upsert_chat(
        chat.chat_id,
        type=chat_type or chat.type,
        title=title,
        username=username,
        active=chat.active,
    )
    return ctx.catalog.get_chat(chat.chat_id) or chat


async def _format_chats(ctx: Ctx, chats, heading: str) -> str:
    lines = [f"<b>{html_esc(heading)}</b>"]
    shown = [await _refresh_blank_chat(ctx, chat) for chat in chats[:40]]
    for chat in shown:
        admins = ""
        try:
            members = await ctx.bot.get_chat_administrators(chat.chat_id)
            ids = [str(m.user.id) for m in members if getattr(m, "user", None) and not m.user.is_bot]
            admins = ",".join(ids[:8])
        except Exception:
            admins = "?"
        mark = " blocked" if ctx.catalog.is_chat_blacklisted(chat.chat_id) else ""
        label = format_chat_label(chat)
        body = f" {label}" if label else ""
        lines.append(f"<code>{chat.chat_id}</code>{body} admins={html_esc(admins)}{mark}")
    if len(lines) == 1:
        lines.append("None.")
    return "\n".join(lines)
