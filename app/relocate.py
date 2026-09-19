from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from app.drive import require_drive, resolve_drive, user_drive_root
from app.formats import detect_format, extension_for, extension_from_path, mime_for_path
from app.genre import genre_tokens
from app.library import library_relative, place_file, review_relative, rmdir_empty, unlink_quiet, write_sidecar
from app.models import Ctx, Identity, TagSet, TrackRecord, identity_from_dict, tagset_from_dict
from app.paths import user_kind_root
from app.tags import AudioMetrics, normalize_tagset, overlay_tagset, read_audio_metrics, read_cover, read_tagset, write_tags
from app.util import sanitize_filename

log = logging.getLogger(__name__)
_FLAC_LOCKS: dict[int, asyncio.Lock] = {}


def _flac_lock(track_id: int) -> asyncio.Lock:
    lock = _FLAC_LOCKS.get(track_id)
    if lock is None:
        lock = asyncio.Lock()
        _FLAC_LOCKS[track_id] = lock
    return lock


def _loads(text: str | None, default: object):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def tags_from_track(track: TrackRecord) -> TagSet:
    if track.tags_json:
        return tagset_from_dict(_loads(track.tags_json, {}))
    report = _loads(track.source_report_json, {})
    chosen = report.get("chosen") or {}
    return TagSet(
        title=str(chosen.get("title") or track.title or ""),
        album=str(chosen.get("album") or track.album or ""),
        artist=str(chosen.get("artist") or track.artist or ""),
        albumartist=str(chosen.get("albumartist") or ""),
        composer=str(chosen.get("composer") or ""),
        genre=str(chosen.get("genre") or ""),
        date=str(chosen.get("year") or chosen.get("date") or ""),
        tracknumber=str(chosen.get("track") or ""),
        discnumber=str(chosen.get("disc") or ""),
        lyrics="",
    )


def identity_from_track(track: TrackRecord) -> Identity:
    if getattr(track, "identity_json", None):
        raw = _loads(track.identity_json, {})
        if isinstance(raw, dict) and raw.get("confidence"):
            return identity_from_dict(raw)
    report = _loads(getattr(track, "source_report_json", None), {})
    acoustid = report.get("acoustid") or {}
    return Identity(
        confidence=report.get("confidence") or "low",
        confidence_reason=str(report.get("confidence_reason") or ""),
        source=str(report.get("chosen_source") or "catalog"),
        mb_recording_id=str(report.get("chosen_mbid") or "") or None,
        acoustid=acoustid.get("id"),
        acoustid_score=acoustid.get("score"),
        title=track.title or "",
        album=track.album or "",
    )


def sidecar_payload(topic: str, file_name: str, tags: TagSet, identity: Identity) -> dict:
    return {
        "confidence": identity.confidence,
        "confidence_reason": identity.confidence_reason,
        "source": identity.source,
        "acoustid": identity.acoustid,
        "acoustid_score": identity.acoustid_score,
        "mb_recording_id": identity.mb_recording_id,
        "topic": topic,
        "proposed": asdict(tags),
        "candidates": [asdict(c) for c in identity.candidates],
        "original_filename": file_name,
    }


def drive_root_id(ctx: Ctx, kind: str, user_id: int = 0) -> str:
    return user_drive_root(ctx, kind, user_id)


def ctx_for_user(ctx: Ctx, user_id: int) -> Ctx:
    client = resolve_drive(ctx, user_id)
    kwargs: dict = {"index_user_id": user_id}
    if client is not None and client is not ctx.drive:
        kwargs["drive"] = client
    return replace(ctx, **kwargs)


def local_kind_root(ctx: Ctx, kind: str, user_id: int = 0) -> Path:
    if user_id:
        return user_kind_root(ctx, user_id, kind)
    return ctx.settings.library_root if kind == "library" else ctx.settings.review_root


def _stage_dest(ctx: Ctx, track: TrackRecord) -> Path:
    raw = Path(track.file_name or track.relative_path or "track.flac").name
    name = sanitize_filename(raw)
    fmt = detect_format(name, "")
    if fmt:
        name = f"{Path(name).stem}{extension_for(fmt)}"
    else:
        name = f"{Path(name).stem}.flac"
    dest = ctx.settings.pending_root / str(uuid.uuid4()) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


