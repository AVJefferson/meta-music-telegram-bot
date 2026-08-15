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
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto

from app.cleanup import alert_general
from app.covers import (
    CoverHit,
    CoverOption,
    cache_album_cover,
    cover_album_key,
    cover_identity,
    existing_album_cover,
    is_shareable_album,
    list_cover_candidates,
    resolve_album_cover,
    upload_album_cover_if_missing,
)
from app.enrich import enrich
from app.identify import identify_file, identity_from_mbid
from app.library import library_relative, place_file, review_relative, unlink_quiet, write_sidecar
from app.models import (
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
    bulk_choice,
    conflict_keyboard,
    cover_keyboard,
    empty_markup,
    format_conflict,
    format_cover_prompt,
    format_summary,
    parse_callback,
    review_keyboard,
    toggle_working_field,
)
from app.songlog import apply_chosen, merge_enrichment, render_songlog, seed_report
from app.tags import hints_to_tagset, identity_to_tags, read_cover, read_hints, write_tags
from app.util import html_esc, sanitize_filename

log = logging.getLogger(__name__)


def quality(bit_depth: int | None, sample_rate: int | None) -> tuple[int, int]:
    return (bit_depth or 0, sample_rate or 0)


def tag_preview(tags: TagSet) -> str:
    lyrics = "present" if tags.lyrics else "none"
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
        private=row.chat_id > 0,
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
            if job.source_pending_id and not ctx.catalog.transition_pending(
                job.source_pending_id, "queued", "processing"
            ):
                log.info("skipping already claimed private job id=%s", job.source_pending_id)
                continue
            await process_job(job, ctx)
            if job.source_pending_id:
                ctx.catalog.transition_pending(
                    job.source_pending_id, "processing", "done"
                )
                if job.local_path:
                    source = Path(job.local_path)
                    unlink_quiet(source)
                    try:
                        source.parent.rmdir()
                    except OSError:
                        pass
        except Exception:
            log.exception("job failed for %s", job.file_name)
            if job.source_pending_id:
                ctx.catalog.update_pending_review(job.source_pending_id, status="failed")
            try:
                await edit_status(ctx, job, f"Failed processing <code>{html_esc(job.file_name)}</code>. Check logs.")
            except Exception:
                pass
        finally:
            queue.task_done()


