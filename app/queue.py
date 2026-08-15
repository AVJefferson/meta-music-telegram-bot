from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.cleanup import alert_general
from app.enrich import enrich
from app.identify import identify_file, identity_from_mbid
from app.library import library_relative, place_file, review_relative, unlink_quiet, write_sidecar
from app.models import (
    Candidate,
    Ctx,
    Identity,
    Job,
    PendingReview,
    TagHints,
    TagSet,
    identity_from_dict,
    tagset_from_dict,
)
from app.review_ui import (
    ReviewStates,
    conflict_keyboard,
    empty_markup,
    field_keyboard,
    format_conflict,
    format_field_prompt,
    format_summary,
    parse_callback,
    review_keyboard,
    set_field,
)
from app.songlog import apply_chosen, merge_enrichment, render_songlog, seed_report
from app.tags import hints_to_tagset, identity_to_tags, read_cover, read_hints, write_tags
from app.util import html_esc, sanitize_filename

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


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(text: str | None, default: object):
    if not text:
        return default
    return json.loads(text)


def _expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")


def _job_from_pending(row: PendingReview) -> Job:
    return Job(
        chat_id=row.chat_id,
        thread_id=row.thread_id,
        topic_name=row.topic_name,
        file_id="",
        file_name=row.file_name,
        status_message_id=row.status_message_id or 0,
    )


async def edit_status(ctx: Ctx, job: Job, text: str, markup=None) -> int:
    markup = empty_markup() if markup is None else markup
    if job.status_message_id:
        try:
            await ctx.bot.edit_message_text(
                text,
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )
            return job.status_message_id
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return job.status_message_id
            log.debug("status edit failed: %s", exc)
        except Exception:
            log.debug("status edit failed", exc_info=True)
    try:
        kwargs: dict = {
            "chat_id": job.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": markup,
        }
        if job.thread_id:
            kwargs["message_thread_id"] = job.thread_id
        sent = await ctx.bot.send_message(**kwargs)
        job.status_message_id = sent.message_id
        return sent.message_id
    except Exception:
        log.debug("status send failed", exc_info=True)
        return job.status_message_id


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


