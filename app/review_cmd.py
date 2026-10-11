from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.membership import allow_from_callback, allow_user, touch, user_is_bot
from app.models import Ctx, TrackRecord
from app.queue import tag_preview
from app.relocate import hydrate_track_tags, metrics_from_track, read_tags_for_card
from app.util import safe_link

log = logging.getLogger(__name__)
PAGE_SIZE = 8
_REVIEW_PAGES: dict[str, dict] = {}


def review_label(track: TrackRecord) -> str:
    stem = Path(track.file_name or track.relative_path or "track").stem
    title = track.title or stem
    artist = track.artist or stem
    album = track.album or stem
    text = f"{artist} — {album} — {title}"
    return text[:64]


def _review_ttl(ctx: Ctx) -> int:
    return int(getattr(ctx.settings, "pagination_ttl_seconds", 300) or 300)


def review_list_keyboard(
    items: list[TrackRecord], page: int, *, ctx: Ctx, user_id: int
) -> InlineKeyboardMarkup:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    now = time.time()
    dead = [key for key, row in _REVIEW_PAGES.items() if row.get("exp", 0) <= now]
    for key in dead:
        _REVIEW_PAGES.pop(key, None)
    token = secrets.token_urlsafe(6)
    _REVIEW_PAGES[token] = {"user_id": user_id, "exp": now + _review_ttl(ctx)}
    rows = [
        [InlineKeyboardButton(text=review_label(track), callback_data=f"rvt:{track.id}")]
        for track in items[start : start + PAGE_SIZE]
    ]
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"rvp:{token}:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"rvp:{token}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_song_card(track: TrackRecord, *, chat_id: int | None = None) -> str:
    tags = read_tags_for_card(track)
    preview = tag_preview(tags, metrics_from_track(track))
    resolved = chat_id
    if resolved is None:
        raw = getattr(track, "source_chat_id", None)
        resolved = raw if isinstance(raw, int) else None
    link = ""
    if isinstance(resolved, int) and resolved > 0:
        href = safe_link(getattr(track, "drive_url", None))
        if href:
            link = f'\nDrive: <a href="{href}">open</a>'
    return f"{preview}{link}"


async def _sync_drive_review(ctx: Ctx, user_id: int) -> None:
    from app.drive import resolve_drive, user_drive_root

    folder = user_drive_root(ctx, "review", user_id)
    drive = resolve_drive(ctx, user_id)
    if not folder or drive is None or not callable(getattr(drive, "list_review_items", None)):
        return
    try:
        items = await asyncio.to_thread(drive.list_review_items, folder)
    except Exception:
        log.exception("Drive review listing failed")
        return
    known = {track.drive_file_id for track in ctx.catalog.list_review_tracks(user_id) if track.drive_file_id}
    for item in items:
        if item.file_id in known:
            continue
        ctx.catalog.insert_pending(
            kind="review",
            mb_recording_id=None,
            acoustid=None,
            local_path="",
            sidecar_path=None,
            relative_path=item.relative_path,
            bit_depth=None,
            sample_rate=None,
            title="",
            artist="",
            album="",
            status="uploaded",
            file_name=item.name,
            drive_file_id=item.file_id,
            drive_sidecar_id=item.sidecar_id,
            user_id=user_id,
        )


async def _authorized(message: Message, ctx: Ctx) -> bool:
    if not message.from_user or user_is_bot(message.from_user, ctx.bot):
        return False
    return await allow_user(ctx, message.from_user.id, message.chat)


