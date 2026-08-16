from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.membership import is_forum_member
from app.models import Ctx, TrackRecord
from app.queue import tag_preview
from app.relocate import hydrate_track_tags, metrics_from_track, read_tags_for_card
from app.util import html_esc, safe_link

log = logging.getLogger(__name__)
PAGE_SIZE = 8


def review_label(track: TrackRecord) -> str:
    stem = Path(track.file_name or track.relative_path or "track").stem
    title = track.title or stem
    artist = track.artist or stem
    album = track.album or stem
    text = f"{artist} — {album} — {title}"
    return text[:64]


def review_list_keyboard(items: list[TrackRecord], page: int) -> InlineKeyboardMarkup:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    rows = [
        [InlineKeyboardButton(text=review_label(track), callback_data=f"rvt:{track.id}")]
        for track in items[start : start + PAGE_SIZE]
    ]
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"rvp:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"rvp:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_song_card(track: TrackRecord) -> str:
    tags = read_tags_for_card(track)
    dest_label = "library" if track.kind == "library" else "review"
    href = safe_link(track.drive_url)
    link = f'\nDrive: <a href="{href}">open</a>' if href else ""
    relative = html_esc(track.relative_path or "")
    path_line = f"\n<code>{relative}</code>" if relative else ""
    return f"{dest_label}\n\n{tag_preview(tags, metrics_from_track(track))}{link}{path_line}"


async def _sync_drive_review(ctx: Ctx) -> None:
    try:
        items = await asyncio.to_thread(
            ctx.drive.list_review_items, ctx.settings.gdrive_review_folder_id
        )
    except Exception:
        log.exception("Drive review listing failed")
        return
    known = {track.drive_file_id for track in ctx.catalog.list_review_tracks() if track.drive_file_id}
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
        )


async def _authorized(message: Message, ctx: Ctx) -> bool:
    if not message.from_user:
        return False
    if message.chat.type != "private" and message.chat.id != ctx.settings.allowed_chat_id:
        return False
    return await is_forum_member(ctx, message.from_user.id)


async def _show_list(message: Message, ctx: Ctx, page: int = 0, *, edit: bool = False) -> None:
    await _sync_drive_review(ctx)
    items = ctx.catalog.list_review_tracks()
    if not items:
        text = "Review queue is empty."
        if edit and message.chat:
            try:
                await message.edit_text(text)
            except Exception:
                await message.answer(text)
        else:
            await message.reply(text)
        return
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    text = f"<b>Review queue</b> — page {page + 1}/{pages}\nPick a song, then react on the info card."
    markup = review_list_keyboard(items, page)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            pass
    await message.reply(text, parse_mode="HTML", reply_markup=markup)


async def _send_card(callback: CallbackQuery, ctx: Ctx, track: TrackRecord) -> None:
    track = await hydrate_track_tags(ctx, track)
    text = format_song_card(track)
    kwargs: dict = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    assert callback.message is not None
    if callback.message.message_thread_id:
        kwargs["message_thread_id"] = callback.message.message_thread_id
    sent = await callback.message.answer(**kwargs)
    ctx.catalog.bind_track_message(track.id, sent.chat.id, sent.message_id)
    if track.thread_id is None and callback.message.message_thread_id:
        ctx.catalog.update_track(track.id, thread_id=callback.message.message_thread_id)


def build_review_command_router() -> Router:
    router = Router()

    @router.message(Command("review", "reviews"))
    async def review_cmd(message: Message, ctx: Ctx) -> None:
        if not await _authorized(message, ctx):
            if message.chat.type == "private":
                await message.reply("Private access requires current membership in configured forum group.")
            return
        await _show_list(message, ctx)

    @router.callback_query(F.data.regexp(r"^rvp:\d+$"))
    async def review_page(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.from_user or not await is_forum_member(ctx, callback.from_user.id):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not callback.message:
            await callback.answer()
            return
        page = int((callback.data or "rvp:0").split(":", 1)[1])
        await callback.answer()
        await _show_list(callback.message, ctx, page, edit=True)

    @router.callback_query(F.data.regexp(r"^rvt:\d+$"))
    async def review_pick(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.from_user or not await is_forum_member(ctx, callback.from_user.id):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not callback.message:
            await callback.answer()
            return
        track_id = int((callback.data or "rvt:0").split(":", 1)[1])
        track = ctx.catalog.get_track(track_id)
        if track is None or track.kind != "review" or track.status == "deleted":
            await callback.answer("Gone from review.", show_alert=True)
            return
        await callback.answer()
        try:
            await _send_card(callback, ctx, track)
        except Exception:
            log.exception("review card send failed track=%s", track_id)
            await callback.message.answer("Could not load that song from Drive. Try again.")

    return router