def _build_report(hints: TagHints, identity: Identity, enrichment, tags: TagSet) -> dict:
    report = seed_report(hints, identity)
    report = merge_enrichment(report, enrichment)
    return apply_chosen(report, tags, identity)


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
        report = _build_report(hints, identity, enrichment, tags)

        if identity.confidence == "high" and identity.mb_recording_id:
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
                await _commit_upload(
                    ctx,
                    job=job,
                    local=tmp,
                    tags=tags,
                    identity=identity,
                    kind="library",
                    source_report=report,
                    replace_id=existing.id,
                    old_drive_id=existing.drive_file_id,
                    replaced=existing.status == "uploaded" and new_q > old_q,
                    old_q=old_q,
                    new_q=new_q,
                )
                return

        if identity.confidence == "low":
            await start_tag_review(ctx, job, tmp, hints, tags, identity, report)
            return

        await _commit_upload(
            ctx,
            job=job,
            local=tmp,
            tags=tags,
            identity=identity,
            kind="library",
            source_report=report,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def start_tag_review(
    ctx: Ctx,
    job: Job,
    tmp: Path,
    hints: TagHints,
    tags: TagSet,
    identity: Identity,
    report: dict,
) -> None:
    pending_dir = ctx.settings.pending_root / str(uuid.uuid4())
    pending_dir.mkdir(parents=True, exist_ok=True)
    dest = pending_dir / (sanitize_filename(Path(job.file_name).stem) + ".flac")
    dest = await asyncio.to_thread(place_file, tmp, dest)
    original = hints_to_tagset(hints)
    pending_id = ctx.catalog.insert_pending_review(
        phase="tags",
        local_path=str(dest),
        sidecar_path=None,
        relative_path=None,
        kind="library",
        original_json=_dumps(asdict(original)),
        recommended_json=_dumps(asdict(tags)),
        working_json=_dumps(asdict(tags)),
        candidates_json=_dumps([asdict(c) for c in identity.candidates]),
        identity_json=_dumps(asdict(identity)),
        source_report_json=_dumps(report),
        chat_id=job.chat_id,
        thread_id=job.thread_id,
        status_message_id=job.status_message_id,
        topic_name=job.topic_name,
        file_name=job.file_name,
        expires_at=_expires_at(),
    )
    text = format_summary(original, tags, tags, reason=identity.confidence_reason)
    markup = review_keyboard(pending_id, identity.candidates)
    status_id = await edit_status(ctx, job, text, markup)
    ctx.catalog.update_pending_review(pending_id, status_message_id=status_id)
    log.info("review waiting id=%s file=%s reason=%s", pending_id, job.file_name, identity.confidence_reason)


def _sidecar_payload(job: Job, tags: TagSet, identity: Identity) -> dict:
    return {
        "confidence": identity.confidence,
        "confidence_reason": identity.confidence_reason,
        "source": identity.source,
        "acoustid": identity.acoustid,
        "acoustid_score": identity.acoustid_score,
        "mb_recording_id": identity.mb_recording_id,
        "topic": job.topic_name,
        "proposed": asdict(tags),
        "candidates": [asdict(c) for c in identity.candidates],
        "original_filename": job.file_name,
    }


async def _commit_upload(
    ctx: Ctx,
    *,
    job: Job,
    local: Path,
    tags: TagSet,
    identity: Identity,
    kind: str,
    source_report: dict,
    replace_id: int | None = None,
    old_drive_id: str | None = None,
    replaced: bool = False,
    old_q: tuple[int, int] | None = None,
    new_q: tuple[int, int] | None = None,
    pending_id: int | None = None,
    conflict_action: str | None = None,
    track_id: int | None = None,
) -> Path | None:
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

    if local.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = await asyncio.to_thread(place_file, local, dest)

    if kind == "review":
        sidecar_path = dest.with_suffix(".json")
        await asyncio.to_thread(write_sidecar, sidecar_path, _sidecar_payload(job, tags, identity))

    parent_id = await asyncio.to_thread(ctx.drive.ensure_parent, root_id, relative)
    filename = relative.name
    found = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent_id, filename)
    conflict_dicts = [
        {"id": c.id, "name": c.name, "size": c.size, "modified": c.modified}
        for c in found
    ]
    catalog_row = ctx.catalog.find_uploaded_by_relative(relative.as_posix())
    catalog_note = False
    if not conflict_dicts and catalog_row:
        catalog_note = True
        meta = None
        if catalog_row.drive_file_id:
            meta = await asyncio.to_thread(ctx.drive.get_child_meta, catalog_row.drive_file_id)
        if meta:
            conflict_dicts = [
                {"id": meta.id, "name": meta.name, "size": meta.size, "modified": meta.modified}
            ]
        else:
            conflict_dicts = [
                {
                    "id": catalog_row.drive_file_id or "",
                    "name": filename,
                    "size": None,
                    "modified": None,
                }
            ]

    if conflict_action == "skip":
        skipped_id = track_id or ctx.catalog.insert_pending(
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
            status="skipped",
        )
        if pending_id is not None:
            ctx.catalog.update_pending_review(pending_id, status="skipped", track_id=skipped_id)
        await edit_status(ctx, job, f"Skipped Drive upload. Kept locally.\n\n{tag_preview(tags)}\n<code>{html_esc(dest)}</code>")
        log.info("drive skip track=%s path=%s", skipped_id, relative)
        return dest

    auto_keep = conflict_action == "auto_keep_both"
    if conflict_dicts and conflict_action is None and not auto_keep:
        await _hold_drive_conflict(
            ctx,
            job=job,
            dest=dest,
            sidecar_path=sidecar_path,
            relative=relative,
            kind=kind,
            tags=tags,
            identity=identity,
            source_report=source_report,
            root_id=root_id,
            conflict_dicts=conflict_dicts,
            catalog_note=catalog_note,
            replace_id=replace_id,
            old_drive_id=old_drive_id,
            pending_id=pending_id,
            track_id=track_id,
        )
        return dest

    replace_file_id = None
    if conflict_action == "replace" and conflict_dicts:
        replace_file_id = conflict_dicts[0].get("id") or None
    elif auto_keep and conflict_dicts:
        filename = await asyncio.to_thread(ctx.drive.unused_name, parent_id, filename)
        relative = relative.with_name(filename)
        dest_new = dest.with_name(filename)
        if dest_new != dest:
            dest_new.parent.mkdir(parents=True, exist_ok=True)
            if dest_new.exists():
                dest_new.unlink()
            dest.rename(dest_new)
            dest = dest_new
            if sidecar_path:
                new_side = dest.with_suffix(".json")
                if sidecar_path.exists():
                    sidecar_path.rename(new_side)
                sidecar_path = new_side
    elif conflict_action == "keep_both":
        filename = await asyncio.to_thread(ctx.drive.unused_name, parent_id, filename)
        relative = relative.with_name(filename)
        dest_new = dest.with_name(filename)
        if dest_new != dest:
            if dest_new.exists():
                dest_new.unlink()
            dest.rename(dest_new)
            dest = dest_new
            if sidecar_path:
                new_side = dest.with_suffix(".json")
                if sidecar_path.exists():
                    sidecar_path.rename(new_side)
                sidecar_path = new_side
        replace_id = None
        old_drive_id = None
        track_id = None

    if replace_id is not None and conflict_action != "skip":
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
    elif track_id is not None:
        ctx.catalog.update_track_paths(
            track_id,
            local_path=str(dest),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            relative_path=relative.as_posix(),
            title=tags.title,
            artist=tags.artist,
            album=tags.album,
            status="pending",
        )
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

    if pending_id is not None:
        ctx.catalog.update_pending_review(
            pending_id,
            local_path=str(dest),
            sidecar_path=str(sidecar_path) if sidecar_path else None,
            relative_path=relative.as_posix(),
            kind=kind,
            track_id=track_id,
            status="uploading",
        )

    await edit_status(ctx, job, f"Uploading to Drive…\n\n{tag_preview(tags)}")
    log.debug("drive upload path=%s parent=%s replace=%s", relative, parent_id, replace_file_id)
    try:
        if replace_file_id:
            file_id, url = await asyncio.to_thread(ctx.drive.replace_file, replace_file_id, dest, "audio/flac")
        else:
            file_id, url = await asyncio.to_thread(ctx.drive.create_file, dest, parent_id, filename, "audio/flac")
        await _upload_sidecars(
            ctx,
            parent_id=parent_id,
            dest=dest,
            sidecar_path=sidecar_path,
            source_report=source_report,
            tags=tags,
            identity=identity,
        )
        ctx.catalog.mark_uploaded(track_id, file_id, url)
        if old_drive_id and old_drive_id != file_id and conflict_action != "keep_both":
            await asyncio.to_thread(ctx.drive.delete_file, old_drive_id)
        if pending_id is not None:
            ctx.catalog.update_pending_review(pending_id, status="done")
    except Exception as exc:
        ctx.catalog.mark_failed(track_id, str(exc))
        if pending_id is not None:
            ctx.catalog.update_pending_review(pending_id, status="failed")
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
        return dest

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
    log.info("saved %s kind=%s confidence=%s path=%s", job.file_name, dest_label, identity.confidence, relative)
    return dest