async def stage_track_flac(ctx: Ctx, track: TrackRecord) -> Path:
    """Copy the current Telegram audio when possible; else local/Drive. Always a new pending file."""
    from app.botapi import discard_download

    uid = getattr(track, "user_id", 0) or getattr(ctx, "index_user_id", 0) or 0
    if uid:
        ctx = ctx_for_user(ctx, uid)
    dest = _stage_dest(ctx, track)
    if track.telegram_file_id:
        try:
            file = await ctx.bot.get_file(track.telegram_file_id)
            await ctx.bot.download(file, destination=dest)
            await asyncio.to_thread(discard_download, file.file_path)
            if dest.is_file() and dest.stat().st_size > 0:
                log.info("stage source=telegram track=%s", track.id)
                return dest
        except Exception:
            log.warning("telegram original unavailable track=%s", track.id, exc_info=True)
            shutil.rmtree(dest.parent, ignore_errors=True)
            dest = _stage_dest(ctx, track)
    local = await ensure_local_flac(ctx, track)
    await asyncio.to_thread(shutil.copy2, local, dest)
    log.info("stage source=local/drive track=%s path=%s", track.id, dest)
    return dest


async def _resolve_drive_file_id(ctx: Ctx, track: TrackRecord) -> str | None:
    if track.drive_file_id:
        return track.drive_file_id
    if not track.relative_path:
        return None
    relative = Path(track.relative_path)
    uid = getattr(track, "user_id", 0) or getattr(ctx, "index_user_id", 0) or 0
    root = drive_root_id(ctx, track.kind, uid)
    drive = require_drive(ctx, uid, method="find_path")
    if drive is None:
        return None
    parent = await asyncio.to_thread(drive.find_path, root, list(relative.parts[:-1]))
    if not parent:
        return None
    hits = await asyncio.to_thread(drive.find_name_conflicts, parent, relative.name)
    return hits[0].id if hits else None


async def ensure_local_flac(ctx: Ctx, track: TrackRecord) -> Path:
    async with _flac_lock(track.id):
        return await _ensure_local_flac_locked(ctx, ctx.catalog.get_track(track.id) or track)


async def _ensure_local_flac_locked(ctx: Ctx, track: TrackRecord) -> Path:
    candidates: list[Path] = []
    if track.local_path:
        candidates.append(Path(track.local_path))
    if track.relative_path:
        root = local_kind_root(ctx, track.kind, getattr(track, "user_id", 0) or 0)
        candidates.append(root / track.relative_path)
    for path in candidates:
        if path.is_file():
            if str(path) != (track.local_path or ""):
                ctx.catalog.update_track(track.id, local_path=str(path))
            return path

    file_id = await _resolve_drive_file_id(ctx, track)
    if not file_id:
        raise FileNotFoundError("Track has no local file and no Drive copy.")
    uid = getattr(track, "user_id", 0) or getattr(ctx, "index_user_id", 0) or 0
    drive = require_drive(ctx, uid, method="download_to")
    if drive is None:
        raise FileNotFoundError("Track has a Drive copy but no Drive client for this user.")
    name = Path(track.relative_path or track.file_name or "track.flac").name
    dest = ctx.settings.pending_root / str(uuid.uuid4()) / sanitize_filename(name)
    log.info("downloading track=%s from Drive id=%s to %s", track.id, file_id, dest)
    await asyncio.to_thread(drive.download_to, file_id, dest)
    ctx.catalog.update_track(track.id, local_path=str(dest), drive_file_id=file_id)
    return dest


async def _delete_drive_named(
    ctx: Ctx, kind: str, relative: Path | None, extra_ids: list[str | None], user_id: int = 0
) -> None:
    seen: set[str] = set()
    drive = require_drive(ctx, user_id, method="delete_file")
    if drive is None:
        return
    for file_id in extra_ids:
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)
        try:
            await asyncio.to_thread(drive.delete_file, file_id)
        except Exception:
            log.warning("Drive delete failed id=%s", file_id, exc_info=True)
    if relative is None:
        return
    root = drive_root_id(ctx, kind, user_id)
    parent = await asyncio.to_thread(drive.find_path, root, list(relative.parts[:-1]))
    if not parent:
        return
    for name in (relative.name, relative.with_suffix(".json").name, relative.with_suffix(".log").name):
        try:
            hits = await asyncio.to_thread(drive.find_name_conflicts, parent, name)
        except Exception:
            log.debug("Drive sibling lookup failed name=%s", name, exc_info=True)
            continue
        for hit in hits:
            if hit.id in seen:
                continue
            seen.add(hit.id)
            try:
                await asyncio.to_thread(drive.delete_file, hit.id)
            except Exception:
                log.warning("Drive sibling delete failed id=%s", hit.id, exc_info=True)


