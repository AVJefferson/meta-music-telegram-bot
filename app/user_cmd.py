from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    WebAppInfo,
)

from app.ephemeral import send_private
from app.errors import AppError
from app.membership import allow_user, check_search_rate, is_admin, touch, user_is_bot
from app.models import Ctx
from app.oauth import issue_login_ticket, public_base_url
from app.util import html_esc

log = logging.getLogger(__name__)

# Flip True when a USER Telethon session can talk to HiFiAudioBot again.
SEARCH_ENABLED = False
SEARCH_DISABLED_TEXT = "Song search is temporarily off. Send audio instead."


def _webapp_url(ctx: Ctx, page: str, *, q: str | None = None) -> str | None:
    try:
        base = public_base_url(ctx.settings)
    except AppError:
        return None
    # Telegram WebAppInfo rejects anything but HTTPS, including http://localhost.
    if not base.lower().startswith("https://"):
        return None
    url = f"{base}/app/{page}"
    extra = (q or "").strip()
    if extra:
        return f"{url}?{urlencode({'q': extra})}"
    return url


def _web_or_url_keyboard(
    ctx: Ctx, page: str, url: str | None, label: str, *, q: str | None = None
) -> InlineKeyboardMarkup | None:
    app = _webapp_url(ctx, page, q=q)
    if app:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=app))]]
        )
    if url:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=url)]])
    return None


def mentions_bot(message: Message, me) -> bool:
    username = (getattr(me, "username", None) or "").casefold()
    bot_id = getattr(me, "id", None)
    raw = message.text or ""
    if username and f"@{username}".casefold() in raw.casefold():
        return True
    for entity in message.entities or []:
        if entity.type == "text_mention" and getattr(entity, "user", None) and entity.user.id == bot_id:
            return True
        if entity.type == "mention" and username:
            chunk = raw[entity.offset : entity.offset + entity.length]
            if chunk.casefold() == f"@{username}":
                return True
    return False


def inline_search_results(query: str) -> list[InlineQueryResultArticle]:
    q = " ".join((query or "").split())
    if not q:
        return []
    title = f"Search {q}"
    if len(title) > 64:
        title = title[:61] + "..."
    return [
        InlineQueryResultArticle(
            id=secrets.token_urlsafe(8),
            title=title,
            description="Send search to this chat",
            input_message_content=InputTextMessageContent(message_text=f"/get {q}"[:4096]),
        )
    ]


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
        months = int(getattr(ctx.settings, "user_inactive_months", 3) or 3)
        cmds = ["/start", "/login", "/settings", "/review", "/suggest"]
        if SEARCH_ENABLED:
            if message.chat.type == "private":
                cmds.append("send a song name or audio")
            else:
                cmds.append("/get <song> or @mention the bot")
        else:
            cmds.append("send audio")
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
        months = int(getattr(ctx.settings, "user_inactive_months", 3) or 3)
        kb = _web_or_url_keyboard(ctx, "settings", None, "Open settings")
        text = (
            f"Drive: {html_esc(user.google_email or 'not connected')}\n"
            f"Forgotten after {months} months idle.\n"
            "Listen-for formats and unlink Drive are in the Mini App."
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
        if not SEARCH_ENABLED:
            await _start_search(ctx, message, "")
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
    if not SEARCH_ENABLED:
        await send_private(
            ctx, chat_id=message.chat.id, user_id=user_id, text=SEARCH_DISABLED_TEXT
        )
        return
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
        if not mentions_bot(message, me):
            return
        raw = message.text.strip()
        query = raw
        username = (me.username or "").casefold()
        if username:
            query = raw.replace(f"@{me.username}", "").replace(f"@{username}", "").strip()
        if not query:
            return
        await _start_search(ctx, message, query)

    @router.inline_query()
    async def on_inline(query: InlineQuery, ctx: Ctx) -> None:
        if not query.from_user or user_is_bot(query.from_user, ctx.bot):
            await query.answer(results=[], cache_time=1, is_personal=True)
            return
        if not await allow_user(ctx, query.from_user.id):
            await query.answer(results=[], cache_time=10, is_personal=True)
            return
        if not SEARCH_ENABLED:
            await query.answer(results=[], cache_time=30, is_personal=True)
            return
        results = inline_search_results(query.query or "")
        kwargs: dict = {"results": results, "cache_time": 1, "is_personal": True}
        if not results:
            kwargs["switch_pm_text"] = "Search in private chat"
            kwargs["switch_pm_parameter"] = "search"
        await query.answer(**kwargs)

    return router