async def _hold_drive_conflict(
    ctx: Ctx,
    *,
    job: Job,
    dest: Path,
    sidecar_path: Path | None,
    relative: Path,
    kind: str,
    tags: TagSet,
    identity: Identity,
    source_report: dict,
    root_id: str,
    conflict_dicts: list[dict],
    catalog_note: bool,
    replace_id: int | None,
    old_drive_id: str | None,
    pending_id: int | None,
    track_id: int | None,
) -> None:
    new_size = dest.stat().st_size if dest.exists() else None
    text = format_conflict(
        relative.name,
        conflict_dicts,
        bit_depth=identity.bit_depth,
        sample_rate=identity.sample_rate,
        new_size=new_size,
        catalog_note=catalog_note,
    )
    fields = dict(
        phase="drive",
        local_path=str(dest),
        sidecar_path=str(sidecar_path) if sidecar_path else None,
        relative_path=relative.as_posix(),
        kind=kind,
        working_json=_dumps(asdict(tags)),
        recommended_json=_dumps(asdict(tags)),
        identity_json=_dumps(asdict(identity)),
        source_report_json=_dumps(source_report),
        drive_conflicts_json=_dumps(conflict_dicts),
        drive_root_id=root_id,
        replace_id=replace_id,
        old_drive_id=old_drive_id,
        track_id=track_id,
    )
    if pending_id is None:
        pending_id = ctx.catalog.insert_pending_review(
            original_json=_dumps(asdict(tags)),
            candidates_json=_dumps([asdict(c) for c in identity.candidates]),
            chat_id=job.chat_id,
            thread_id=job.thread_id,
            status_message_id=job.status_message_id,
            topic_name=job.topic_name,
            file_name=job.file_name,
            expires_at=_expires_at(),
            **fields,
        )
    else:
        ctx.catalog.update_pending_review(pending_id, status="waiting", **fields)
    markup = conflict_keyboard(pending_id)
    status_id = await edit_status(ctx, job, text, markup)
    ctx.catalog.update_pending_review(pending_id, status_message_id=status_id)
    log.info("drive conflict waiting id=%s file=%s", pending_id, relative.name)