async def _upload_review_sidecar(
    ctx: Ctx, dest: Path, parent_id: str, topic: str, file_name: str, tags: TagSet, identity: Identity
) -> tuple[Path, str | None]:
    sidecar = dest.with_suffix(".json")
    await asyncio.to_thread(write_sidecar, sidecar, sidecar_payload(topic, file_name, tags, identity))
    hits = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent_id, sidecar.name)
    try:
        if hits:
            sidecar_id, _url = await asyncio.to_thread(
                ctx.drive.replace_file, hits[0].id, sidecar, "application/json"
            )
        else:
            sidecar_id, _url = await asyncio.to_thread(
                ctx.drive.create_file, sidecar, parent_id, sidecar.name, "application/json"
            )
    except Exception:
        log.warning("review sidecar upload failed", exc_info=True)
        return sidecar, None
    return sidecar, sidecar_id


async def _upload_songlog(
    ctx: Ctx, dest: Path, parent_id: str, source_report: dict, tags: TagSet, identity: Identity
) -> str | None:
    if not ctx.settings.enable_log_per_music_file:
        return None
    from app.songlog import apply_chosen, render_songlog

    payload = render_songlog(apply_chosen(source_report, tags, identity)).encode("utf-8")
    log_name = dest.with_suffix(".log").name
    hits = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent_id, log_name)
    replace_id = hits[0].id if hits else None
    try:
        file_id, _url = await asyncio.to_thread(
            ctx.drive.upload_bytes, payload, parent_id, log_name, "text/plain", replace_id=replace_id
        )
    except Exception:
        log.warning("song log upload failed", exc_info=True)
        return None
    return file_id