async def recover_interrupted(ctx: Ctx, jobs: asyncio.Queue[Job]) -> None:
    rows = ctx.catalog.list_pending_by_status(
        "queued", "processing", "uploading", "expiring", "cleanup_pending"
    )
    for row in rows:
        try:
            if row.status == "cleanup_pending":
                if await _delete_promoted_review_source(ctx, row):
                    ctx.catalog.update_pending_review(row.id, status="done")
                continue
            if row.phase == "dm_topic" and row.status in {"queued", "processing"}:
                ctx.catalog.update_pending_review(row.id, status="queued")
                await jobs.put(
                    Job(
                        chat_id=row.chat_id,
                        thread_id=None,
                        topic_name=row.topic_name,
                        file_id="",
                        file_name=row.file_name,
                        status_message_id=row.status_message_id,
                        local_path=row.local_path,
                        private=True,
                        source_pending_id=row.id,
                    )
                )
                continue
            if row.status == "uploading":
                ctx.catalog.update_pending_review(row.id, status="waiting", phase="drive")
                refreshed = ctx.catalog.get_pending_review(row.id)
                if refreshed:
                    await _apply_drive_choice(ctx, refreshed, "replace")
                continue
            ctx.catalog.update_pending_review(row.id, status="waiting")
        except Exception:
            log.exception("pending recovery failed id=%s status=%s", row.id, row.status)


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
        if job.local_path:
            await asyncio.to_thread(shutil.copy2, job.local_path, tmp)
        else:
            file = await ctx.bot.get_file(job.file_id)
            await ctx.bot.download(file, destination=tmp)

        hints = await asyncio.to_thread(read_hints, tmp, job.file_name)
        file_cover = await asyncio.to_thread(read_cover, tmp)
        await edit_status(ctx, job, "Fingerprinting / identifying…")
        identity = await asyncio.to_thread(identify_file, tmp, hints, settings, ctx.mb)

        await edit_status(ctx, job, "Fetching lyrics, genre…")
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

        await asyncio.to_thread(write_tags, tmp, tags, file_cover[0], file_cover[1])
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
                await _library_commit_with_cover(
                    ctx,
                    job=job,
                    local=tmp,
                    tags=tags,
                    identity=identity,
                    report=report,
                    replace_id=existing.id,
                    old_drive_id=existing.drive_file_id,
                    replaced=existing.status == "uploaded" and new_q > old_q,
                    old_q=old_q,
                    new_q=new_q,
                    file_cover=file_cover,
                    pending_id=job.source_pending_id,
                )
                return

        if identity.confidence == "low":
            await start_tag_review(ctx, job, tmp, hints, tags, identity, report)
            return

        await _library_commit_with_cover(
            ctx,
            job=job,
            local=tmp,
            tags=tags,
            identity=identity,
            report=report,
            file_cover=file_cover,
            pending_id=job.source_pending_id,
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
    if job.source_pending_id:
        pending_id = job.source_pending_id
        ctx.catalog.update_pending_review(
            pending_id,
            phase="tags",
            status="waiting",
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
            topic_name=job.topic_name,
            thread_id=job.thread_id,
            file_name=job.file_name,
            expires_at=_expires_at(),
        )
    else:
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
    if job.private:
        ctx.catalog.update_pending_review(pending_id, phase="edit:0")
        from app.private_ui import show_editor_prompt

        row = ctx.catalog.get_pending_review(pending_id)
        if row:
            await show_editor_prompt(ctx, row)
        status_id = job.status_message_id
    else:
        text = format_summary(original, tags, tags, reason=identity.confidence_reason)
        markup = review_keyboard(pending_id, original, tags, tags)
        status_id = await edit_status(ctx, job, text, markup)
    ctx.catalog.update_pending_review(pending_id, status_message_id=status_id)
    log.info("review waiting id=%s file=%s reason=%s", pending_id, job.file_name, identity.confidence_reason)


def _set_cover_report(report: dict, source: str, caa_release: str | None = None) -> None:
    report["cover_source"] = source
    caa = report.get("coverartarchive") or {}
    caa["used"] = source == "caa"
    caa["release"] = caa_release or ""
    report["coverartarchive"] = caa


def _cover_picker_meta(report: dict) -> dict:
    raw = report.get("cover_picker")
    return raw if isinstance(raw, dict) else {}


def _find_cover_leader(ctx: Ctx, album_key: str) -> PendingReview | None:
    for row in ctx.catalog.list_waiting_by_phase("cover"):
        picker = _cover_picker_meta(_loads(row.source_report_json, {}))
        if picker.get("album_key") == album_key and picker.get("role") == "leader":
            return row
    return None


async def _park_pending_flac(ctx: Ctx, job: Job, local: Path) -> Path:
    pending_root = ctx.settings.pending_root.resolve()
    try:
        local.resolve().relative_to(pending_root)
        return local
    except ValueError:
        pass
    pending_dir = ctx.settings.pending_root / str(uuid.uuid4())
    pending_dir.mkdir(parents=True, exist_ok=True)
    dest = pending_dir / (sanitize_filename(Path(job.file_name).stem) + ".flac")
    return await asyncio.to_thread(place_file, local, dest)


async def _embed_cover(local: Path, tags: TagSet, data: bytes | None, mime: str | None) -> None:
    await asyncio.to_thread(write_tags, local, tags, data, mime)


async def _apply_resolved_cover(
    local: Path,
    tags: TagSet,
    report: dict,
    hit: CoverHit,
) -> None:
    if hit.data:
        await _embed_cover(local, tags, hit.data, hit.mime)
        _set_cover_report(report, hit.source, hit.caa_release)


def _write_cover_option_files(options: list[CoverOption], pending_dir: Path) -> list[dict]:
    meta: list[dict] = []
    for index, option in enumerate(options):
        name = f"cover-{index}.jpg"
        (pending_dir / name).write_bytes(option.data)
        meta.append(
            {
                "path": name,
                "source": option.source,
                "label": option.label,
                "digest": option.digest,
                "caa_release": option.caa_release or "",
                "url": option.url or "",
            }
        )
    return meta


def _read_cover_option(local: Path, option: dict) -> tuple[bytes, str, str, str | None] | None:
    path = local.parent / str(option.get("path") or "")
    if not path.is_file():
        return None
    data = path.read_bytes()
    if not data:
        return None
    source = str(option.get("source") or "none")
    caa = str(option.get("caa_release") or "") or None
    return data, "image/jpeg", source, caa


_API_COVER_ROOT = Path("/var/lib/telegram-bot-api/cover-picker")


def _cleanup_staged_covers(album_key: str) -> None:
    if not album_key:
        return
    path = _API_COVER_ROOT / album_key
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _cleanup_cover_option_files(local: Path, options: list, album_key: str = "") -> None:
    for option in options:
        if isinstance(option, dict):
            unlink_quiet(local.parent / str(option.get("path") or ""))
    _cleanup_staged_covers(album_key)


def _cover_caption(index: int, option: dict, album: str, albumartist: str) -> str:
    label = str(option.get("label") or f"{index + 1}")
    caption = f"{index + 1}. {label}"
    if index == 0:
        caption = f"Pick cover\n{albumartist} — {album}\n{caption}"
    return caption[:1024]


def _gallery_kwargs(job: Job) -> dict:
    kwargs: dict = {"chat_id": job.chat_id}
    if job.thread_id:
        kwargs["message_thread_id"] = job.thread_id
    return kwargs


async def _send_one_cover(
    ctx: Ctx,
    path: Path,
    kwargs: dict,
    url: str = "",
) -> int | None:
    payload = path.read_bytes()
    photo = url if url.startswith("https://") or url.startswith("http://") else None
    if photo:
        try:
            sent = await ctx.bot.send_photo(photo=photo, **kwargs)
            log.info("cover sent via photo-url %s", path.name)
            return sent.message_id
        except Exception as exc:
            log.warning("cover send_photo url failed %s: %s", path.name, exc)
    try:
        sent = await ctx.bot.send_photo(
            photo=BufferedInputFile(payload, filename=path.name), **kwargs
        )
        log.info("cover sent via photo-bytes %s", path.name)
        return sent.message_id
    except Exception as exc:
        log.warning("cover send_photo failed %s: %s", path.name, exc)
    try:
        sent = await ctx.bot.send_document(
            document=BufferedInputFile(payload, filename=path.name), **kwargs
        )
        log.info("cover sent via doc-bytes %s", path.name)
        return sent.message_id
    except Exception as exc:
        log.warning("cover send_document failed %s: %s", path.name, exc)
    if url:
        text = str(kwargs.get("caption") or "")
        msg: dict = {
            "chat_id": kwargs["chat_id"],
            "text": f"{text}\n{url}".strip(),
            "disable_web_page_preview": False,
        }
        if kwargs.get("message_thread_id"):
            msg["message_thread_id"] = kwargs["message_thread_id"]
        try:
            sent = await ctx.bot.send_message(**msg)
            log.info("cover sent via link preview %s", path.name)
            return sent.message_id
        except Exception as exc:
            log.warning("cover link preview failed %s: %s", path.name, exc)
    return None


async def _send_cover_gallery(
    ctx: Ctx,
    job: Job,
    pending_dir: Path,
    options: list[dict],
    album: str,
    albumartist: str,
    album_key: str,
) -> list[int]:
    del album_key
    ready: list[tuple[int, dict, Path]] = []
    for index, option in enumerate(options):
        src = pending_dir / str(option.get("path") or "")
        if not src.is_file():
            log.warning("cover option file missing %s", src)
            continue
        ready.append((index, option, src))
    if not ready:
        log.warning("cover gallery empty album=%s options=%s", album, len(options))
        return []

    base = _gallery_kwargs(job)
    if len(ready) >= 2:
        media = []
        for index, option, path in ready:
            item: dict = {
                "media": BufferedInputFile(path.read_bytes(), filename=path.name),
                "caption": _cover_caption(index, option, album, albumartist),
            }
            media.append(InputMediaPhoto(**item))
        try:
            sent = await ctx.bot.send_media_group(media=media, **base)
            ids = [msg.message_id for msg in sent]
            log.info("cover sent via media_group album=%s photos=%s", album, len(ids))
            return ids
        except Exception as exc:
            log.warning("cover send_media_group failed: %s", exc)

    ids: list[int] = []
    for index, option, path in ready:
        kwargs = dict(base)
        kwargs["caption"] = _cover_caption(index, option, album, albumartist)
        message_id = await _send_one_cover(ctx, path, kwargs, url=str(option.get("url") or ""))
        if message_id is None:
            log.warning("cover option %s not sent", path.name)
            continue
        ids.append(message_id)
    if not ids:
        log.warning("cover gallery empty album=%s options=%s", album, len(options))
    return ids


async def _ensure_leader_gallery(
    ctx: Ctx,
    leader: PendingReview,
    album: str,
    albumartist: str,
    album_k: str,
) -> list[int]:
    report = _loads(leader.source_report_json, {})
    picker = _cover_picker_meta(report)
    existing = [int(x) for x in (picker.get("media_message_ids") or []) if x]
    if existing:
        return existing
    options = list(picker.get("options") or [])
    if not options:
        return []
    job = _job_from_pending(leader)
    media_ids = await _send_cover_gallery(
        ctx, job, Path(leader.local_path).parent, options, album, albumartist, album_k
    )
    picker["media_message_ids"] = media_ids
    report["cover_picker"] = picker
    ctx.catalog.update_pending_review(leader.id, source_report_json=_dumps(report))
    if media_ids:
        text = format_cover_prompt(album, albumartist, options, leader.file_name)
        status_id = await edit_status(ctx, job, text, cover_keyboard(leader.id, options))
        ctx.catalog.update_pending_review(leader.id, status_message_id=status_id)
    return media_ids


async def _delete_cover_gallery(ctx: Ctx, chat_id: int, message_ids: list) -> None:
    for message_id in message_ids:
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        except Exception:
            log.debug("cover gallery delete failed id=%s", message_id)


def _upsert_cover_pending(
    ctx: Ctx,
    *,
    job: Job,
    dest: Path,
    tags: TagSet,
    identity: Identity,
    report: dict,
    pending_id: int | None,
    replace_id: int | None,
    old_drive_id: str | None,
    track_id: int | None,
) -> int:
    if pending_id is None:
        return ctx.catalog.insert_pending_review(
            phase="cover",
            local_path=str(dest),
            sidecar_path=None,
            relative_path=None,
            kind="library",
            original_json=_dumps(asdict(tags)),
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
            track_id=track_id,
            replace_id=replace_id,
            old_drive_id=old_drive_id,
            expires_at=_expires_at(),
        )
    ctx.catalog.update_pending_review(
        pending_id,
        phase="cover",
        status="waiting",
        local_path=str(dest),
        kind="library",
        working_json=_dumps(asdict(tags)),
        recommended_json=_dumps(asdict(tags)),
        identity_json=_dumps(asdict(identity)),
        source_report_json=_dumps(report),
        replace_id=replace_id,
        old_drive_id=old_drive_id,
        track_id=track_id,
        expires_at=_expires_at(),
    )
    return pending_id


async def maybe_cover_picker(
    ctx: Ctx,
    *,
    job: Job,
    local: Path,
    tags: TagSet,
    identity: Identity,
    report: dict,
    pending_id: int | None = None,
    replace_id: int | None = None,
    old_drive_id: str | None = None,
    track_id: int | None = None,
    replaced: bool = False,
    old_q: tuple[int, int] | None = None,
    new_q: tuple[int, int] | None = None,
    file_cover: tuple[bytes | None, str | None] | None = None,
) -> bool:
    album = tags.album
    albumartist = tags.albumartist or tags.artist
    ident = cover_identity(identity, tags)
    if report.get("manual_cover_final"):
        return False
    if replaced or old_q is not None or new_q is not None:
        report["quality_replace"] = {
            "replaced": replaced,
            "old_q": list(old_q) if old_q else None,
            "new_q": list(new_q) if new_q else None,
        }

    if not is_shareable_album(album):
        hit = await resolve_album_cover(
            ctx, ident, job.topic_name, album=album, albumartist=albumartist
        )
        await _apply_resolved_cover(local, tags, report, hit)
        return False

    existing = await existing_album_cover(ctx, job.topic_name, album, albumartist)
    if existing and existing.data:
        await _apply_resolved_cover(local, tags, report, existing)
        return False

    album_k = cover_album_key(album, albumartist)
    leader = _find_cover_leader(ctx, album_k)
    park_kwargs = dict(
        job=job,
        tags=tags,
        identity=identity,
        pending_id=pending_id,
        replace_id=replace_id,
        old_drive_id=old_drive_id,
        track_id=track_id,
    )
    if leader:
        await _ensure_leader_gallery(ctx, leader, album, albumartist, album_k)
        dest = await _park_pending_flac(ctx, job, local)
        parked = dict(report)
        parked["cover_picker"] = {
            "album_key": album_k,
            "album": album,
            "albumartist": albumartist,
            "role": "follower",
            "leader_id": leader.id,
            "options": [],
            "media_message_ids": [],
        }
        new_id = _upsert_cover_pending(ctx, dest=dest, report=parked, **park_kwargs)
        text = format_cover_prompt(album, albumartist, [], job.file_name, waiting=True)
        status_id = await edit_status(ctx, job, text)
        ctx.catalog.update_pending_review(new_id, status_message_id=status_id)
        log.info("cover follower waiting id=%s leader=%s album=%s", new_id, leader.id, album)
        return True

    cover_tuple: tuple[bytes, str] | None = None
    if file_cover and file_cover[0]:
        cover_tuple = (file_cover[0], file_cover[1] or "image/jpeg")
    else:
        data, mime = await asyncio.to_thread(read_cover, local)
        if data:
            cover_tuple = (data, mime or "image/jpeg")

    await edit_status(ctx, job, "Fetching album cover options…")
    options = await list_cover_candidates(ctx, ident, cover_tuple)
    if len(options) <= 1:
        if options:
            opt = options[0]
            await _embed_cover(local, tags, opt.data, opt.mime)
            cache_album_cover(ctx, album, albumartist, opt.data)
            _set_cover_report(report, opt.source, opt.caa_release)
        return False

    dest = await _park_pending_flac(ctx, job, local)
    option_meta = _write_cover_option_files(options, dest.parent)
    parked = dict(report)
    parked["cover_picker"] = {
        "album_key": album_k,
        "album": album,
        "albumartist": albumartist,
        "role": "leader",
        "options": option_meta,
        "media_message_ids": [],
    }
    new_id = _upsert_cover_pending(ctx, dest=dest, report=parked, **park_kwargs)
    text = format_cover_prompt(album, albumartist, option_meta, job.file_name)
    status_id = await edit_status(ctx, job, text, cover_keyboard(new_id, option_meta))
    ctx.catalog.update_pending_review(new_id, status_message_id=status_id)
    media_ids = await _send_cover_gallery(
        ctx, job, dest.parent, option_meta, album, albumartist, album_k
    )
    parked["cover_picker"]["media_message_ids"] = media_ids
    if not media_ids:
        text = format_cover_prompt(
            album, albumartist, option_meta, job.file_name, rights_warning=True
        )
        status_id = await edit_status(ctx, job, text, cover_keyboard(new_id, option_meta))
        ctx.catalog.update_pending_review(new_id, status_message_id=status_id)
    ctx.catalog.update_pending_review(new_id, source_report_json=_dumps(parked))
    log.info(
        "cover picker waiting id=%s file=%s options=%s photos=%s",
        new_id,
        job.file_name,
        len(options),
        len(media_ids),
    )
    return True


async def _library_commit_with_cover(
    ctx: Ctx,
    *,
    job: Job,
    local: Path,
    tags: TagSet,
    identity: Identity,
    report: dict,
    pending_id: int | None = None,
    replace_id: int | None = None,
    old_drive_id: str | None = None,
    track_id: int | None = None,
    replaced: bool = False,
    old_q: tuple[int, int] | None = None,
    new_q: tuple[int, int] | None = None,
    file_cover: tuple[bytes | None, str | None] | None = None,
) -> None:
    paused = await maybe_cover_picker(
        ctx,
        job=job,
        local=local,
        tags=tags,
        identity=identity,
        report=report,
        pending_id=pending_id,
        replace_id=replace_id,
        old_drive_id=old_drive_id,
        track_id=track_id,
        replaced=replaced,
        old_q=old_q,
        new_q=new_q,
        file_cover=file_cover,
    )
    if paused:
        return
    await _commit_upload(
        ctx,
        job=job,
        local=local,
        tags=tags,
        identity=identity,
        kind="library",
        source_report=report,
        replace_id=replace_id,
        old_drive_id=old_drive_id,
        replaced=replaced,
        old_q=old_q,
        new_q=new_q,
        pending_id=pending_id,
        track_id=track_id,
    )


async def _resume_library_after_cover(
    ctx: Ctx,
    row: PendingReview,
    tags: TagSet,
    identity: Identity,
    report: dict,
) -> None:
    job = _job_from_pending(row)
    qr = report.get("quality_replace") or {}
    old_q = tuple(qr["old_q"]) if qr.get("old_q") else None
    new_q = tuple(qr["new_q"]) if qr.get("new_q") else None
    await _commit_upload(
        ctx,
        job=job,
        local=Path(row.local_path),
        tags=tags,
        identity=identity,
        kind="library",
        source_report=apply_chosen(report, tags, identity),
        replace_id=row.replace_id,
        old_drive_id=row.old_drive_id,
        replaced=bool(qr.get("replaced")),
        old_q=old_q,
        new_q=new_q,
        pending_id=row.id,
        track_id=row.track_id,
    )


async def _apply_cover_to_waiter(
    ctx: Ctx,
    row: PendingReview,
    data: bytes,
    mime: str,
    source: str,
    caa_release: str | None,
) -> None:
    working = tagset_from_dict(_loads(row.working_json, {}))
    identity = identity_from_dict(_loads(row.identity_json, {}))
    report = _loads(row.source_report_json, {})
    local = Path(row.local_path)
    if not local.exists():
        ctx.catalog.update_pending_review(row.id, status="failed")
        return
    await _embed_cover(local, working, data, mime)
    _set_cover_report(report, source, caa_release)
    report.pop("cover_picker", None)
    await _resume_library_after_cover(ctx, row, working, identity, report)


async def _apply_cover_choice(ctx: Ctx, row: PendingReview, index: int) -> None:
    _original, _recommended, working, identity, report, _cands = _load_pending_state(row)
    picker = _cover_picker_meta(report)
    if picker.get("role") != "leader":
        return
    options = picker.get("options") or []
    if index < 0 or index >= len(options):
        return
    local = Path(row.local_path)
    job = _job_from_pending(row)
    if not local.exists():
        await edit_status(ctx, job, "Local file missing. Cannot continue.")
        ctx.catalog.update_pending_review(row.id, status="failed")
        return
    chosen = _read_cover_option(local, options[index])
    if not chosen:
        await edit_status(ctx, job, "Cover file missing. Cannot continue.")
        ctx.catalog.update_pending_review(row.id, status="failed")
        return
    data, mime, source, caa_release = chosen
    tags = tagset_from_dict(working)
    album = str(picker.get("album") or tags.album)
    albumartist = str(picker.get("albumartist") or tags.albumartist or tags.artist)
    await _embed_cover(local, tags, data, mime)
    cache_album_cover(ctx, album, albumartist, data)
    _set_cover_report(report, source, caa_release)
    await _delete_cover_gallery(ctx, row.chat_id, list(picker.get("media_message_ids") or []))
    _cleanup_cover_option_files(local, options, str(picker.get("album_key") or ""))
    album_k = picker.get("album_key")
    report.pop("cover_picker", None)
    ctx.catalog.update_pending_review(row.id, source_report_json=_dumps(report))
    await _resume_library_after_cover(ctx, row, tags, identity, report)
    for other in ctx.catalog.list_waiting_by_phase("cover"):
        if other.id == row.id:
            continue
        other_picker = _cover_picker_meta(_loads(other.source_report_json, {}))
        if other_picker.get("album_key") != album_k:
            continue
        await _apply_cover_to_waiter(ctx, other, data, mime, source, caa_release)


async def _cover_choice_from_pending(
    ctx: Ctx, row: PendingReview
) -> tuple[bytes, str, str, str | None] | None:
    report = _loads(row.source_report_json, {})
    picker = _cover_picker_meta(report)
    tags = tagset_from_dict(_loads(row.working_json, {}))
    album = str(picker.get("album") or tags.album)
    albumartist = str(picker.get("albumartist") or tags.albumartist or tags.artist)
    existing = await existing_album_cover(ctx, row.topic_name, album, albumartist)
    if existing and existing.data:
        return existing.data, existing.mime or "image/jpeg", existing.source, existing.caa_release
    if picker.get("role") == "leader":
        options = picker.get("options") or []
        if options:
            return _read_cover_option(Path(row.local_path), options[0])
        return None
    leader_id = picker.get("leader_id")
    if not leader_id:
        return None
    leader = ctx.catalog.get_pending_review(int(leader_id))
    if leader is None:
        return None
    leader_picker = _cover_picker_meta(_loads(leader.source_report_json, {}))
    options = leader_picker.get("options") or []
    if not options:
        return None
    return _read_cover_option(Path(leader.local_path), options[0])


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
            kind=kind,
        )
        ctx.catalog.mark_uploaded(track_id, file_id, url)
        if old_drive_id and old_drive_id != file_id and conflict_action != "keep_both":
            await asyncio.to_thread(ctx.drive.delete_file, old_drive_id)
        if pending_id is not None:
            completed = ctx.catalog.get_pending_review(pending_id)
            if completed:
                cleaned = await _delete_promoted_review_source(ctx, completed)
                ctx.catalog.update_pending_review(
                    pending_id, status="done" if cleaned else "cleanup_pending"
                )
            else:
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


