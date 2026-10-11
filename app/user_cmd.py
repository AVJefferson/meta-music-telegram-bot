from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
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
from app.membership import allow_user, check_search_rate, is_admin, profile_fields, touch, user_is_bot
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


def inactive_days(ctx: Ctx) -> int:
    days = int(getattr(ctx.settings, "user_inactive_days", 0) or 0)
    if days > 0:
        return days
    months = int(getattr(ctx.settings, "user_inactive_months", 3) or 3)
    return months * 30


def format_since(value: str) -> str:
    raw = (value or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10] or "?"
    return dt.strftime("%Y %b %d")


def format_start_text(user, *, username: str, prefs_logged_in: bool, admin: bool, days: int) -> str:
    handle = username or str(user.telegram_user_id)
    email = user.google_email or ""
    login = "<code>/login</code> to upload files to your drive."
    if prefs_logged_in and email:
        drive = html_esc(email)
    elif email:
        drive = f"{html_esc(email)} | {login}"
    else:
        drive = login
    cmds = ["/start", "/settings", "/get", "/hifi", "/review", "/suggest", "/cancel"]
    if admin:
        cmds.extend(["/listusers", "/listgroups", "/listchannels", "/block", "/unblock"])
    return (
        f"Hi @{html_esc(handle)}, welcome to the MetaMusic bot!\n"
        f"User Since {html_esc(format_since(user.first_seen_at))}\n"
        f"Songs Processed: {int(getattr(user, 'songs_processed', 0) or 0)} "
        f"Songs Edited: {user.songs_edited}\n"
        f"Drive: {drive}\n"
        f"Commands: {html_esc(', '.join(cmds))}, or send audio file to process.\n\n"
        f"<b>Your account will be deleted if inactive for {days} days</b>"
    )


async def _blocked(message: Message, ctx: Ctx) -> bool:
    from app.audio_in import BLOCKED_TEXT
    from app.membership import user_is_blocked

    user = message.from_user
    if user is None:
        return False
    scoped_fn = getattr(ctx.catalog, "is_user_blocked_in_chat", None)
    scoped = bool(
        message.chat.id < 0 and callable(scoped_fn) and scoped_fn(user.id, message.chat.id)
    )
    if await user_is_blocked(ctx, user.id) or scoped:
        from app.ephemeral import send_private

        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=user.id,
            text=BLOCKED_TEXT,
        )
        return True
    return False