async def relocate_track(
    ctx: Ctx,
    track: TrackRecord,
    *,
    kind: str,
    tags: TagSet,
    identity: Identity,
    source_report: dict,
    topic_name: str,
    file_name: str,
    staged: Path | None = None,
    correct_telegram: bool = False,
    editor_user_id: int | None = None,
) -> TrackRecord:
    uid = getattr(track, "user_id", 0) or 0
    ctx = ctx_for_user(ctx, uid)
    local = staged if staged and staged.is_file() else await ensure_local_flac(ctx, track)
    tags = normalize_tagset(tags, ctx.genre)
    if tags.genre:
        tags = replace(tags, genre=ctx.genre.classify(genre_tokens(tags.genre)))
    cover, mime = await asyncio.to_thread(read_cover, local)
    await asyncio.to_thread(write_tags, local, tags, cover, mime)

    old_kind = track.kind
    old_relative = Path(track.relative_path) if track.relative_path else None
    old_local = Path(track.local_path) if track.local_path else None
    from app.library_index import remember_library_tags, remove_library_index

    if kind == "library":
        relative = library_relative(topic_name, tags, ext=extension_from_path(local))
        dest = local_kind_root(ctx, "library", uid) / relative
        root_id = drive_root_id(ctx, "library", uid)
        sidecar_path: Path | None = None
    else:
        relative = review_relative(file_name)
        dest = local_kind_root(ctx, "review", uid) / relative
        root_id = drive_root_id(ctx, "review", uid)
        sidecar_path = dest.with_suffix(".json")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if local.resolve() != dest.resolve():
        dest = await asyncio.to_thread(place_file, local, dest)

    drive = ctx.drive
    can_drive = bool(root_id) and callable(getattr(drive, "ensure_parent", None))
    file_id, url = "", None
    sidecar_id = None
    log_id = None
    filename = relative.name
    if can_drive:
        parent_id = await asyncio.to_thread(drive.ensure_parent, root_id, relative)
        conflicts = await asyncio.to_thread(drive.find_name_conflicts, parent_id, filename)
        try:
            if conflicts:
                file_id, url = await asyncio.to_thread(
                    drive.replace_file, conflicts[0].id, dest, mime_for_path(dest)
                )
            else:
                file_id, url = await asyncio.to_thread(
                    drive.create_file, dest, parent_id, filename, mime_for_path(dest)
                )
        except Exception as exc:
            log.exception("Drive relocate upload failed track=%s kind=%s", track.id, kind)
            from app.errors import to_app_error

            raise to_app_error(exc) from exc
        if kind == "review":
            sidecar_path, sidecar_id = await _upload_review_sidecar(
                ctx, dest, parent_id, topic_name, file_name, tags, identity
            )
        log_id = await _upload_songlog(ctx, dest, parent_id, source_report, tags, identity)
    elif kind == "review" and sidecar_path:
        from app.library import write_sidecar

        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            write_sidecar,
            sidecar_path,
            {"topic": topic_name, "file_name": file_name, "proposed": asdict(tags)},
        )

    same_drive = bool(track.drive_file_id and track.drive_file_id == file_id)
    if not same_drive:
        await _delete_drive_named(
            ctx,
            old_kind,
            old_relative,
            [track.drive_file_id, track.drive_sidecar_id, track.drive_log_id],
            user_id=uid,
        )
    elif kind != "review" and track.drive_sidecar_id:
        try:
            await asyncio.to_thread(ctx.drive.delete_file, track.drive_sidecar_id)
        except Exception:
            log.warning("old review sidecar delete failed", exc_info=True)

    if old_local and old_local.exists() and old_local.resolve() != dest.resolve():
        unlink_quiet(old_local)
        if old_kind == "review":
            unlink_quiet(old_local.with_suffix(".json"))
        root = ctx.settings.library_root if old_kind == "library" else ctx.settings.review_root
        rmdir_empty(root)

    ctx.catalog.update_track(
        track.id,
        kind=kind,
        local_path=str(dest),
        sidecar_path=str(sidecar_path) if sidecar_path else None,
        relative_path=relative.as_posix(),
        status="uploaded",
        title=tags.title,
        artist=tags.artist,
        album=tags.album,
        drive_file_id=file_id,
        drive_url=url,
        drive_sidecar_id=sidecar_id,
        drive_log_id=log_id,
        source_report_json=json.dumps(source_report, ensure_ascii=False),
        tags_json=json.dumps(asdict(tags), ensure_ascii=False),
        identity_json=json.dumps(asdict(identity), ensure_ascii=False),
        topic_name=topic_name,
        file_name=file_name,
        error=None,
        last_editor_user_id=editor_user_id if editor_user_id else getattr(track, "last_editor_user_id", None),
    )
    if old_kind == "library" and old_relative and (
        kind != "library" or old_relative.as_posix() != relative.as_posix()
    ):
        await asyncio.to_thread(remove_library_index, ctx, old_relative.as_posix())
    await asyncio.to_thread(
        remember_library_tags,
        ctx,
        kind=kind,
        relative_path=relative.as_posix(),
        drive_file_id=file_id,
        topic_name=topic_name,
        tags=tags,
        telegram_file_id=track.telegram_file_id,
        chat_id=track.source_chat_id,
        message_id=track.source_message_id,
        thread_id=track.thread_id,
    )
    refreshed = ctx.catalog.get_track(track.id)
    assert refreshed is not None
    if correct_telegram and refreshed.source_chat_id and (refreshed.source_message_id or refreshed.telegram_file_id):
        from app.captions import music_caption
        from app.telegram_file import update_public_audio

        caption = music_caption(
            tags=tags,
            track=refreshed,
            relative_path=relative.as_posix(),
            last_editor_user_id=editor_user_id,
        )
        msg_id = refreshed.source_message_id
        if msg_id:
            new_id = await update_public_audio(
                ctx,
                chat_id=refreshed.source_chat_id,
                message_id=msg_id,
                path=dest,
                caption=caption,
                correct_media=True,
            )
            if new_id:
                ctx.catalog.update_track(refreshed.id, telegram_file_id=new_id)
                refreshed = ctx.catalog.get_track(refreshed.id) or refreshed
        if editor_user_id:
            ctx.catalog.increment_songs_edited(editor_user_id)
    return refreshed


async def delete_track(ctx: Ctx, track: TrackRecord) -> None:
    from app.library_index import remove_library_index

    relative = Path(track.relative_path) if track.relative_path else None
    if track.kind == "library" and track.relative_path:
        await asyncio.to_thread(remove_library_index, ctx, track.relative_path)
    await _delete_drive_named(
        ctx,
        track.kind,
        relative,
        [track.drive_file_id, track.drive_sidecar_id, track.drive_log_id],
        user_id=getattr(track, "user_id", 0) or 0,
    )
    local = Path(track.local_path) if track.local_path else None
    if local:
        unlink_quiet(local)
        unlink_quiet(local.with_suffix(".json"))
        unlink_quiet(local.with_suffix(".log"))
        root = ctx.settings.library_root if track.kind == "library" else ctx.settings.review_root
        rmdir_empty(root)
    ctx.catalog.update_track(
        track.id,
        status="deleted",
        local_path=None,
        sidecar_path=None,
        drive_file_id=None,
        drive_url=None,
        drive_sidecar_id=None,
        drive_log_id=None,
        error=None,
    )


def read_tags_for_card(track: TrackRecord) -> TagSet:
    tags = tags_from_track(track)
    if track.local_path:
        path = Path(track.local_path)
        if path.is_file():
            try:
                return overlay_tagset(tags, read_tagset(path))
            except Exception:
                log.debug("read_tagset failed path=%s", path, exc_info=True)
    return tags