async def _delete_promoted_review_source(ctx: Ctx, row: PendingReview) -> bool:
    if not row.source_drive_file_id:
        return True
    try:
        await asyncio.to_thread(ctx.drive.delete_file, row.source_drive_file_id)
        if row.source_drive_sidecar_id:
            await asyncio.to_thread(ctx.drive.delete_file, row.source_drive_sidecar_id)
        return True
    except Exception:
        log.warning("promoted review source cleanup failed", exc_info=True)
        return False


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
    kind: str,
) -> None:
    if kind == "library":
        cover, mime = await asyncio.to_thread(read_cover, dest)
        if cover:
            try:
                await asyncio.to_thread(upload_album_cover_if_missing, ctx, parent_id, cover, mime)
            except Exception:
                log.warning("album cover upload failed", exc_info=True)
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
    original, recommended, working, identity, _report, _candidates = _load_pending_state(row)
    job = _job_from_pending(row)
    text = format_summary(original, recommended, working, reason=identity.confidence_reason)
    status_id = await edit_status(ctx, job, text, review_keyboard(row.id, original, recommended, working))
    if status_id != row.status_message_id:
        ctx.catalog.update_pending_review(row.id, status_message_id=status_id)


async def _run_claimed_action(ctx: Ctx, row: PendingReview, action) -> None:
    try:
        await action
    except Exception:
        log.exception("pending action failed id=%s phase=%s", row.id, row.phase)
        ctx.catalog.transition_pending(row.id, "processing", "waiting")
        refreshed = ctx.catalog.get_pending_review(row.id)
        if refreshed and refreshed.phase == "tags":
            await _refresh_tag_ui(ctx, refreshed)
        elif refreshed and refreshed.phase == "cover":
            report = _loads(refreshed.source_report_json, {})
            picker = _cover_picker_meta(report)
            tags = tagset_from_dict(_loads(refreshed.working_json, {}))
            await edit_status(
                ctx,
                _job_from_pending(refreshed),
                format_cover_prompt(
                    str(picker.get("album") or tags.album),
                    str(picker.get("albumartist") or tags.albumartist or tags.artist),
                    picker.get("options") or [],
                    refreshed.file_name,
                    waiting=picker.get("role") == "follower",
                ),
                cover_keyboard(refreshed.id, picker.get("options") or []),
            )
        elif refreshed and refreshed.phase == "drive":
            await edit_status(
                ctx,
                _job_from_pending(refreshed),
                "Action failed. Nothing was discarded; retry or cancel.",
                conflict_keyboard(refreshed.id),
            )


