from __future__ import annotations

import logging
from pathlib import Path

from aiogram.types import FSInputFile, InputMediaAudio, InputMediaDocument

from app.captions import music_caption
from app.models import TagSet, TrackRecord

log = logging.getLogger(__name__)


def message_file_id(message) -> str:
    for attr in ("audio", "document"):
        obj = getattr(message, attr, None)
        fid = getattr(obj, "file_id", None) if obj is not None else None
        if fid:
            return str(fid)
    return ""


def audio_send_kwargs(path: Path, tags: TagSet | None = None) -> dict:
    title = ((tags.title if tags else "") or "").strip() or path.stem
    performer = ((tags.artist if tags else "") or "").strip()
    kwargs: dict = {"title": title}
    if performer:
        kwargs["performer"] = performer
    try:
        from app.tags import read_audio_metrics

        duration = int(read_audio_metrics(path).duration or 0)
        if duration > 0:
            kwargs["duration"] = duration
    except Exception:
        log.debug("audio duration unavailable path=%s", path, exc_info=True)
    return kwargs


async def send_public_audio(
    ctx,
    chat_id: int,
    path: Path,
    *,
    caption: str = "",
    thread_id: int | None = None,
    tags: TagSet | None = None,
    filename: str | None = None,
):
    """Send FLAC as playable Telegram audio. Fall back to a document if the API rejects it."""
    name = filename or path.name
    media = FSInputFile(path, filename=name)
    extra = audio_send_kwargs(path, tags)
    send_kwargs: dict = {"parse_mode": "HTML"}
    if caption:
        send_kwargs["caption"] = caption
    if thread_id:
        send_kwargs["message_thread_id"] = thread_id
    try:
        return await ctx.bot.send_audio(chat_id, media, **extra, **send_kwargs)
    except Exception:
        log.warning("send_audio failed chat=%s file=%s, falling back to document", chat_id, name, exc_info=True)
        return await ctx.bot.send_document(chat_id, media, **send_kwargs)


async def update_public_audio(
    ctx,
    *,
    chat_id: int,
    message_id: int,
    path: Path,
    caption: str,
    correct_media: bool,
    tags: TagSet | None = None,
) -> str | None:
    """Update the public audio message. Media replace is opt-in on first save, always on later edits."""
    file_id: str | None = None
    if correct_media and path.is_file() and message_id:
        extra = audio_send_kwargs(path, tags)
        media_file = FSInputFile(path, filename=path.name)
        try:
            edited = await ctx.bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaAudio(
                    media=media_file,
                    caption=caption,
                    parse_mode="HTML",
                    **extra,
                ),
            )
            return message_file_id(edited) or None
        except Exception:
            log.warning("editMessageMedia audio failed chat=%s message=%s", chat_id, message_id, exc_info=True)
        try:
            edited = await ctx.bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaDocument(
                    media=media_file,
                    caption=caption,
                    parse_mode="HTML",
                ),
            )
            return message_file_id(edited) or None
        except Exception:
            log.warning("editMessageMedia failed chat=%s message=%s", chat_id, message_id, exc_info=True)
    if message_id:
        try:
            await ctx.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=caption,
                parse_mode="HTML",
            )
        except Exception:
            log.warning("edit caption failed chat=%s message=%s", chat_id, message_id, exc_info=True)
    return file_id


def caption_for(tags: TagSet, track: TrackRecord | None = None, *, relative: str = "", extra: str = "") -> str:
    return music_caption(tags=tags, track=track, relative_path=relative, extra=extra)
