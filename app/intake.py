from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram.types.chat import Chat

from app.errors import AppError
from app.formats import stored_download_name
from app.membership import check_upload_rate, touch
from app.models import Ctx, Job, TagSet
from app.paths import file_sha256, remember_cache
from app.telegram_file import message_file_id, send_public_audio
from app.util import sanitize_filename

log = logging.getLogger(__name__)


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")


async def ingest_local_audio(
    ctx: Ctx,
    *,
    chat: Chat,
    user_id: int,
    path: str | Path,
    file_name: str,
    thread_id: int | None = None,
    topic_name: str = "",
    telegram_file_id: str = "",
    source_message_id: int = 0,
    status_message_id: int | None = None,
) -> int:
    touch(ctx, user_id)
    check_upload_rate(ctx, user_id)
    local = Path(path)
    if not local.is_file():
        raise AppError("not_found")
    sha = await asyncio.to_thread(file_sha256, local)
    remember_cache(ctx, sha, local)
    from app.audio_in import CHECKS_TEXT

    tags = TagSet(title=Path(file_name).stem)
    caption = CHECKS_TEXT
    ctx.catalog.increment_songs_processed(user_id)
    text_status = bool(status_message_id)
    if text_status:
        sent_id = int(status_message_id or 0)
        try:
            await ctx.bot.edit_message_text(
                caption,
                chat_id=chat.id,
                message_id=sent_id,
                parse_mode="HTML",
            )
        except Exception:
            log.debug("placeholder edit failed message=%s", sent_id, exc_info=True)
        sent_file_id = telegram_file_id
    else:
        sent = await send_public_audio(
            ctx,
            chat.id,
            local,
            caption=caption,
            thread_id=thread_id,
            tags=tags,
        )
        sent_id = sent.message_id
        sent_file_id = telegram_file_id or message_file_id(sent)
    pending_id = ctx.catalog.insert_pending_review(
        phase="intake",
        status="queued",
        local_path=str(local),
        sidecar_path=None,
        relative_path=None,
        kind="library",
        original_json="{}",
        recommended_json="{}",
        working_json="{}",
        candidates_json="[]",
        identity_json="{}",
        source_report_json='{"text_status": true}' if text_status else "{}",
        chat_id=chat.id,
        thread_id=thread_id,
        status_message_id=sent_id,
        topic_name=topic_name or "",
        file_name=sanitize_filename(file_name) or stored_download_name(file_name),
        telegram_file_id=sent_file_id,
        source_message_id=source_message_id or sent_id,
        expires_at=_expires(),
        user_id=user_id,
        public_message_id=sent_id,
    )
    await ctx.jobs.put(
        Job(
            chat_id=chat.id,
            thread_id=thread_id,
            topic_name=topic_name or "",
            file_id=sent_file_id,
            file_name=file_name,
            status_message_id=sent_id,
            local_path=str(local),
            private=chat.id > 0,
            source_pending_id=pending_id,
            source_message_id=source_message_id or sent_id,
            user_id=user_id,
            public_message_id=sent_id,
        )
    )
    return pending_id


ingest_local_flac = ingest_local_audio
