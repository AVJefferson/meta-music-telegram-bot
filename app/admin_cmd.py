from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.ephemeral import send_private
from app.membership import is_admin, touch
from app.models import Ctx
from app.util import html_esc

log = logging.getLogger(__name__)


def _since_iso(ctx: Ctx) -> str:
    months = int(getattr(ctx.settings, "user_inactive_months", 6) or 6)
    return (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat(timespec="seconds")


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


def build_admin_router() -> Router:
    router = Router()

    async def _require_admin(message: Message, ctx: Ctx) -> bool:
        return bool(message.from_user and is_admin(ctx, message.from_user.id))

    @router.message(Command("listusers"))
    async def listusers(message: Message, ctx: Ctx) -> None:
        if not await _require_admin(message, ctx):
            return
        touch(ctx, message.from_user.id)
        rows = ctx.catalog.list_active_users(_since_iso(ctx))
        lines = ["<b>Active users</b>"]
        for user in rows[:40]:
            lines.append(
                f"<code>{user.telegram_user_id}</code> since {html_esc(format_since_date(user.first_seen_at))}"
            )
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text="\n".join(lines) if len(lines) > 1 else "No active users.",
            parse_mode="HTML",
            reply_markup=_admin_kb_users(ctx, rows),
        )

    @router.message(Command("listgroups"))
    async def listgroups(message: Message, ctx: Ctx) -> None:
        if not await _require_admin(message, ctx):
            return
        touch(ctx, message.from_user.id)
        chats = [
            c
            for c in ctx.catalog.list_chats()
            if c.type in {"group", "supergroup"} or (c.chat_id < 0 and c.type != "channel")
        ]
        text = await _format_chats(ctx, chats, "Groups")
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=text,
            parse_mode="HTML",
            reply_markup=_admin_kb_chats(chats, blocked_fn=ctx.catalog.is_chat_blacklisted, prefix="g"),
        )

    @router.message(Command("listchannels"))
    async def listchannels(message: Message, ctx: Ctx) -> None:
        if not await _require_admin(message, ctx):
            return
        touch(ctx, message.from_user.id)
        chats = [c for c in ctx.catalog.list_chats() if c.type == "channel"]
        text = await _format_chats(ctx, chats, "Channels")
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=text,
            parse_mode="HTML",
            reply_markup=_admin_kb_chats(chats, blocked_fn=ctx.catalog.is_chat_blacklisted, prefix="c"),
        )

    @router.message(Command("blockuser"))
    async def blockuser(message: Message, ctx: Ctx, command: CommandObject) -> None:
        if not await _require_admin(message, ctx):
            return
        target = _target_user(message, command)
        if target is None:
            await send_private(
                ctx,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                text="Tag a user or reply to them: /blockuser @user",
            )
            return
        if is_admin(ctx, target):
            await send_private(
                ctx, chat_id=message.chat.id, user_id=message.from_user.id, text="Cannot block self."
            )
            return
        ctx.catalog.blacklist_user(target)
        await send_private(
            ctx, chat_id=message.chat.id, user_id=message.from_user.id, text=f"Blocked {target}."
        )

    @router.message(Command("unblockuser"))
    async def unblockuser(message: Message, ctx: Ctx, command: CommandObject) -> None:
        if not await _require_admin(message, ctx):
            return
        target = _target_user(message, command)
        if target is None:
            await send_private(
                ctx,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                text="Tag a user or reply: /unblockuser @user",
            )
            return
        ctx.catalog.unblacklist_user(target)
        await send_private(
            ctx, chat_id=message.chat.id, user_id=message.from_user.id, text=f"Unblocked {target}."
        )

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
        elif action == "uu":
            ctx.catalog.unblacklist_user(target)
            await callback.answer("unblocked")
        elif action in {"bg", "bc"}:
            ctx.catalog.blacklist_chat(target)
            await callback.answer("chat blocked")
        elif action in {"ug", "uc"}:
            ctx.catalog.unblacklist_chat(target)
            await callback.answer("chat unblocked")
        else:
            await callback.answer()

    return router


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


async def _format_chats(ctx: Ctx, chats, heading: str) -> str:
    lines = [f"<b>{html_esc(heading)}</b>"]
    for chat in chats[:40]:
        admins = ""
        try:
            members = await ctx.bot.get_chat_administrators(chat.chat_id)
            ids = [str(m.user.id) for m in members if getattr(m, "user", None) and not m.user.is_bot]
            admins = ",".join(ids[:8])
        except Exception:
            admins = "?"
        mark = " blocked" if ctx.catalog.is_chat_blacklisted(chat.chat_id) else ""
        lines.append(
            f"<code>{chat.chat_id}</code> {html_esc(chat.title or '')} admins={html_esc(admins)}{mark}"
        )
    if len(lines) == 1:
        lines.append("None.")
    return "\n".join(lines)