def build_user_command_router() -> Router:
    router = Router()

    @router.message(Command("start", "stats"))
    async def start(message: Message, ctx: Ctx) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if await _blocked(message, ctx):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            if message.chat.type == "private":
                await message.reply("Private access requires membership in a known group.")
            return
        user = ctx.catalog.touch_user(message.from_user.id, **profile_fields(message.from_user))
        text = format_start_text(
            user,
            username=(message.from_user.username or message.from_user.first_name or ""),
            prefs_logged_in=user.logged_in,
            admin=is_admin(ctx, message.from_user.id),
            days=inactive_days(ctx),
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
        if await _blocked(message, ctx):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        touch(ctx, message.from_user.id, message.from_user)
        user = ctx.catalog.get_user(message.from_user.id)
        state = await _drive_state(ctx, user)
        if state == "valid":
            await send_private(
                ctx,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                text="Google Drive already connected.",
            )
            return
        try:
            ticket_url = issue_login_ticket(ctx, message.from_user.id)
        except AppError as exc:
            await send_private(
                ctx, chat_id=message.chat.id, user_id=message.from_user.id, text=exc.user_message
            )
            return
        kb = _web_or_url_keyboard(ctx, "login", ticket_url, "Connect Google Drive")
        text = (
            "Google Drive connection expired. Please connect again."
            if state == "expired"
            else "Connect to Google Drive. This bot can see only the files you share with it."
        )
        await send_private(
            ctx,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            text=text,
            reply_markup=kb,
        )

    @router.message(Command("settings"))
    async def settings_cmd(message: Message, ctx: Ctx) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if await _blocked(message, ctx):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        user = ctx.catalog.touch_user(message.from_user.id, **profile_fields(message.from_user))
        from app.prefs import format_settings_text, process_prefs_from_user

        kb = _web_or_url_keyboard(ctx, "settings", None, "Edit Settings in Mini App")
        text = format_settings_text(
            email=user.google_email,
            logged_in=user.logged_in,
            prefs=process_prefs_from_user(user),
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
        if await _blocked(message, ctx):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        query = (command.args or "").strip()
        if not query:
            from app.notify import send_notice

            await send_notice(
                ctx,
                message.from_user.id,
                "Please provide a valid search query",
                level="error",
                chat_id=message.chat.id,
            )
            return
        await _start_get(ctx, message, query)

    @router.message(Command("hifi"))
    async def hifi_cmd(message: Message, ctx: Ctx, command: CommandObject) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if await _blocked(message, ctx):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        query = (command.args or "").strip()
        if not query:
            from app.notify import send_notice

            await send_notice(
                ctx,
                message.from_user.id,
                "Please provide a valid search query",
                level="error",
                chat_id=message.chat.id,
            )
            return
        await _start_search(ctx, message, query)

    @router.message(Command("cancel"))
    async def cancel_cmd(message: Message, ctx: Ctx) -> None:
        if not message.from_user or user_is_bot(message.from_user, ctx.bot):
            return
        if await _blocked(message, ctx):
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        await _cancel_user(ctx, message)

    @router.callback_query(F.data.startswith("gf:") | F.data.startswith("gd:") | F.data.startswith("gc:"))
    async def get_file(callback, ctx: Ctx) -> None:
        await _get_file_callback(callback, ctx)

    @router.callback_query(F.data.startswith("gp:"))
    async def get_page(callback, ctx: Ctx) -> None:
        await _get_page_callback(callback, ctx)

    return router


async def _start_search(ctx: Ctx, message: Message, query: str) -> None:
    if not message.from_user or user_is_bot(message.from_user, ctx.bot):
        return
    user_id = message.from_user.id
    if not SEARCH_ENABLED:
        from app.notify import send_notice

        await send_notice(
            ctx,
            user_id,
            "@HiFiAudioBot has not been enabled by admin",
            level="error",
            chat_id=message.chat.id,
        )
        return
    touch(ctx, user_id, message.from_user)
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
    rows = getattr(markup, "inline_keyboard", None) or []
    count = max(0, len(rows) - 1) if markup is not None else 0
    await send_private(
        ctx,
        chat_id=message.chat.id,
        user_id=user_id,
        text=f"Found {count} songs in @HiFiAudioBot.",
        parse_mode="HTML",
        reply_markup=markup if count else None,
    )


_GET_PAGES: dict[str, dict] = {}


def _ttl(ctx: Ctx) -> int:
    return int(getattr(ctx.settings, "pagination_ttl_seconds", 300) or 300)


def _prune_pages(ctx: Ctx) -> None:
    now = time.time()
    dead = [key for key, row in _GET_PAGES.items() if row.get("exp", 0) <= now]
    for key in dead:
        _GET_PAGES.pop(key, None)
    del ctx


async def _drive_state(ctx: Ctx, user) -> str:
    if user is None or not user.logged_in:
        return "missing"
    from app.drive import DriveClient

    try:
        await asyncio.to_thread(DriveClient.for_user, ctx.settings, ctx.catalog, user.telegram_user_id)
    except AppError:
        return "expired"
    except Exception:
        return "expired"
    return "valid"


def _get_keyboard(
    ctx: Ctx, items: list[tuple[str, str]], page: int, *, user_id: int
) -> InlineKeyboardMarkup | None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if not items:
        return None
    page_size = 8
    token = secrets.token_urlsafe(6)
    _GET_PAGES[token] = {
        "items": items,
        "exp": time.time() + _ttl(ctx),
        "user_id": user_id,
    }
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    rows = [
        [InlineKeyboardButton(text=label[:64], callback_data=data)]
        for label, data in items[start : start + page_size]
    ]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"gp:{token}:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"gp:{token}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _track_get_item(track) -> tuple[str, str]:
    label = f"{track.artist or ''} - {track.title or track.file_name or track.id}".strip(" -")
    return label or str(track.id), f"gf:{track.id}"


async def _drive_name_hits(ctx: Ctx, user_id: int, query: str) -> list:
    from app.drive import DriveClient

    def _search():
        client = DriveClient.for_user(ctx.settings, ctx.catalog, user_id)
        if client is None:
            return []
        return client.search_audio_names(query)

    return await asyncio.to_thread(_search)


async def _collect_get_items(ctx: Ctx, user_id: int, query: str, chat_id: int) -> list[tuple[str, str]]:
    from app.notify import send_notice

    tracks = ctx.catalog.search_user_tracks(user_id, query)
    items = [_track_get_item(track) for track in tracks]
    seen_drive = {str(track.drive_file_id) for track in tracks if track.drive_file_id}
    user = ctx.catalog.get_user(user_id)
    if user and user.logged_in:
        try:
            hits = await _drive_name_hits(ctx, user_id, query)
        except AppError:
            ticket = None
            try:
                ticket = issue_login_ticket(ctx, user_id)
            except AppError:
                ticket = None
            await send_notice(
                ctx,
                user_id,
                "Google Drive connection expired. Please connect again.",
                level="error",
                chat_id=chat_id,
                reply_markup=_web_or_url_keyboard(ctx, "login", ticket, "Connect Google Drive"),
            )
            hits = []
        for child in hits:
            file_id = str(getattr(child, "file_id", "") or "")
            if not file_id or file_id in seen_drive:
                continue
            data = f"gd:{file_id}"
            if len(data.encode()) > 64:
                continue
            seen_drive.add(file_id)
            items.append((str(getattr(child, "name", "") or file_id), data))
        return items
    for sha, path in ctx.catalog.search_cached_files(query):
        items.append((Path(path).name, f"gc:{sha[:16]}"))
    return items


async def _start_get(ctx: Ctx, message: Message, query: str) -> None:
    user_id = message.from_user.id
    touch(ctx, user_id, message.from_user)
    items = await _collect_get_items(ctx, user_id, query, message.chat.id)
    text = f"Files Found: {len(items)}"
    markup = _get_keyboard(ctx, items, 0, user_id=user_id) if items else None
    await send_private(
        ctx,
        chat_id=message.chat.id,
        user_id=user_id,
        text=text,
        reply_markup=markup,
    )


async def _placeholder_alive(bot, chat_id: int, message_id: int) -> bool:
    from app.audio_in import source_deleted

    return not await source_deleted(bot, chat_id, message_id)


async def _drive_expired(ctx: Ctx, user_id: int, chat_id: int) -> bool:
    from app.notify import send_notice

    user = ctx.catalog.get_user(user_id)
    if not user or not user.logged_in:
        return False
    if await _drive_state(ctx, user) != "expired":
        return False
    ticket = None
    try:
        ticket = issue_login_ticket(ctx, user_id)
    except AppError:
        ticket = None
    await send_notice(
        ctx,
        user_id,
        "Google Drive connection expired. Please connect again.",
        level="error",
        chat_id=chat_id,
        reply_markup=_web_or_url_keyboard(ctx, "login", ticket, "Connect Google Drive"),
    )
    return True


class DownloadCancelled(Exception):
    pass


async def _download_drive_file(
    ctx: Ctx,
    user_id: int,
    file_id: str,
    chat_id: int,
    *,
    alive=None,
    on_update=None,
) -> Path | None:
    import threading

    from app.audio_in import progress_due
    from app.drive import DriveClient, DriveDownloadStopped

    dest = ctx.settings.tmp_root / f"get-{secrets.token_hex(4)}-{file_id[-8:]}"
    stop = threading.Event()
    progress = {"pct": 0, "shown": 0, "at": time.time()}

    def _note(pct: int) -> None:
        progress["pct"] = max(0, min(100, int(pct)))

    def _pull() -> Path:
        client = DriveClient.for_user(ctx.settings, ctx.catalog, user_id)
        if client is None:
            raise AppError("needs_login")
        return client.download_to(file_id, dest, stop=stop, on_progress=_note)

    task = asyncio.create_task(asyncio.to_thread(_pull))
    try:
        while not task.done():
            if alive is not None and not await alive():
                stop.set()
                break
            pct = int(progress["pct"])
            if on_update is not None and progress_due(time.time() - progress["at"], pct - progress["shown"]):
                progress["at"] = time.time()
                progress["shown"] = pct
                with contextlib.suppress(Exception):
                    await on_update(pct)
            await asyncio.wait({task}, timeout=1)
        if stop.is_set():
            with contextlib.suppress(Exception):
                await task
            dest.unlink(missing_ok=True)
            raise DownloadCancelled()
        return await task
    except DownloadCancelled:
        raise
    except DriveDownloadStopped:
        dest.unlink(missing_ok=True)
        raise DownloadCancelled() from None
    except AppError:
        await _drive_expired(ctx, user_id, chat_id)
        return None
    except Exception:
        log.exception("drive get failed id=%s", file_id)
        return None


async def _resolve_get_target(ctx: Ctx, user_id: int, kind: str, raw: str):
    from app.models import TagSet
    from app.relocate import tags_from_track

    if kind == "gf":
        try:
            track_id = int(raw)
        except ValueError:
            return None, TagSet(), ""
        track = ctx.catalog.get_track(track_id)
        if track is None or track.user_id != user_id or track.status == "deleted":
            return None, TagSet(), ""
        path = Path(track.local_path) if track.local_path else None
        if path is not None and not path.is_file():
            path = None
        return path, tags_from_track(track), str(track.drive_file_id or "")
    if kind == "gd":
        return None, TagSet(title=raw), raw
    if kind == "gc":
        found = ctx.catalog.cached_file_by_prefix(raw)
        if not found:
            return None, TagSet(), ""
        path = Path(found)
        return (path if path.is_file() else None), TagSet(title=path.stem), ""
    return None, TagSet(), ""


async def _get_file_callback(callback, ctx: Ctx) -> None:
    from app.notify import send_notice
    from app.telegram_file import send_public_audio

    if not callback.from_user:
        await callback.answer()
        return
    from app.membership import answer_if_blocked

    if await answer_if_blocked(ctx, callback):
        return
    if not await allow_user(ctx, callback.from_user.id, getattr(callback.message, "chat", None)):
        await callback.answer("Not allowed.", show_alert=True)
        return
    data = callback.data or ""
    kind, raw = data.split(":", 1) if ":" in data else ("", "")
    chat = callback.message.chat if callback.message else None
    if chat is None:
        await callback.answer()
        return
    user_id = callback.from_user.id
    path, tags, drive_id = await _resolve_get_target(ctx, user_id, kind, raw)
    if path is None and drive_id and await _drive_expired(ctx, user_id, chat.id):
        await callback.answer()
        return
    if path is None and not drive_id:
        await send_notice(ctx, user_id, "File not found", level="error", chat_id=chat.id)
        await callback.answer()
        return
    await callback.answer()
    name = tags.title or "file"
    with contextlib.suppress(Exception):
        await ctx.bot.send_chat_action(chat.id, "upload_document")
    from app.audio_in import downloading_text

    placeholder = await ctx.bot.send_message(chat.id, downloading_text(name, 0))
    if not await _placeholder_alive(ctx.bot, chat.id, placeholder.message_id):
        await send_notice(ctx, user_id, "Download cancelled", level="info", chat_id=chat.id)
        return
    if (path is None or not path.is_file()) and drive_id:
        try:
            async def _paint(pct: int) -> None:
                await ctx.bot.edit_message_text(
                    downloading_text(name, pct),
                    chat_id=chat.id,
                    message_id=placeholder.message_id,
                )

            path = await _download_drive_file(
                ctx,
                user_id,
                drive_id,
                chat.id,
                alive=lambda: _placeholder_alive(ctx.bot, chat.id, placeholder.message_id),
                on_update=_paint,
            )
        except DownloadCancelled:
            await send_notice(ctx, user_id, "Download cancelled", level="info", chat_id=chat.id)
            return
    if not await _placeholder_alive(ctx.bot, chat.id, placeholder.message_id):
        await send_notice(ctx, user_id, "Download cancelled", level="info", chat_id=chat.id)
        return
    if path is None or not path.is_file():
        if not await _placeholder_alive(ctx.bot, chat.id, placeholder.message_id):
            await send_notice(ctx, user_id, "Download cancelled", level="info", chat_id=chat.id)
            return
        await send_notice(ctx, user_id, "File not found", level="error", chat_id=chat.id)
        return
    from app.captions import song_card
    from app.tags import read_audio_metrics

    metrics = None
    try:
        metrics = await asyncio.to_thread(read_audio_metrics, path)
    except Exception:
        log.debug("get card metrics failed", exc_info=True)
    await send_public_audio(
        ctx, chat.id, path, caption=song_card(tags, metrics), tags=tags
    )
    with contextlib.suppress(Exception):
        await placeholder.delete()


async def _get_page_callback(callback, ctx: Ctx) -> None:
    from app.membership import answer_if_blocked

    if await answer_if_blocked(ctx, callback):
        return
    _prune_pages(ctx)
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    token, page_s = parts[1], parts[2]
    row = _GET_PAGES.get(token)
    if not row or row.get("exp", 0) <= time.time():
        _GET_PAGES.pop(token, None)
        await callback.answer("Expired.", show_alert=True)
        return
    if callback.from_user and row.get("user_id") and row["user_id"] != callback.from_user.id:
        await callback.answer("Not your search.", show_alert=True)
        return
    try:
        page = int(page_s)
    except ValueError:
        await callback.answer()
        return
    items = list(row.get("items") or [])
    markup = _get_keyboard(ctx, items, page, user_id=int(row.get("user_id") or 0))
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            log.debug("get page edit failed", exc_info=True)
    await callback.answer()


async def _cancel_user(ctx: Ctx, message: Message) -> None:
    from app.notify import send_notice
    from app.prefs import load_user_prefs
    from app.queue import cancel_pending

    user_id = message.from_user.id
    prefs = load_user_prefs(ctx, user_id)
    rows = [
        row
        for row in ctx.catalog.list_pending_by_status("waiting", "processing", "queued")
        if row.user_id == user_id
    ]
    if not rows:
        return
    for row in rows:
        try:
            await cancel_pending(ctx, row)
        except Exception:
            log.exception("cancel pending failed id=%s", row.id)
    if prefs.notifications == "none" or prefs.channel == "reactions":
        return
    noun = "process" if len(rows) == 1 else "processes"
    await send_notice(
        ctx,
        user_id,
        f"Cancelled {len(rows)} ongoing {noun}.",
        level="info",
        chat_id=message.chat.id,
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
