from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

from app.cleanup import alert_general
from app.enrich import enrich
from app.identify import identify_file
from app.library import library_relative, place_file, review_relative, unlink_quiet, write_sidecar
from app.models import Ctx, Identity, Job, TagSet
from app.tags import identity_to_tags, read_hints, write_tags
from app.util import html_esc

log = logging.getLogger(__name__)


def quality(bit_depth: int | None, sample_rate: int | None) -> tuple[int, int]:
    return (bit_depth or 0, sample_rate or 0)


def tag_preview(tags: TagSet) -> str:
    lyrics = "synced" if tags.lyrics else "none"
    return (
        f"<b>{html_esc(tags.title)}</b>\n"
        f"Artist: {html_esc(tags.artist)}\n"
        f"Album: {html_esc(tags.album)}\n"
        f"Album artist: {html_esc(tags.albumartist)}\n"
        f"Composer: {html_esc(tags.composer)}\n"
        f"Genre: {html_esc(tags.genre)}\n"
        f"Year: {html_esc(tags.date)}\n"
        f"Lyrics: {lyrics}"
    )


async def edit_status(ctx: Ctx, job: Job, text: str) -> None:
    try:
        await ctx.bot.edit_message_text(
            text,
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        log.debug("status edit failed", exc_info=True)


async def worker(name: str, queue: asyncio.Queue[Job], ctx: Ctx) -> None:
    log.info("worker %s started", name)
    while True:
        job = await queue.get()
        try:
            await process_job(job, ctx)
        except Exception:
            log.exception("job failed for %s", job.file_name)
            try:
                await edit_status(ctx, job, f"Failed processing <code>{html_esc(job.file_name)}</code>. Check logs.")
            except Exception:
                pass
        finally:
            queue.task_done()


async def process_job(job: Job, ctx: Ctx) -> None:
    settings = ctx.settings
    work = settings.tmp_root / str(uuid.uuid4())
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / "source.flac"
    try:
        await edit_status(ctx, job, f"Downloading <code>{html_esc(job.file_name)}</code>…")
        file = await ctx.bot.get_file(job.file_id)
        await ctx.bot.download(file, destination=tmp)

        hints = await asyncio.to_thread(read_hints, tmp, job.file_name)
        await edit_status(ctx, job, "Fingerprinting / identifying…")
        identity = await asyncio.to_thread(identify_file, tmp, hints, settings, ctx.mb)

        await edit_status(ctx, job, "Fetching cover, lyrics, genre…")
        enrichment = await enrich(
            ctx.http,
            identity,
            ctx.genre,
            settings.lastfm_api_key,
            job.topic_name,
        )
        tags = identity_to_tags(identity, enrichment)
        if not tags.title:
            tags.title = Path(job.file_name).stem

        await asyncio.to_thread(write_tags, tmp, tags, enrichment.cover, enrichment.cover_mime)

        kind = "library" if identity.confidence == "high" else "review"
        if kind == "library" and identity.mb_recording_id:
            existing = ctx.catalog.find_library_by_mbid(identity.mb_recording_id)
            if existing:
                new_q = quality(identity.bit_depth, identity.sample_rate)
                old_q = quality(existing.bit_depth, existing.sample_rate)
                if existing.status == "uploaded" and new_q <= old_q:
                    await edit_status(
                        ctx,
                        job,
                        "Duplicate — already in library "
                        f"({old_q[0]}/{old_q[1]}). Skipped.\n\n{tag_preview(tags)}",
                    )
                    return
                unlink_quiet(existing.local)
                if existing.sidecar_path:
                    unlink_quiet(Path(existing.sidecar_path))
                await _save_and_upload(
                    ctx,
                    job,
                    tmp,
                    tags,
                    identity,
                    kind="library",
                    replace_id=existing.id,
                    old_drive_id=existing.drive_file_id,
                    replaced=existing.status == "uploaded" and new_q > old_q,
                    old_q=old_q,
                    new_q=new_q,
                )
                return

        await _save_and_upload(ctx, job, tmp, tags, identity, kind=kind)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _save_and_upload(
    ctx: Ctx,
    job: Job,
    tmp: Path,
    tags: TagSet,
    identity: Identity,
    *,
    kind: str,
    replace_id: int | None = None,
    old_drive_id: str | None = None,
    replaced: bool = False,
    old_q: tuple[int, int] | None = None,
    new_q: tuple[int, int] | None = None,
) -> None:
    settings = ctx.settings
    sidecar_path: Path | None = None
    if kind == "library":
        relative = library_relative(job.topic_name, tags)
        dest = settings.library_root / relative
        root_id = settings.gdrive_folder_id
    else:
        relative = review_relative(job.file_name)
        dest = settings.review_root / relative
        root_id = settings.gdrive_review_folder_id

    dest = await asyncio.to_thread(place_file, tmp, dest)

    if kind == "review":
        payload = {
            "confidence": identity.confidence,
            "source": identity.source,
            "acoustid": identity.acoustid,
            "acoustid_score": identity.acoustid_score,
            "mb_recording_id": identity.mb_recording_id,
            "topic": job.topic_name,
            "proposed": asdict(tags),
            "candidates": [asdict(c) for c in identity.candidates],
            "original_filename": job.file_name,
        }
        sidecar_path = dest.with_suffix(".json")
        await asyncio.to_thread(write_sidecar, sidecar_path, payload)

    if replace_id is not None:
        ctx.catalog.update_quality_and_local(
            replace_id,
            local_path=str(dest),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            relative_path=relative.as_posix(),
            bit_depth=identity.bit_depth,
            sample_rate=identity.sample_rate,
            title=tags.title,
            artist=tags.artist,
            album=tags.album,
            acoustid=identity.acoustid,
        )
        track_id = replace_id
    else:
        track_id = ctx.catalog.insert_pending(
            kind=kind,
            mb_recording_id=identity.mb_recording_id,
            acoustid=identity.acoustid,
            local_path=str(dest),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            relative_path=relative.as_posix(),
            bit_depth=identity.bit_depth,
            sample_rate=identity.sample_rate,
            title=tags.title,
            artist=tags.artist,
            album=tags.album,
        )

    await edit_status(ctx, job, f"Uploading to Drive…\n\n{tag_preview(tags)}")
    try:
        file_id, url = await asyncio.to_thread(
            ctx.drive.upload_with_retry,
            dest,
            root_id,
            relative,
            "audio/flac",
        )
        if sidecar_path and sidecar_path.exists():
            await asyncio.to_thread(
                ctx.drive.upload_with_retry,
                sidecar_path,
                root_id,
                relative.with_suffix(".json"),
                "application/json",
            )
        ctx.catalog.mark_uploaded(track_id, file_id, url)
        if old_drive_id and old_drive_id != file_id:
            await asyncio.to_thread(ctx.drive.delete_file, old_drive_id)
    except Exception as exc:
        ctx.catalog.mark_failed(track_id, str(exc))
        await alert_general(
            ctx,
            "<b>Drive upload failed</b>\n"
            f"File: {html_esc(job.file_name)}\n"
            f"Title: {html_esc(tags.title)}\n"
            f"Local: <code>{html_esc(dest)}</code>\n"
            f"Error: {html_esc(exc)}",
            fallback_thread_id=job.thread_id,
        )
        await edit_status(
            ctx,
            job,
            f"Tagged, but Drive upload failed. Kept locally.\n\n{tag_preview(tags)}\n\n"
            f"<code>{html_esc(dest)}</code>",
        )
        return

    dest_label = "library" if kind == "library" else "review"
    extra = ""
    if replaced and old_q and new_q:
        extra = f"\nReplaced lower-quality copy ({old_q[0]}/{old_q[1]} → {new_q[0]}/{new_q[1]})."
    link = f'\nDrive: <a href="{html_esc(url)}">open</a>' if url else ""
    await edit_status(
        ctx,
        job,
        f"Saved ({dest_label}, {identity.confidence} confidence).{extra}\n\n"
        f"{tag_preview(tags)}{link}\n"
        f"<code>{html_esc(relative.as_posix())}</code>",
    )