async def handle_pending_callback(callback: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
    action = parse_callback(callback.data)
    if action is None:
        await callback.answer()
        return
    row = ctx.catalog.get_pending_review(action.pending_id)
    if row is None or row.status not in {"waiting", "expiring"}:
        await callback.answer("Already handled.")
        return
    if callback.message and callback.message.chat.id != row.chat_id:
        await callback.answer()
        return
    if row.chat_id > 0 and callback.from_user:
        try:
            member = await ctx.bot.get_chat_member(
                ctx.settings.allowed_chat_id, callback.from_user.id
            )
            raw_status = member.status
            status = str(getattr(raw_status, "value", raw_status))
            allowed = status in {"creator", "administrator", "member"} or (
                status == "restricted" and bool(getattr(member, "is_member", False))
            )
        except Exception:
            allowed = False
        if not allowed:
            await callback.answer("Access denied.", show_alert=True)
            return
    if row.status != "waiting":
        await callback.answer("Expired.")
        return
    if not ctx.catalog.claim_pending(row.id, "processing"):
        await callback.answer("Already handled.")
        return
    await callback.answer()
    log.debug("fsm callback id=%s op=%s field=%s", row.id, action.op, action.field)

    if action.op == "cancel":
        await state.clear()
        await cancel_pending(ctx, row)
        return
    if action.op == "ok":
        await state.clear()
        await _run_claimed_action(
            ctx, row, _apply_confirm(ctx, row, kind="library")
        )
        return
    if action.op == "rev":
        await state.clear()
        await _run_claimed_action(
            ctx, row, _apply_confirm(ctx, row, kind="review")
        )
        return
    if action.op == "cover" and action.index is not None:
        if row.phase != "cover":
            ctx.catalog.update_pending_review(row.id, status="waiting")
            return
        await state.clear()
        await _run_claimed_action(
            ctx, row, _apply_cover_choice(ctx, row, action.index)
        )
        return
    if action.op == "cand" and action.index is not None:
        await state.clear()
        await _run_claimed_action(
            ctx, row, _apply_candidate(ctx, row, action.index)
        )
        return
    if action.op == "toggle" and action.field:
        original, recommended, working, _identity, _report, _cands = _load_pending_state(row)
        working = toggle_working_field(original, recommended, working, action.field)
        ctx.catalog.update_pending_review(
            row.id, working_json=_dumps(working), status="waiting"
        )
        row = ctx.catalog.get_pending_review(row.id)
        if row:
            await _refresh_tag_ui(ctx, row)
        return
    if action.op in {"use_file", "use_rec"}:
        await state.clear()
        original, recommended, _working, _identity, _report, _cands = _load_pending_state(row)
        working = bulk_choice(original, recommended, use_file=action.op == "use_file")
        ctx.catalog.update_pending_review(
            row.id, working_json=_dumps(working), status="waiting"
        )
        row = ctx.catalog.get_pending_review(row.id)
        if row:
            await _refresh_tag_ui(ctx, row)
        return
    if action.op in {"drive_replace", "drive_keep", "drive_skip"}:
        mapping = {"drive_replace": "replace", "drive_keep": "keep_both", "drive_skip": "skip"}
        await _run_claimed_action(
            ctx, row, _apply_drive_choice(ctx, row, mapping[action.op])
        )
        return
    ctx.catalog.update_pending_review(row.id, status="waiting")


async def cancel_pending(ctx: Ctx, row: PendingReview) -> None:
    report = _loads(row.source_report_json, {})
    picker = _cover_picker_meta(report)
    if picker:
        await _delete_cover_gallery(
            ctx, row.chat_id, list(picker.get("media_message_ids") or [])
        )
        _cleanup_cover_option_files(
            Path(row.local_path),
            picker.get("options") or [],
            str(picker.get("album_key") or ""),
        )
    local = Path(row.local_path) if row.local_path else None
    sidecar = Path(row.sidecar_path) if row.sidecar_path else None
    unlink_quiet(sidecar)
    if local and row.local_path:
        unlink_quiet(local.parent / "manual-cover.jpg")
    if local and local.is_file():
        unlink_quiet(local)
    if local and row.local_path:
        try:
            local.parent.rmdir()
        except OSError:
            pass
    ctx.catalog.update_pending_review(row.id, status="cancelled")
    await edit_status(ctx, _job_from_pending(row), "Cancelled.")


async def _apply_confirm(ctx: Ctx, row: PendingReview, *, kind: str) -> None:
    _original, _recommended, working, identity, report, _cands = _load_pending_state(row)
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
            await _library_commit_with_cover(
                ctx,
                job=job,
                local=local,
                tags=tags,
                identity=identity,
                report=report,
                replace_id=existing.id,
                old_drive_id=existing.drive_file_id,
                replaced=existing.status == "uploaded" and new_q > old_q,
                old_q=old_q,
                new_q=new_q,
                pending_id=row.id,
                file_cover=(cover, mime),
            )
            return

    if kind != "library":
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
        return

    await _library_commit_with_cover(
        ctx,
        job=job,
        local=local,
        tags=tags,
        identity=identity,
        report=report,
        replace_id=row.replace_id,
        old_drive_id=row.old_drive_id,
        pending_id=row.id,
        track_id=row.track_id,
        file_cover=(cover, mime),
    )


async def _apply_candidate(ctx: Ctx, row: PendingReview, index: int) -> None:
    _original, _recommended, _working, identity, report, candidates = _load_pending_state(row)
    if index < 0 or index >= len(candidates):
        ctx.catalog.update_pending_review(row.id, status="waiting")
        return
    cand = candidates[index]
    mbid = cand.get("mb_recording_id") if isinstance(cand, dict) else cand.mb_recording_id
    if not mbid:
        ctx.catalog.update_pending_review(row.id, status="waiting")
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
        ctx.catalog.update_pending_review(row.id, status="waiting")
        row = ctx.catalog.get_pending_review(row.id)
        if row:
            await _refresh_tag_ui(ctx, row)
        return
    new_identity.candidates = identity.candidates
    new_identity.confidence = "low"
    new_identity.confidence_reason = identity.confidence_reason
    new_identity.source_report = {**identity.source_report, **new_identity.source_report}
    local = Path(row.local_path)
    cover, mime = await asyncio.to_thread(read_cover, local)
    enrichment = await enrich(
        ctx.http,
        new_identity,
        ctx.genre,
        ctx.settings.lastfm_api_key,
        row.topic_name,
        cover=cover,
        cover_mime=mime,
        cover_source="file" if cover else "none",
    )
    tags = identity_to_tags(new_identity, enrichment)
    await asyncio.to_thread(write_tags, local, tags, cover, mime)
    report = merge_enrichment(report, enrichment)
    report = apply_chosen(report, tags, new_identity)
    ctx.catalog.update_pending_review(
        row.id,
        recommended_json=_dumps(asdict(tags)),
        working_json=_dumps(asdict(tags)),
        identity_json=_dumps(asdict(new_identity)),
        source_report_json=_dumps(report),
        status="waiting",
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
    for cleanup in ctx.catalog.list_pending_by_status("cleanup_pending"):
        if await _delete_promoted_review_source(ctx, cleanup):
            ctx.catalog.update_pending_review(cleanup.id, status="done")
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
            elif row.phase == "cover":
                working = tagset_from_dict(_loads(row.working_json, {}))
                identity = identity_from_dict(_loads(row.identity_json, {}))
                report = _loads(row.source_report_json, {})
                picker = _cover_picker_meta(report)
                local = Path(row.local_path)
                if not local.exists():
                    ctx.catalog.update_pending_review(row.id, status="failed")
                    continue
                chosen = await _cover_choice_from_pending(ctx, row)
                if chosen:
                    data, mime, source, caa_release = chosen
                    await _embed_cover(local, working, data, mime)
                    cache_album_cover(
                        ctx,
                        str(picker.get("album") or working.album),
                        str(picker.get("albumartist") or working.albumartist or working.artist),
                        data,
                    )
                    _set_cover_report(report, source, caa_release)
                if picker.get("role") == "leader":
                    await _delete_cover_gallery(
                        ctx, row.chat_id, list(picker.get("media_message_ids") or [])
                    )
                    _cleanup_cover_option_files(
                        local,
                        picker.get("options") or [],
                        str(picker.get("album_key") or ""),
                    )
                await edit_status(ctx, job, "Timed out (24h). Using first cover option…")
                report.pop("cover_picker", None)
                await _resume_library_after_cover(ctx, row, working, identity, report)
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
                local = Path(row.local_path) if row.local_path else None
                if local:
                    unlink_quiet(local)
                    unlink_quiet(local.parent / "manual-cover.jpg")
                if row.sidecar_path:
                    unlink_quiet(Path(row.sidecar_path))
                ctx.catalog.update_pending_review(row.id, status="expired")
                await edit_status(ctx, job, "Expired after 24 hours. Start again.")
        except Exception:
            log.exception("expire pending id=%s failed", row.id)
            ctx.catalog.update_pending_review(row.id, status="failed")
