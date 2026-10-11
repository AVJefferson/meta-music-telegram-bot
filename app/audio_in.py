from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from aiogram.types import Message

from app.formats import detect_format, is_allowed_audio_message, message_name_mime, user_allowed_formats
from app.models import Ctx

log = logging.getLogger(__name__)

CHECKS_TEXT = "...Going through this song file"
FORMAT_TEXT = "File format not supported"
LARGE_TEXT = "File is too large"
CORRUPT_TEXT = "File is corrupted"
DELETED_TEXT = "Song deleted while downloading"
BLOCKED_TEXT = "You are blocked."

_AUDIOISH = {
    ".aiff",
    ".aif",
    ".wma",
    ".ape",
    ".wv",
    ".dsf",
    ".dff",
    ".mka",
    ".aac",
    ".mp2",
    ".oga",
}

_download_locks: dict[int, asyncio.Lock] = {}
_upload_locks: dict[int, asyncio.Lock] = {}
_identify_slots = asyncio.Semaphore(2)


class SongDeleted(Exception):
    pass


def chat_download_lock(chat_id: int) -> asyncio.Lock:
    lock = _download_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _download_locks[chat_id] = lock
    return lock


def user_upload_lock(user_id: int) -> asyncio.Lock:
    key = int(user_id or 0)
    lock = _upload_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _upload_locks[key] = lock
    return lock


def identify_slots() -> asyncio.Semaphore:
    return _identify_slots


def progress_due(elapsed: float, delta_pct: int) -> bool:
    """Update when both 5s and 25 points have passed. The longer gate wins."""
    return elapsed >= 5.0 and delta_pct >= 25


def downloading_text(file_name: str, percent: int) -> str:
    name = file_name or "audio"
    return f"...Downloading {name} ({max(0, min(100, int(percent)))}%)"


def max_audio_bytes(ctx: Ctx) -> int:
    raw = None
    getter = getattr(ctx.catalog, "get_setting", None)
    if callable(getter):
        raw = getter("max_audio_mb")
    if raw is None:
        raw = getattr(ctx.settings, "max_audio_mb", 50)
    try:
        mb = float(raw)
    except (TypeError, ValueError):
        mb = 50.0
    if mb <= 0:
        mb = 50.0
    return int(mb * 1024 * 1024)


def media_size(message: Message) -> int | None:
    media = message.document or message.audio
    if media is None:
        return None
    size = getattr(media, "file_size", None)
    if size is None:
        return None
    try:
        return int(size)
    except (TypeError, ValueError):
        return None


def looks_like_audio(message: Message) -> bool:
    if getattr(message, "audio", None) is not None:
        return True
    name, mime = message_name_mime(message)
    if detect_format(name, mime):
        return True
    if (mime or "").lower().startswith("audio/"):
        return True
    return Path(name).suffix.lower() in _AUDIOISH


def known_supported(message: Message) -> bool:
    name, mime = message_name_mime(message)
    return detect_format(name, mime) is not None


async def source_deleted(bot, chat_id: int, message_id: int) -> bool:
    if not message_id:
        return False
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
        return False
    except Exception as exc:
        text = str(exc).lower()
        if "not modified" in text or "message is not modified" in text:
            return False
        return "not found" in text or "message_id_invalid" in text or "message to edit not found" in text


def _local_download_path(bot, file_path: str) -> Path | None:
    path = Path(file_path or "")
    if path.is_file():
        return path
    api = getattr(getattr(bot, "session", None), "api", None)
    if api is None or not getattr(api, "is_local", False):
        return None
    wrapper = getattr(api, "wrap_local_file", None)
    if wrapper is None:
        return None
    try:
        local = Path(wrapper.to_local(file_path))
    except Exception:
        return None
    return local if local.is_file() else None


async def download_telegram(
    bot, telegram_file, dest: Path, on_progress, should_stop, total_hint: int | None = None
) -> None:
    """Copy a local Bot API file, or stream a remote file, and report progress."""
    file_path = str(getattr(telegram_file, "file_path", "") or "")
    local = _local_download_path(bot, file_path)
    if local is not None:
        await copy_with_progress(local, dest, on_progress, should_stop)
        return
    known = int(getattr(telegram_file, "file_size", None) or total_hint or 0)
    api = bot.session.api
    url = api.file_url(bot.token, file_path)
    stream = bot.session.stream_content(
        url=url, timeout=3600, chunk_size=256 * 1024, raise_for_status=True
    )
    copied = 0
    last_t = time.monotonic()
    last_pct = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as out:
            async for chunk in stream:
                if await should_stop():
                    raise SongDeleted()
                if not chunk:
                    continue
                out.write(chunk)
                copied += len(chunk)
                if known:
                    pct = min(99, int(copied * 100 / known))
                    due = progress_due(time.monotonic() - last_t, pct - last_pct)
                else:
                    pct = 0
                    due = time.monotonic() - last_t >= 5
                if due:
                    await on_progress(pct)
                    last_t = time.monotonic()
                    last_pct = pct
    finally:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()
    await on_progress(100)


async def copy_with_progress(src: Path, dest: Path, on_progress, should_stop) -> None:
    total = src.stat().st_size or 1
    copied = 0
    last_t = time.monotonic()
    last_pct = 0
    loop = asyncio.get_running_loop()
    with src.open("rb") as inp, dest.open("wb") as out:
        while True:
            if await should_stop():
                raise SongDeleted()
            chunk = await loop.run_in_executor(None, inp.read, 256 * 1024)
            if not chunk:
                break
            await loop.run_in_executor(None, out.write, chunk)
            copied += len(chunk)
            pct = min(100, int(copied * 100 / total))
            now = time.monotonic()
            if pct >= 100 or progress_due(now - last_t, pct - last_pct):
                await on_progress(pct)
                last_t = now
                last_pct = pct


def user_wants_format(ctx: Ctx, user_id: int, message: Message) -> bool:
    return is_allowed_audio_message(message, user_allowed_formats(ctx, user_id))
