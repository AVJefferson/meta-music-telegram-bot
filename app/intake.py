from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram.types import FSInputFile
from aiogram.types.chat import Chat

from app.captions import music_caption
from app.errors import AppError
from app.membership import check_upload_rate, touch
from app.models import Ctx, Job, TagSet
from app.paths import file_sha256, remember_cache
from app.util import sanitize_filename

log = logging.getLogger(__name__)


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")


async def ingest_local_flac(
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
) -> int:
    touch(ctx, user_id)
    check_upload_rate(ctx, user_id)
    local = Path(path)
    if not local.is_file():
        raise AppError("not_found")
    sha = await asyncio.to_thread(file_sha256, local)
    existing = ctx.catalog.find_user_track_by_sha(user_id, sha)
    if existing:
        caption = music_caption(track=existing, extra="already in library/review")
        await ctx.bot.send_document(
            chat.id,
            FSInputFile(existing.local_path or local),
            caption=caption,
            parse_mode="HTML",
            message_thread_id=thread_id,
        )
        return existing.id
    remember_cache(ctx, sha, local)
    caption = music_caption(tags=TagSet(title=Path(file_name).stem), extra="identifying…")
    sent = await ctx.bot.send_document(
        chat.id,
        FSInputFile(local),
        caption=caption,
        parse_mode="HTML",
        message_thread_id=thread_id,
    )
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
        source_report_json="{}",
        chat_id=chat.id,
        thread_id=thread_id,
        status_message_id=sent.message_id,
        topic_name=topic_name or "",
        file_name=sanitize_filename(file_name) or "track.flac",
        telegram_file_id=telegram_file_id or (sent.document.file_id if sent.document else ""),
        source_message_id=source_message_id or sent.message_id,
        expires_at=_expires(),
        user_id=user_id,
        public_message_id=sent.message_id,
    )
    await ctx.jobs.put(
        Job(
            chat_id=chat.id,
            thread_id=thread_id,
            topic_name=topic_name or "",
            file_id=telegram_file_id or (sent.document.file_id if sent.document else ""),
            file_name=file_name,
            status_message_id=sent.message_id,
            local_path=str(local),
            private=chat.id > 0,
            source_pending_id=pending_id,
            source_message_id=source_message_id or sent.message_id,
            user_id=user_id,
            public_message_id=sent.message_id,
        )
    )
    return pending_id
