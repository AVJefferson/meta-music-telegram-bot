from __future__ import annotations

import logging
from pathlib import Path

from aiogram.types import FSInputFile, InputMediaDocument

from app.captions import music_caption
from app.models import TagSet, TrackRecord

log = logging.getLogger(__name__)


async def update_public_audio(
    ctx,
    *,
    chat_id: int,
    message_id: int,
    path: Path,
    caption: str,
    correct_media: bool,
) -> str | None:
    """Update the public audio message. Media replace is opt-in on first save, always on later edits."""
    file_id: str | None = None
    if correct_media and path.is_file() and message_id:
        try:
            edited = await ctx.bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=InputMediaDocument(
                    media=FSInputFile(path, filename=path.name),
                    caption=caption,
                    parse_mode="HTML",
                ),
            )
            doc = getattr(edited, "document", None) or getattr(edited, "audio", None)
            file_id = str(getattr(doc, "file_id", None) or "") or None
            return file_id
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