def _bitrate_from_report(report: object) -> int | None:
    if not isinstance(report, dict):
        return None
    raw = report.get("bitrate")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value or None


def metrics_from_track(track: TrackRecord) -> AudioMetrics:
    if track.local_path:
        path = Path(track.local_path)
        if path.is_file():
            try:
                return read_audio_metrics(path)
            except Exception:
                log.debug("read_audio_metrics failed path=%s", path, exc_info=True)
    ident = identity_from_track(track)
    report = ident.source_report or _loads(getattr(track, "source_report_json", None), {})
    duration = ident.duration or 0.0
    if isinstance(report, dict) and not duration:
        duration = float(report.get("duration") or 0)
    return AudioMetrics(
        duration=duration or 0.0,
        bit_depth=getattr(track, "bit_depth", None) or ident.bit_depth,
        sample_rate=getattr(track, "sample_rate", None) or ident.sample_rate,
        bitrate_kbps=_bitrate_from_report(report),
    )


async def hydrate_track_tags(ctx: Ctx, track: TrackRecord) -> TrackRecord:
    local = await ensure_local_flac(ctx, track)
    tags = await asyncio.to_thread(read_tagset, local)
    fields: dict[str, object] = {"local_path": str(local)}
    if any(asdict(tags).values()):
        fields["title"] = tags.title or track.title
        fields["artist"] = tags.artist or track.artist
        fields["album"] = tags.album or track.album
        fields["tags_json"] = json.dumps(asdict(tags), ensure_ascii=False)
    try:
        metrics = await asyncio.to_thread(read_audio_metrics, local)
        fields["bit_depth"] = metrics.bit_depth
        fields["sample_rate"] = metrics.sample_rate
        report = _loads(track.source_report_json, {})
        if not isinstance(report, dict):
            report = {}
        report["duration"] = metrics.duration
        report["bit_depth"] = metrics.bit_depth
        report["sample_rate"] = metrics.sample_rate
        if metrics.bitrate_kbps:
            report["bitrate"] = metrics.bitrate_kbps
        fields["source_report_json"] = json.dumps(report, ensure_ascii=False)
    except Exception:
        log.debug("hydrate audio metrics failed path=%s", local, exc_info=True)
    ctx.catalog.update_track(track.id, **fields)
    return ctx.catalog.get_track(track.id) or track


async def copy_track_for_user(
    ctx: Ctx,
    track: TrackRecord,
    user_id: int,
    *,
    kind: str,
) -> TrackRecord:
    tags = tags_from_track(track)
    identity = identity_from_track(track)
    report = _loads(track.source_report_json, {})
    if not isinstance(report, dict):
        report = {}
    staged = await stage_track_flac(ctx, track)
    if getattr(track, "user_id", 0) == user_id:
        return await relocate_track(
            ctx,
            track,
            kind=kind,
            tags=tags,
            identity=identity,
            source_report=report,
            topic_name=track.topic_name or "Unknown",
            file_name=track.file_name or "track.flac",
            staged=staged,
        )
    new_id = ctx.catalog.insert_pending(
        kind=kind,
        mb_recording_id=track.mb_recording_id,
        acoustid=track.acoustid,
        local_path=str(staged),
        sidecar_path=None,
        relative_path=track.relative_path or staged.name,
        bit_depth=track.bit_depth,
        sample_rate=track.sample_rate,
        title=track.title,
        artist=track.artist,
        album=track.album,
        user_id=user_id,
        audio_sha256=getattr(track, "audio_sha256", None),
        telegram_file_id=track.telegram_file_id,
        tags_json=track.tags_json,
        identity_json=track.identity_json,
        source_report_json=track.source_report_json,
        topic_name=track.topic_name,
        file_name=track.file_name,
    )
    dest = ctx.catalog.get_track(new_id)
    assert dest is not None
    return await relocate_track(
        ctx,
        dest,
        kind=kind,
        tags=tags,
        identity=identity,
        source_report=report,
        topic_name=track.topic_name or "Unknown",
        file_name=track.file_name or "track.flac",
        staged=staged,
    )


def user_copy_of(ctx: Ctx, track: TrackRecord, user_id: int) -> TrackRecord | None:
    sha = getattr(track, "audio_sha256", None)
    if sha:
        found = ctx.catalog.find_user_track_by_sha(user_id, sha)
        if found:
            return found
    if track.mb_recording_id:
        found = ctx.catalog.find_user_track_by_mbid(user_id, track.mb_recording_id)
        if found:
            return found
    if getattr(track, "user_id", 0) == user_id:
        return track
    return None