async def _upload_sidecars(
    ctx: Ctx,
    *,
    parent_id: str,
    dest: Path,
    sidecar_path: Path | None,
    source_report: dict,
    tags: TagSet,
    identity: Identity,
) -> None:
    json_name = dest.with_suffix(".json").name
    log_name = dest.with_suffix(".log").name
    if sidecar_path and sidecar_path.exists():
        json_hits = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent_id, json_name)
        try:
            if json_hits:
                await asyncio.to_thread(ctx.drive.replace_file, json_hits[0].id, sidecar_path, "application/json")
            else:
                await asyncio.to_thread(
                    ctx.drive.create_file, sidecar_path, parent_id, json_name, "application/json"
                )
        except Exception:
            log.warning("sidecar json upload failed", exc_info=True)
    if not ctx.settings.enable_log_per_music_file:
        return
    report = apply_chosen(source_report, tags, identity)
    payload = render_songlog(report).encode("utf-8")
    log_hits = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent_id, log_name)
    replace_log_id = log_hits[0].id if log_hits else None
    try:
        await asyncio.to_thread(
            ctx.drive.upload_bytes,
            payload,
            parent_id,
            log_name,
            "text/plain",
            replace_id=replace_log_id,
        )
    except Exception:
        log.warning("song log upload failed", exc_info=True)


def _load_pending_state(row: PendingReview) -> tuple[dict, dict, dict, Identity, dict, list]:
    original = _loads(row.original_json, {})
    recommended = _loads(row.recommended_json, {})
    working = _loads(row.working_json, {})
    identity = identity_from_dict(_loads(row.identity_json, {}))
    report = _loads(row.source_report_json, {})
    candidates = _loads(row.candidates_json, [])
    return original, recommended, working, identity, report, candidates


async def _refresh_tag_ui(ctx: Ctx, row: PendingReview) -> None:
    original, recommended, working, identity, _report, candidates = _load_pending_state(row)
    job = _job_from_pending(row)
    text = format_summary(original, recommended, working, reason=identity.confidence_reason)
    cand_objs = [
        Candidate(
            mb_recording_id=c.get("mb_recording_id") or "",
            title=c.get("title") or "",
            artist=c.get("artist") or "",
            score=c.get("score"),
        )
        if isinstance(c, dict)
        else c
        for c in candidates
    ]
    status_id = await edit_status(ctx, job, text, review_keyboard(row.id, cand_objs))
    if status_id != row.status_message_id:
        ctx.catalog.update_pending_review(row.id, status_message_id=status_id)


