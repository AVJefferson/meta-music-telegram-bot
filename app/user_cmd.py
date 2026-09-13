from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.ephemeral import send_private
from app.errors import AppError
from app.membership import allow_user, check_search_rate, is_admin, touch, user_is_bot
from app.models import Ctx
from app.oauth import issue_login_ticket, public_base_url
from app.util import html_esc

log = logging.getLogger(__name__)


def _webapp_url(ctx: Ctx, page: str) -> str | None:
    try:
        return f"{public_base_url(ctx.settings)}/app/{page}"
    except AppError:
        return None


def _web_or_url_keyboard(ctx: Ctx, page: str, url: str | None, label: str) -> InlineKeyboardMarkup | None:
    app = _webapp_url(ctx, page)
    if app:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=app))]]
        )
    if url:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])
    return None


def build_user_command_router() -> Router:
    router = Router()

    @router.message(Command("start"))
    async def start(message: Message, ctx: Ctx) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            if message.chat.type == "private":
                await message.reply("Private access requires membership in a known group.")
            return
        user = ctx.catalog.touch_user(message.from_user.id)
        months = int(getattr(ctx.settings, "user_inactive_months", 6) or 6)
        cmds = ["/start", "/login", "/settings", "/review", "/suggest"]
        if message.chat.type == "private":
            cmds.append("send a song name or FLAC")
        else:
            cmds.append("/get <song> or @mention the bot")
        if is_admin(ctx, message.from_user.id):
            cmds.extend(["/listusers", "/listgroups", "/listchannels", "/blockuser"])
        if not user.logged_in:
            cmds = [c for c in cmds if c != "Drive library"]
        text = (
            f"User since {html_esc(user.first_seen_at)}\n"
            f"Songs edited: {user.songs_edited}\n"
            f"Drive: {html_esc(user.google_email or 'not connected')}\n"
            f"Inactive after {months} months.\n\n"
            f"Commands: {html_esc(', '.join(cmds))}"
        )
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=text,
            parse_mode="HTML",
        )

    @router.message(Command("login"))
    async def login(message: Message, ctx: Ctx) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        touch(ctx, message.from_user.id)
        try:
            ticket_url = issue_login_ticket(ctx, message.from_user.id)
        except AppError as exc:
            await send_private(
                ctx, chat_id=message.chat.id, user_id=message.from_user.id, text=exc.user_message
            )
            return
        kb = _web_or_url_keyboard(ctx, "login", ticket_url, "Connect Google Drive")
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text="Connect Google Drive. The link works once and only for you.",
            reply_markup=kb,
        )

    @router.message(Command("settings"))
    async def settings_cmd(message: Message, ctx: Ctx) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        user = ctx.catalog.touch_user(message.from_user.id)
        months = int(getattr(ctx.settings, "user_inactive_months", 6) or 6)
        kb = _web_or_url_keyboard(ctx, "settings", None, "Open settings")
        text = (
            f"Drive: {html_esc(user.google_email or 'not connected')}\n"
            f"Forgotten after {months} months idle.\n"
            "Unlink Drive in the Mini App."
        )
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
        )

    @router.message(Command("get"))
    async def get_cmd(message: Message, ctx: Ctx, command: CommandObject) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        query = (command.args or "").strip()
        if not query:
            await send_private(
                ctx, chat_id=message.chat.id, user_id=message.from_user.id, text="Usage: /get song name"
            )
            return
        await _start_search(ctx, message, query)

    return router


async def _start_search(ctx: Ctx, message: Message, query: str) -> None:
    if not message.from_user or user_is_bot(message.from_user, ctx.bot):
        return
    user_id = message.from_user.id
    touch(ctx, user_id)
    try:
        check_search_rate(ctx, user_id)
    except AppError as exc:
        await send_private(ctx, chat_id=message.chat.id, user_id=user_id, text=exc.user_message)
        return
    from app.hifi import search_songs

    try:
        markup = await search_songs(ctx, user_id, query)
    except AppError as exc:
        await send_private(ctx, chat_id=message.chat.id, user_id=user_id, text=exc.user_message)
        return
    await send_private(
        ctx,
        chat_id=message.chat.id,
        user_id=user_id,
        text=f"Results for {html_esc(query)}",
        parse_mode="HTML",
        reply_markup=markup,
    )


def build_search_text_router() -> Router:
    router = Router()

    @router.message(F.chat.type == "private", F.text)
    async def dm_text(message: Message, ctx: Ctx) -> None:
        if not message.from_user or not message.text or user_is_bot(message.from_user, ctx.bot):
            return
        text = message.text.strip()
        if text.startswith("/"):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        pending = ctx.catalog.get_waiting_for_chat(message.chat.id)
        if pending and str(pending.phase).startswith("edit"):
            return
        await _start_search(ctx, message, text)

    @router.message(F.text)
    async def group_mention(message: Message, ctx: Ctx) -> None:
        if not message.from_user or not message.text or user_is_bot(message.from_user, ctx.bot):
            return
        if message.chat.type == "private":
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        me = await ctx.bot.get_me()
        username = (me.username or "").casefold()
        raw = message.text.strip()
        mentioned = False
        if username and f"@{username}".casefold() in raw.casefold():
            mentioned = True
        for entity in message.entities or []:
            if entity.type == "mention":
                mentioned = True
            if entity.type == "text_mention" and getattr(entity, "user", None) and entity.user.id == me.id:
                mentioned = True
        if not mentioned:
            return
        query = raw
        if username:
            query = raw.replace(f"@{me.username}", "").replace(f"@{username}", "").strip()
        if not query:
            return
        await _start_search(ctx, message, query)

    return router
