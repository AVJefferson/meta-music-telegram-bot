from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from app.genre import genre_tokens
from app.library import library_relative, place_file, review_relative, rmdir_empty, unlink_quiet, write_sidecar
from app.models import Ctx, Identity, TagSet, TrackRecord, identity_from_dict, tagset_from_dict
from app.tags import overlay_tagset, read_cover, read_tagset, write_tags
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
    if track.identity_json:
        return identity_from_dict(_loads(track.identity_json, {}))
    report = _loads(track.source_report_json, {})
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


def drive_root_id(ctx: Ctx, kind: str) -> str:
    if kind == "library":
        return ctx.settings.gdrive_folder_id
    return ctx.settings.gdrive_review_folder_id


async def _resolve_drive_file_id(ctx: Ctx, track: TrackRecord) -> str | None:
    if track.drive_file_id:
        return track.drive_file_id
    if not track.relative_path:
        return None
    relative = Path(track.relative_path)
    root = drive_root_id(ctx, track.kind)
    parent = await asyncio.to_thread(ctx.drive.find_path, root, list(relative.parts[:-1]))
    if not parent:
        return None
    hits = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent, relative.name)
    return hits[0].id if hits else None


async def ensure_local_flac(ctx: Ctx, track: TrackRecord) -> Path:
    async with _flac_lock(track.id):
        return await _ensure_local_flac_locked(ctx, ctx.catalog.get_track(track.id) or track)


async def _ensure_local_flac_locked(ctx: Ctx, track: TrackRecord) -> Path:
    candidates: list[Path] = []
    if track.local_path:
        candidates.append(Path(track.local_path))
    if track.relative_path:
        root = ctx.settings.library_root if track.kind == "library" else ctx.settings.review_root
        candidates.append(root / track.relative_path)
    for path in candidates:
        if path.is_file():
            if str(path) != (track.local_path or ""):
                ctx.catalog.update_track(track.id, local_path=str(path))
            return path

    file_id = await _resolve_drive_file_id(ctx, track)
    if not file_id:
        raise FileNotFoundError("Track has no local file and no Drive copy.")
    name = Path(track.relative_path or track.file_name or "track.flac").name
    dest = ctx.settings.pending_root / str(uuid.uuid4()) / sanitize_filename(name)
    log.info("downloading track=%s from Drive id=%s to %s", track.id, file_id, dest)
    await asyncio.to_thread(ctx.drive.download_to, file_id, dest)
    ctx.catalog.update_track(track.id, local_path=str(dest), drive_file_id=file_id)
    return dest


async def _delete_drive_named(ctx: Ctx, kind: str, relative: Path | None, extra_ids: list[str | None]) -> None:
    seen: set[str] = set()
    for file_id in extra_ids:
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)
        try:
            await asyncio.to_thread(ctx.drive.delete_file, file_id)
        except Exception:
            log.warning("Drive delete failed id=%s", file_id, exc_info=True)
    if relative is None:
        return
    root = drive_root_id(ctx, kind)
    parent = await asyncio.to_thread(ctx.drive.find_path, root, list(relative.parts[:-1]))
    if not parent:
        return
    for name in (relative.name, relative.with_suffix(".json").name, relative.with_suffix(".log").name):
        try:
            hits = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent, name)
        except Exception:
            log.debug("Drive sibling lookup failed name=%s", name, exc_info=True)
            continue
        for hit in hits:
            if hit.id in seen:
                continue
            seen.add(hit.id)
            try:
                await asyncio.to_thread(ctx.drive.delete_file, hit.id)
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
) -> TrackRecord:
    local = staged if staged and staged.is_file() else await ensure_local_flac(ctx, track)
    if tags.genre:
        tags = replace(tags, genre=ctx.genre.classify(genre_tokens(tags.genre)))
    cover, mime = await asyncio.to_thread(read_cover, local)
    await asyncio.to_thread(write_tags, local, tags, cover, mime)

    old_kind = track.kind
    old_relative = Path(track.relative_path) if track.relative_path else None
    old_local = Path(track.local_path) if track.local_path else None

    if kind == "library":
        relative = library_relative(topic_name, tags)
        dest = ctx.settings.library_root / relative
        root_id = ctx.settings.gdrive_folder_id
        sidecar_path: Path | None = None
    else:
        relative = review_relative(file_name)
        dest = ctx.settings.review_root / relative
        root_id = ctx.settings.gdrive_review_folder_id
        sidecar_path = dest.with_suffix(".json")

    if local.resolve() != dest.resolve():
        dest = await asyncio.to_thread(place_file, local, dest)

    parent_id = await asyncio.to_thread(ctx.drive.ensure_parent, root_id, relative)
    filename = relative.name
    conflicts = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent_id, filename)
    try:
        if conflicts:
            file_id, url = await asyncio.to_thread(
                ctx.drive.replace_file, conflicts[0].id, dest, "audio/flac"
            )
        else:
            file_id, url = await asyncio.to_thread(
                ctx.drive.create_file, dest, parent_id, filename, "audio/flac"
            )
    except Exception:
        log.exception("Drive relocate upload failed track=%s kind=%s", track.id, kind)
        raise

    sidecar_id = None
    if kind == "review":
        sidecar_path, sidecar_id = await _upload_review_sidecar(
            ctx, dest, parent_id, topic_name, file_name, tags, identity
        )
    log_id = await _upload_songlog(ctx, dest, parent_id, source_report, tags, identity)

    same_drive = bool(track.drive_file_id and track.drive_file_id == file_id)
    if not same_drive:
        await _delete_drive_named(
            ctx,
            old_kind,
            old_relative,
            [track.drive_file_id, track.drive_sidecar_id, track.drive_log_id],
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
    )
    refreshed = ctx.catalog.get_track(track.id)
    assert refreshed is not None
    return refreshed


async def delete_track(ctx: Ctx, track: TrackRecord) -> None:
    relative = Path(track.relative_path) if track.relative_path else None
    await _delete_drive_named(
        ctx,
        track.kind,
        relative,
        [track.drive_file_id, track.drive_sidecar_id, track.drive_log_id],
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


async def hydrate_track_tags(ctx: Ctx, track: TrackRecord) -> TrackRecord:
    local = await ensure_local_flac(ctx, track)
    tags = await asyncio.to_thread(read_tagset, local)
    fields: dict[str, object] = {"local_path": str(local)}
    if any(asdict(tags).values()):
        fields["title"] = tags.title or track.title
        fields["artist"] = tags.artist or track.artist
        fields["album"] = tags.album or track.album
        fields["tags_json"] = json.dumps(asdict(tags), ensure_ascii=False)
    ctx.catalog.update_track(track.id, **fields)
    return ctx.catalog.get_track(track.id) or track
