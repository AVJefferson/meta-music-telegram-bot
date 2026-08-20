from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.enrich import fetch_lrclib
from app.library_index import entries_to_tracks, library_tracks_from_index, load_index_entries
from app.membership import is_forum_member
from app.models import Ctx, Identity, TrackRecord
from app.relocate import identity_from_track, tags_from_track
from app.suggest import (
    PAGE_SIZE,
    LastfmClient,
    Suggestion,
    leftover_matches_seed,
    owned_key,
    owned_keys_from_tracks,
    resolve_leftover,
    select_library_seeds,
    session_expires_at,
    suggest_tracks,
)
from app.tags import read_tagset
from app.util import clip_html, html_esc, is_synced_lrc, safe_link

log = logging.getLogger(__name__)
LYRICS_LIMIT = 3500


def suggest_label(item: Suggestion) -> str:
    mark = "✓ " if item.in_library else ""
    text = f"{mark}{item.artist} — {item.title}"
    return text[:64]


def suggest_list_keyboard(session_id: int, items: list[Suggestion], page: int) -> InlineKeyboardMarkup:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    rows = [
        [InlineKeyboardButton(text=suggest_label(item), callback_data=f"sgt:{session_id}:{start + offset}")]
        for offset, item in enumerate(items[start : start + PAGE_SIZE])
    ]
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"sg:{session_id}:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"sg:{session_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_suggest_card(item: Suggestion) -> str:
    title = html_esc(item.title)
    artist = html_esc(item.artist)
    owned = "\nIn library" if item.in_library else ""
    why = f"\n{html_esc(item.why)}" if item.why else ""
    href = safe_link(item.url)
    lastfm = f'\nLast.fm: <a href="{href}">open</a>' if href else ""
    mb = ""
    if item.mbid:
        mb_href = safe_link(f"https://musicbrainz.org/recording/{item.mbid}")
        if mb_href:
            mb = f'\nMusicBrainz: <a href="{mb_href}">open</a>'
    return f"<b>{artist}</b> — {title}{owned}{why}{lastfm}{mb}"


def parse_command_args(message: Message, command: CommandObject | None) -> str:
    if command and command.args:
        return command.args.strip()
    text = (message.text or message.caption or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def topic_name_for(message: Message, ctx: Ctx) -> str:
    if message.chat.type == "private":
        return ""
    thread_id = message.message_thread_id
    if not message.is_topic_message:
        return ctx.catalog.get_topic(thread_id or 1) or "General"
    if thread_id:
        cached = ctx.catalog.get_topic(thread_id)
        if cached:
            return cached
        return f"Topic {thread_id}"
    return "General"


def load_session_payload(raw: str) -> tuple[str | None, list[Suggestion]]:
    payload = json.loads(raw or "[]")
    language = None
    items_raw: list = []
    if isinstance(payload, dict):
        language = str(payload.get("language") or "") or None
        items_raw = payload.get("items") or []
    elif isinstance(payload, list):
        items_raw = payload
    items: list[Suggestion] = []
    if isinstance(items_raw, list):
        for item in items_raw:
            if isinstance(item, dict):
                items.append(Suggestion.from_dict(item))
    return language, items


async def _authorized(message: Message, ctx: Ctx) -> bool:
    if not message.from_user:
        return False
    if message.chat.type != "private" and message.chat.id != ctx.settings.allowed_chat_id:
        return False
    return await is_forum_member(ctx, message.from_user.id)


def _not_modified(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


async def _deliver(message: Message, text: str, markup=None, *, edit: bool = False) -> None:
    kwargs: dict = {"parse_mode": "HTML", "disable_web_page_preview": True}
    if markup is not None:
        kwargs["reply_markup"] = markup
    if edit:
        edit_text = getattr(message, "edit_text", None)
        if callable(edit_text):
            try:
                await edit_text(text, **kwargs)
                return
            except TelegramBadRequest as exc:
                if _not_modified(exc):
                    return
                log.debug("suggest list edit failed: %s", exc)
            except Exception:
                log.debug("suggest list edit failed", exc_info=True)
    try:
        if not edit:
            try:
                await message.reply(text, **kwargs)
                return
            except TelegramBadRequest as exc:
                log.debug("suggest list reply failed: %s", exc)
            except TypeError as exc:
                log.debug("suggest list reply failed: %s", exc)
        await message.answer(text, **kwargs)
    except (TelegramBadRequest, TypeError) as exc:
        log.warning("suggest list send failed: %s", exc)


def _page_items(items: list[Suggestion], page: int) -> tuple[int, list[Suggestion]]:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    return page, items[start : start + PAGE_SIZE]


def _list_text(page: int, pages: int, language: str | None, query: str) -> str:
    scope = html_esc(language) if language else "all languages"
    extra = f" · {html_esc(query)}" if query else ""
    return f"<b>Suggestions</b> — {scope}{extra}\npage {page + 1}/{pages}\n✓ = in library. Pick a row."


def _mark_page(ctx: Ctx, user_id: int, items: list[Suggestion], page: int) -> None:
    _, visible = _page_items(items, page)
    keys = [owned_key(item.artist, item.title) for item in visible]
    ctx.catalog.mark_suggest_shown(user_id, keys)


async def _show_session(
    message: Message,
    ctx: Ctx,
    session_id: int,
    items: list[Suggestion],
    page: int,
    *,
    user_id: int,
    language: str | None,
    query: str,
    edit: bool = False,
) -> None:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    _mark_page(ctx, user_id, items, page)
    await _deliver(
        message,
        _list_text(page, pages, language, query),
        suggest_list_keyboard(session_id, items, page),
        edit=edit,
    )


async def load_drive_library(ctx: Ctx, *, topic: str | None, notify=None) -> list[TrackRecord]:
    try:
        entries = await asyncio.to_thread(load_index_entries, ctx)
        if entries is None:
            if notify is not None:
                try:
                    await notify(
                        "Building <code>Telegram Music/library/tracks.json</code> from Drive tags (once). "
                        "FLACs stay on Drive."
                    )
                except Exception:
                    log.debug("suggest index notice failed", exc_info=True)
            tracks, rebuilt = await asyncio.to_thread(
                lambda: library_tracks_from_index(ctx, topic=topic, rebuild=True)
            )
        else:
            tracks = entries_to_tracks(entries, topic=topic)
            rebuilt = False
    except Exception:
        log.exception("drive library index failed")
        return []
    log.info("suggest drive index tracks=%s topic=%s rebuilt=%s", len(tracks), topic or "*", rebuilt)
    return tracks


async def _run_suggest(message: Message, ctx: Ctx, query: str) -> None:
    api_key = (ctx.settings.lastfm_api_key or "").strip()
    if not api_key:
        await _deliver(message, "Suggestions need LASTFM_API_KEY (Last.fm API account).")
        return
    mapper = ctx.genre
    topic = topic_name_for(message, ctx)
    language = mapper.language_from_topic(topic)
    query_tokens, leftover = mapper.extract_query_tokens(query)
    reply_track: TrackRecord | None = None
    reply = message.reply_to_message
    if reply:
        reply_track = ctx.catalog.get_track_by_message(message.chat.id, reply.message_id)
    library = ctx.catalog.list_library_tracks()
    if not library:
        library = await load_drive_library(
            ctx,
            topic=topic if language else None,
            notify=lambda text: _deliver(message, text),
        )
    if not library and reply_track is None:
        await _deliver(message, "Library is empty. Upload FLACs first.")
        return
    seeds = select_library_seeds(
        library,
        mapper,
        language=language,
        leftover=leftover,
        boosted=reply_track,
    )
    leftover_matched = bool(leftover) and any(leftover_matches_seed(seed, leftover) for seed in seeds)
    if leftover and not leftover_matched:
        resolved = await resolve_leftover(LastfmClient(ctx.http, api_key, ctx.catalog), leftover)
        if resolved:
            seeds.append(resolved)
    if language and not seeds:
        await _deliver(
            message,
            f"No library tracks for {html_esc(language)}. Upload some, or run /suggest in General / DM.",
        )
        return
    if not seeds:
        await _deliver(message, "No library tracks to seed suggestions.")
        return
    owned = owned_keys_from_tracks(library)
    shown = ctx.catalog.list_suggest_shown(message.from_user.id) if message.from_user else set()
    client = LastfmClient(ctx.http, api_key, ctx.catalog)
    try:
        items = await suggest_tracks(
            client,
            seeds,
            owned=owned,
            shown=shown,
            mapper=mapper,
            language=language,
            query_tokens=query_tokens,
            library=library,
        )
    except Exception:
        log.exception("suggest lookup failed")
        await _deliver(message, "Last.fm lookup failed. Try again.")
        return
    if not items:
        await _deliver(message, "No similar tracks found. Try another topic, genre, or seed song.")
        return
    _fill_origin_messages(ctx, items)
    session_id = ctx.catalog.insert_suggest_session(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
        query=query,
        results_json=json.dumps(
            {"language": language or "", "items": [item.to_dict() for item in items]},
            ensure_ascii=False,
        ),
        expires_at=session_expires_at(),
    )
    await _show_session(
        message,
        ctx,
        session_id,
        items,
        0,
        user_id=message.from_user.id,
        language=language,
        query=query,
    )


def _lyrics_filename(item: Suggestion) -> str:
    stem = f"{item.artist} - {item.title}".strip(" -") or "lyrics"
    return f"{stem[:80]}.lrc"


def format_lyrics_text(item: Suggestion, lyrics: str) -> str:
    header = format_suggest_card(item)
    if not lyrics.strip():
        return f"{header}\n\nNo lyrics found."
    kind = "Synced lyrics" if is_synced_lrc(lyrics) else "Lyrics"
    return clip_html(f"{header}\n\n<b>{kind}</b>\n{html_esc(lyrics)}", LYRICS_LIMIT)


def pick_lyrics(stored: str, fetched: str) -> str:
    if is_synced_lrc(stored):
        return stored
    if is_synced_lrc(fetched):
        return fetched
    return stored.strip() or fetched


def _fill_origin_messages(ctx: Ctx, items: list[Suggestion]) -> None:
    for item in items:
        if not item.in_library or item.message_id:
            continue
        if not item.track_id:
            continue
        messages = ctx.catalog.list_track_messages(item.track_id)
        if not messages:
            continue
        item.chat_id, item.message_id = messages[0]


async def resolve_lyrics(ctx: Ctx, item: Suggestion) -> str:
    stored = ""
    identity = Identity(confidence="low", title=item.title, artists=[item.artist] if item.artist else [])
    if item.in_library:
        track = ctx.catalog.get_track(item.track_id) if item.track_id else None
        if track:
            tags = tags_from_track(track)
            stored = tags.lyrics.strip()
            if track.local_path:
                path = Path(track.local_path)
                if path.is_file():
                    file_tags = await asyncio.to_thread(read_tagset, path)
                    if is_synced_lrc(file_tags.lyrics) or not stored:
                        stored = file_tags.lyrics.strip() or stored
            identity = identity_from_track(track)
            if not identity.title:
                identity.title = item.title
            if not identity.artists:
                identity.artists = [item.artist] if item.artist else []
    if is_synced_lrc(stored):
        return stored
    hit = await fetch_lrclib(ctx.http, identity)
    fetched = hit.lyrics if hit and hit.lyrics else ""
    return pick_lyrics(stored, fetched)


async def _send_text(ctx: Ctx, chat_id: int, text: str, **kwargs) -> None:
    await ctx.bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        **kwargs,
    )


async def _send_lyrics_payload(
    ctx: Ctx,
    chat_id: int,
    item: Suggestion,
    lyrics: str,
    *,
    thread_id: int | None = None,
    reply_to: int | None = None,
    as_file: bool = False,
) -> None:
    kwargs: dict = {}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
    body = format_lyrics_text(item, lyrics)
    too_long = bool(lyrics) and len(body) >= LYRICS_LIMIT - 20
    want_file = bool(lyrics) and (as_file or too_long or (is_synced_lrc(lyrics) and len(lyrics) > 1200))
    if want_file:
        caption = clip_html(format_suggest_card(item), 900)
        try:
            await ctx.bot.send_document(
                chat_id,
                BufferedInputFile(lyrics.encode("utf-8"), filename=_lyrics_filename(item)),
                caption=caption,
                parse_mode="HTML",
                **kwargs,
            )
            return
        except TelegramBadRequest:
            log.debug("suggest lyrics file send failed", exc_info=True)
            kwargs.pop("reply_to_message_id", None)
    try:
        await _send_text(ctx, chat_id, body, **kwargs)
    except TelegramBadRequest:
        kwargs.pop("reply_to_message_id", None)
        kwargs.pop("message_thread_id", None)
        await _send_text(ctx, chat_id, body, **kwargs)


async def _deliver_suggestion(callback: CallbackQuery, ctx: Ctx, item: Suggestion) -> None:
    assert callback.message is not None
    current = callback.message
    lyrics = await resolve_lyrics(ctx, item)
    if item.in_library:
        origin_chat = item.chat_id or current.chat.id
        origin_thread = item.thread_id
        origin_msg = item.message_id or None
        await _send_lyrics_payload(
            ctx,
            origin_chat,
            item,
            lyrics,
            thread_id=origin_thread,
            reply_to=origin_msg,
        )
        if current.chat.id != origin_chat:
            await _send_lyrics_payload(ctx, current.chat.id, item, lyrics)
        return
    if current.chat.type == "private":
        await _send_lyrics_payload(ctx, current.chat.id, item, lyrics, as_file=True)
        return
    await current.answer(
        format_suggest_card(item),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def build_suggest_command_router() -> Router:
    router = Router()

    @router.message(Command("suggest"))
    async def suggest_cmd(message: Message, ctx: Ctx, command: CommandObject | None = None) -> None:
        if not await _authorized(message, ctx):
            if message.chat.type == "private":
                await message.reply("Private access requires current membership in configured forum group.")
            return
        await _run_suggest(message, ctx, parse_command_args(message, command))

    @router.callback_query(F.data.regexp(r"^sg:\d+:\d+$"))
    async def suggest_page(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.from_user or not await is_forum_member(ctx, callback.from_user.id):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not callback.message:
            await callback.answer()
            return
        _, session_s, page_s = (callback.data or "sg:0:0").split(":")
        session = ctx.catalog.get_suggest_session(int(session_s))
        if session is None:
            await callback.answer("Suggestions expired. Run /suggest again.", show_alert=True)
            return
        await callback.answer()
        language, items = load_session_payload(session.results_json)
        try:
            await _show_session(
                callback.message,
                ctx,
                session.id,
                items,
                int(page_s),
                user_id=callback.from_user.id,
                language=language,
                query=session.query,
                edit=True,
            )
        except Exception:
            log.exception("suggest page failed")

    @router.callback_query(F.data.regexp(r"^sgt:\d+:\d+$"))
    async def suggest_pick(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.from_user or not await is_forum_member(ctx, callback.from_user.id):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not callback.message:
            await callback.answer()
            return
        _, session_s, index_s = (callback.data or "sgt:0:0").split(":")
        session = ctx.catalog.get_suggest_session(int(session_s))
        if session is None:
            await callback.answer("Suggestions expired. Run /suggest again.", show_alert=True)
            return
        _language, items = load_session_payload(session.results_json)
        index = int(index_s)
        if index < 0 or index >= len(items):
            await callback.answer("Gone.", show_alert=True)
            return
        await callback.answer()
        try:
            await _deliver_suggestion(callback, ctx, items[index])
        except Exception:
            log.exception("suggest card send failed session=%s index=%s", session.id, index)
            try:
                await callback.message.answer("Could not open that suggestion.")
            except TelegramBadRequest:
                log.debug("suggest card error reply failed", exc_info=True)

    return router