async def handle_pending_callback(callback: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    if callback.message and callback.message.chat.id != ctx.settings.allowed_chat_id:
        await callback.answer()
        return
    action = parse_callback(callback.data)
    if action is None:
        await callback.answer()
        return
    row = ctx.catalog.get_pending_review(action.pending_id)
    if row is None or row.status not in {"waiting", "expiring"}:
        await callback.answer("Already handled.")
        return
    if row.status != "waiting":
        await callback.answer("Expired.")
        return
    await callback.answer()
    log.debug("fsm callback id=%s op=%s field=%s", row.id, action.op, action.field)

    if action.op == "ok":
        await state.clear()
        await _apply_confirm(ctx, row, kind="library")
        return
    if action.op == "rev":
        await state.clear()
        await _apply_confirm(ctx, row, kind="review")
        return
    if action.op == "back":
        await state.clear()
        await _refresh_tag_ui(ctx, row)
        return
    if action.op == "cand" and action.index is not None:
        await state.clear()
        await _apply_candidate(ctx, row, action.index)
        return
    if action.op == "edit" and action.field:
        original, recommended, _working, _identity, _report, _cands = _load_pending_state(row)
        await state.set_state(ReviewStates.waiting_custom)
        await state.update_data(pending_id=row.id, field=action.field)
        job = _job_from_pending(row)
        text = format_field_prompt(action.field, original, recommended)
        status_id = await edit_status(ctx, job, text, field_keyboard(row.id, action.field))
        ctx.catalog.update_pending_review(row.id, status_message_id=status_id)
        return
    if action.op in {"use_file", "use_rec"} and action.field:
        await state.clear()
        original, recommended, working, _identity, _report, _cands = _load_pending_state(row)
        source = original if action.op == "use_file" else recommended
        value = source.get("date") if action.field == "year" else source.get(action.field) or ""
        if action.field == "year":
            value = source.get("date") or source.get("year") or ""
        working = set_field(working, action.field, str(value or ""))
        ctx.catalog.update_pending_review(row.id, working_json=_dumps(working))
        row = ctx.catalog.get_pending_review(row.id)
        if row:
            await _refresh_tag_ui(ctx, row)
        return
    if action.op in {"drive_replace", "drive_keep", "drive_skip"}:
        mapping = {"drive_replace": "replace", "drive_keep": "keep_both", "drive_skip": "skip"}
        await _apply_drive_choice(ctx, row, mapping[action.op])


async def handle_custom_tag(message: Message, ctx: Ctx, state: FSMContext) -> None:
    if message.chat.id != ctx.settings.allowed_chat_id:
        return
    data = await state.get_data()
    pending_id = data.get("pending_id")
    field = data.get("field")
    if not pending_id or not field:
        await state.clear()
        return
    row = ctx.catalog.get_pending_review(int(pending_id))
    if row is None or row.status != "waiting":
        await state.clear()
        return
    working = _loads(row.working_json, {})
    working = set_field(working, str(field), (message.text or "").strip())
    ctx.catalog.update_pending_review(row.id, working_json=_dumps(working))
    await state.clear()
    row = ctx.catalog.get_pending_review(row.id)
    if row:
        await _refresh_tag_ui(ctx, row)
    try:
        await message.delete()
    except Exception:
        pass


async def _apply_confirm(ctx: Ctx, row: PendingReview, *, kind: str) -> None:
    original, recommended, working, identity, report, _cands = _load_pending_state(row)
    tags = tagset_from_dict(working)
    local = Path(row.local_path)
    if not local.exists():
        await edit_status(ctx, _job_from_pending(row), "Local file missing. Cannot continue.")
        ctx.catalog.update_pending_review(row.id, status="failed")
        return
    cover, mime = await asyncio.to_thread(read_cover, local)
    await asyncio.to_thread(write_tags, local, tags, cover, mime)
    report = apply_chosen(report, tags, identity)

    job = _job_from_pending(row)
    if kind == "library" and identity.mb_recording_id:
        existing = ctx.catalog.find_library_by_mbid(identity.mb_recording_id)
        if existing and existing.id != row.replace_id:
            new_q = quality(identity.bit_depth, identity.sample_rate)
            old_q = quality(existing.bit_depth, existing.sample_rate)
            if existing.status == "uploaded" and new_q <= old_q:
                ctx.catalog.update_pending_review(row.id, status="done")
                await edit_status(
                    ctx,
                    job,
                    "Duplicate — already in library "
                    f"({old_q[0]}/{old_q[1]}). Skipped.\n\n{tag_preview(tags)}",
                )
                return
            await _commit_upload(
                ctx,
                job=job,
                local=local,
                tags=tags,
                identity=identity,
                kind="library",
                source_report=report,
                replace_id=existing.id,
                old_drive_id=existing.drive_file_id,
                replaced=existing.status == "uploaded" and new_q > old_q,
                old_q=old_q,
                new_q=new_q,
                pending_id=row.id,
            )
            return

    await _commit_upload(
        ctx,
        job=job,
        local=local,
        tags=tags,
        identity=identity,
        kind=kind,
        source_report=report,
        replace_id=row.replace_id,
        old_drive_id=row.old_drive_id,
        pending_id=row.id,
        track_id=row.track_id,
    )


async def _apply_candidate(ctx: Ctx, row: PendingReview, index: int) -> None:
    _original, _recommended, _working, identity, report, candidates = _load_pending_state(row)
    if index < 0 or index >= len(candidates):
        return
    cand = candidates[index]
    mbid = cand.get("mb_recording_id") if isinstance(cand, dict) else cand.mb_recording_id
    if not mbid:
        return
    job = _job_from_pending(row)
    await edit_status(ctx, job, "Loading that recording…")
    try:
        new_identity = await asyncio.to_thread(
            identity_from_mbid,
            ctx.mb,
            mbid,
            duration=identity.duration,
            bit_depth=identity.bit_depth,
            sample_rate=identity.sample_rate,
            acoustid=identity.acoustid,
            acoustid_score=identity.acoustid_score,
            source="candidate",
        )
    except Exception:
        log.exception("candidate lookup failed")
        await edit_status(ctx, job, "Could not load that candidate. Try another.")
        row = ctx.catalog.get_pending_review(row.id)
        if row:
            await _refresh_tag_ui(ctx, row)
        return
    new_identity.candidates = identity.candidates
    new_identity.confidence = "low"
    new_identity.confidence_reason = identity.confidence_reason
    new_identity.source_report = {**identity.source_report, **new_identity.source_report}
    enrichment = await enrich(
        ctx.http,
        new_identity,
        ctx.genre,
        ctx.settings.lastfm_api_key,
        row.topic_name,
    )
    tags = identity_to_tags(new_identity, enrichment)
    local = Path(row.local_path)
    await asyncio.to_thread(write_tags, local, tags, enrichment.cover, enrichment.cover_mime)
    report = merge_enrichment(report, enrichment)
    report = apply_chosen(report, tags, new_identity)
    ctx.catalog.update_pending_review(
        row.id,
        recommended_json=_dumps(asdict(tags)),
        working_json=_dumps(asdict(tags)),
        identity_json=_dumps(asdict(new_identity)),
        source_report_json=_dumps(report),
    )
    row = ctx.catalog.get_pending_review(row.id)
    if row:
        await _refresh_tag_ui(ctx, row)


async def _apply_drive_choice(ctx: Ctx, row: PendingReview, action: str) -> None:
    working = tagset_from_dict(_loads(row.working_json, {}))
    identity = identity_from_dict(_loads(row.identity_json, {}))
    report = _loads(row.source_report_json, {})
    local = Path(row.local_path)
    job = _job_from_pending(row)
    await _commit_upload(
        ctx,
        job=job,
        local=local,
        tags=working,
        identity=identity,
        kind=row.kind,
        source_report=report,
        replace_id=row.replace_id,
        old_drive_id=row.old_drive_id,
        pending_id=row.id,
        conflict_action=action,
        track_id=row.track_id,
    )


async def expire_pending(ctx: Ctx) -> None:
    rows = ctx.catalog.claim_expired_pending()
    if not rows:
        return
    log.info("expiring %s pending review(s)", len(rows))
    for row in rows:
        try:
            job = _job_from_pending(row)
            if row.phase == "tags":
                working = tagset_from_dict(_loads(row.working_json, {}))
                identity = identity_from_dict(_loads(row.identity_json, {}))
                report = _loads(row.source_report_json, {})
                local = Path(row.local_path)
                if not local.exists():
                    ctx.catalog.update_pending_review(row.id, status="failed")
                    continue
                cover, mime = await asyncio.to_thread(read_cover, local)
                await asyncio.to_thread(write_tags, local, working, cover, mime)
                await edit_status(ctx, job, "Timed out (24h). Sending to review folder…")
                await _commit_upload(
                    ctx,
                    job=job,
                    local=local,
                    tags=working,
                    identity=identity,
                    kind="review",
                    source_report=apply_chosen(report, working, identity),
                    pending_id=row.id,
                    conflict_action="auto_keep_both",
                    track_id=row.track_id,
                )
            elif row.phase == "drive":
                working = tagset_from_dict(_loads(row.working_json, {}))
                identity = identity_from_dict(_loads(row.identity_json, {}))
                report = _loads(row.source_report_json, {})
                await edit_status(ctx, job, "Timed out (24h). Skipping Drive upload…")
                await _commit_upload(
                    ctx,
                    job=job,
                    local=Path(row.local_path),
                    tags=working,
                    identity=identity,
                    kind=row.kind,
                    source_report=report,
                    replace_id=row.replace_id,
                    old_drive_id=row.old_drive_id,
                    pending_id=row.id,
                    conflict_action="skip",
                    track_id=row.track_id,
                )
            else:
                ctx.catalog.update_pending_review(row.id, status="expired")
        except Exception:
            log.exception("expire pending id=%s failed", row.id)
            ctx.catalog.update_pending_review(row.id, status="failed")