def _not_modified(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


async def _deliver_list(message: Message, text: str, markup=None, *, edit: bool = False) -> None:
    kwargs: dict = {"parse_mode": "HTML"}
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
                log.debug("review list edit failed: %s", exc)
            except Exception:
                log.debug("review list edit failed", exc_info=True)
    try:
        if not edit:
            try:
                await message.reply(text, **kwargs)
                return
            except TelegramBadRequest as exc:
                log.debug("review list reply failed: %s", exc)
            except TypeError as exc:
                log.debug("review list reply failed: %s", exc)
        await message.answer(text, **kwargs)
    except (TelegramBadRequest, TypeError) as exc:
        log.warning("review list send failed: %s", exc)


async def _show_list(message: Message, ctx: Ctx, page: int = 0, *, edit: bool = False, user_id: int = 0) -> None:
    await _sync_drive_review(ctx, user_id)
    items = ctx.catalog.list_review_tracks(user_id or None)
    if not items:
        await _send_review_reply(message, ctx, user_id, "Review Pile: 0", None, edit=edit)
        return
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    text = f"Review Pile: {len(items)}"
    markup = review_list_keyboard(items, page, ctx=ctx, user_id=user_id)
    from app.user_cmd import _webapp_url

    url = _webapp_url(ctx, "review")
    if url and markup is not None:
        markup.inline_keyboard.append(
            [InlineKeyboardButton(text="Open Review", web_app=WebAppInfo(url=url))]
        )
    await _send_review_reply(message, ctx, user_id, text, markup, edit=edit)


async def _send_review_reply(message: Message, ctx: Ctx, user_id: int, text: str, markup, *, edit: bool) -> None:
    """Group /review goes to the user's private chat, including an empty pile."""
    if message.chat.type != "private" and not edit:
        try:
            await ctx.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            log.debug("review private send failed", exc_info=True)
    await _deliver_list(message, text, markup, edit=edit)


async def _send_card(callback: CallbackQuery, ctx: Ctx, track: TrackRecord) -> None:
    from app.audio_in import downloading_text, source_deleted
    from app.captions import song_card
    from app.notify import send_notice
    from app.telegram_file import send_public_audio

    track = await hydrate_track_tags(ctx, track)
    assert callback.message is not None
    chat = callback.message.chat
    user_id = callback.from_user.id if callback.from_user else 0
    name = track.title or track.file_name or "file"
    path = Path(track.local_path) if track.local_path else None
    tags = read_tags_for_card(track)
    drive_id = ""
    if path is None or not path.is_file():
        drive_id = str(getattr(track, "drive_file_id", "") or "")
        from app.user_cmd import _drive_expired

        if drive_id and await _drive_expired(ctx, user_id, chat.id):
            return
        if not drive_id:
            await send_notice(ctx, user_id, "File not found", level="error", chat_id=chat.id)
            return
    with contextlib.suppress(Exception):
        await ctx.bot.send_chat_action(chat.id, "upload_document")
    placeholder = await callback.message.answer(downloading_text(name, 0))
    if await source_deleted(ctx.bot, chat.id, placeholder.message_id):
        await send_notice(ctx, user_id, "Download cancelled", level="info", chat_id=chat.id)
        return
    if drive_id and (path is None or not path.is_file()):
        from app.user_cmd import DownloadCancelled, _download_drive_file

        async def _alive() -> bool:
            return not await source_deleted(ctx.bot, chat.id, placeholder.message_id)

        async def _paint(pct: int) -> None:
            await ctx.bot.edit_message_text(
                downloading_text(name, pct),
                chat_id=chat.id,
                message_id=placeholder.message_id,
            )

        try:
            path = await _download_drive_file(
                ctx,
                user_id,
                drive_id,
                chat.id,
                alive=_alive,
                on_update=_paint,
            )
        except DownloadCancelled:
            await send_notice(ctx, user_id, "Download cancelled", level="info", chat_id=chat.id)
            return
    if await source_deleted(ctx.bot, chat.id, placeholder.message_id):
        await send_notice(ctx, user_id, "Download cancelled", level="info", chat_id=chat.id)
        return
    if path is None or not path.is_file():
        await send_notice(ctx, user_id, "File not found", level="error", chat_id=chat.id)
        with contextlib.suppress(Exception):
            await placeholder.delete()
        return
    from app.tags import read_audio_metrics

    metrics = None
    try:
        metrics = await asyncio.to_thread(read_audio_metrics, path)
    except Exception:
        log.debug("review card metrics failed", exc_info=True)
    sent = await send_public_audio(
        ctx,
        chat.id,
        path,
        caption=song_card(tags, metrics),
        thread_id=callback.message.message_thread_id,
        tags=tags,
    )
    ctx.catalog.bind_track_message(track.id, sent.chat.id, sent.message_id)
    with contextlib.suppress(Exception):
        await placeholder.delete()
    thread_id = callback.message.message_thread_id
    if track.thread_id is None and thread_id:
        ctx.catalog.update_track(track.id, thread_id=thread_id)


def build_review_command_router() -> Router:
    router = Router()

    @router.message(Command("review", "reviews"))
    async def review_cmd(message: Message, ctx: Ctx) -> None:
        from app.user_cmd import _blocked

        if await _blocked(message, ctx):
            return
        if not await _authorized(message, ctx):
            if message.chat.type == "private":
                await message.reply("Private access requires membership in a known group.")
            return
        touch(ctx, message.from_user.id, message.from_user)
        await _show_list(message, ctx, user_id=message.from_user.id)

    @router.callback_query(F.data.regexp(r"^rvp:[^:]+:\d+$"))
    async def review_page(callback: CallbackQuery, ctx: Ctx) -> None:
        from app.membership import answer_if_blocked

        if await answer_if_blocked(ctx, callback):
            return
        if not await allow_from_callback(ctx, callback):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not callback.message:
            await callback.answer()
            return
        _token, page_s = (callback.data or "rvp:x:0").split(":", 1)[1].rsplit(":", 1)
        row = _REVIEW_PAGES.get(_token)
        if not row or row.get("exp", 0) <= time.time() or row.get("user_id") != callback.from_user.id:
            _REVIEW_PAGES.pop(_token, None)
            await callback.answer("Expired.", show_alert=True)
            return
        page = int(page_s)
        await callback.answer()
        try:
            await _show_list(callback.message, ctx, page, edit=True, user_id=callback.from_user.id)
        except Exception:
            log.exception("review page failed")

    @router.callback_query(F.data.regexp(r"^rvt:\d+$"))
    async def review_pick(callback: CallbackQuery, ctx: Ctx) -> None:
        from app.membership import answer_if_blocked

        if await answer_if_blocked(ctx, callback):
            return
        if not await allow_from_callback(ctx, callback):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not callback.message:
            await callback.answer()
            return
        track_id = int((callback.data or "rvt:0").split(":", 1)[1])
        track = ctx.catalog.get_track(track_id)
        if (
            track is None
            or track.kind != "review"
            or track.status == "deleted"
            or getattr(track, "user_id", 0) != callback.from_user.id
        ):
            await callback.answer("Gone from review.", show_alert=True)
            return
        await callback.answer()
        try:
            await _send_card(callback, ctx, track)
        except Exception:
            log.exception("review card send failed track=%s", track_id)
            try:
                await callback.message.answer("Could not load that song from Drive. Try again.")
            except TelegramBadRequest:
                log.debug("review card error reply failed", exc_info=True)

    return router
