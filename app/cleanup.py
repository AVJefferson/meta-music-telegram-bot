from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.botapi import sweep_downloads
from app.covers import purge_stale_covers, upload_album_cover_if_missing
from app.drive import resolve_drive, user_drive_root
from app.library import rmdir_empty, unlink_quiet
from app.models import Ctx, TrackRecord
from app.notify import alert_admin, notify_user
from app.paths import cache_path
from app.relocate import ctx_for_user
from app.tags import read_cover

log = logging.getLogger(__name__)
LOCAL_TTL_DAYS = 7
LOGGED_OUT_TTL_DAYS = LOCAL_TTL_DAYS
CACHE_TTL_DAYS = LOCAL_TTL_DAYS


async def alert_general(ctx: Ctx, text: str, fallback_thread_id: int | None = None) -> None:
    """Infra / leftover alias — admin DM only. Does not post to a shared group."""
    await alert_admin(ctx, text)


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _parse_utc(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _mtime_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _within_ttl(when: datetime | None, *, days: int = LOCAL_TTL_DAYS) -> bool:
    if when is None:
        return False
    return when > datetime.now(timezone.utc) - timedelta(days=days)


def _uploaded_local_stamp(row: TrackRecord) -> datetime | None:
    for raw in (row.uploaded_at, row.created_at):
        parsed = _parse_utc(raw)
        if parsed:
            return parsed
    local = row.local
    if local is None:
        return None
    return _mtime_utc(local)


def _iso_months_ago(months: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat(timespec="seconds")


async def _retry_row(ctx: Ctx, row: TrackRecord) -> None:
    uid = getattr(row, "user_id", 0) or 0
    user = ctx.catalog.get_user(uid) if uid else None
    if user and not user.logged_in:
        return
    bound = ctx_for_user(ctx, uid) if uid else ctx
    local = row.local
    if local is None or not local.exists():
        if uid:
            await notify_user(bound, uid, "Drive retry failed: local file missing.")
        return
    root = user_drive_root(bound, row.kind, uid)
    if not root:
        return
    relative = Path(row.relative_path or local.name)
    mime = "application/json" if local.suffix.lower() == ".json" else "audio/flac"
    try:
        file_id, url = await _upload(bound, local, root, relative, mime)
        bound.catalog.mark_uploaded(row.id, file_id, url)
        if row.sidecar_path:
            sidecar = Path(row.sidecar_path)
            if sidecar.exists():
                await _upload(bound, sidecar, root, relative.with_suffix(".json"), "application/json")
        if row.kind == "library" and local.suffix.lower() == ".flac":
            try:
                parent = await asyncio.to_thread(bound.drive.ensure_parent, root, relative)
                cover, cover_mime = await asyncio.to_thread(read_cover, local)
                if cover:
                    await asyncio.to_thread(upload_album_cover_if_missing, bound, parent, cover, cover_mime)
            except Exception:
                log.warning("album cover retry upload failed", exc_info=True)
        log.info("retried upload ok id=%s", row.id)
    except Exception as exc:
        bound.catalog.mark_failed(row.id, str(exc)[:500])
        if uid:
            await notify_user(bound, uid, "Drive upload failed. Retry later or /login.")


async def _upload(ctx: Ctx, local: Path, root: str, relative: Path, mime: str) -> tuple[str, str | None]:
    return await asyncio.to_thread(ctx.drive.upload_with_retry, local, root, relative, mime)


async def _purge_uploaded_local(ctx: Ctx, row: TrackRecord) -> None:
    if not row.drive_file_id:
        return
    if _within_ttl(_uploaded_local_stamp(row)):
        return
    uid = getattr(row, "user_id", 0) or 0
    bound = ctx_for_user(ctx, uid) if uid else ctx
    drive = resolve_drive(bound, uid) or bound.drive
    exists = await asyncio.to_thread(drive.file_exists, row.drive_file_id)
    if not exists:
        log.warning("drive file missing for track %s, re-uploading", row.id)
        bound.catalog.mark_failed(row.id, "drive file missing on cleanup")
        await _retry_row(bound, row)
        return
    unlink_quiet(row.local)
    if row.sidecar_path:
        unlink_quiet(Path(row.sidecar_path))
    bound.catalog.clear_local_paths(row.id)


def _purge_tmp(tmp_root: Path) -> None:
    if not tmp_root.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOCAL_TTL_DAYS)
    for child in tmp_root.iterdir():
        mtime = _mtime_utc(child)
        if mtime is None or mtime > cutoff:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            unlink_quiet(child)


def _purge_logged_out_locals(ctx: Ctx) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOGGED_OUT_TTL_DAYS)
    for track in ctx.catalog.list_owned_tracks():
        uid = getattr(track, "user_id", 0) or 0
        if not uid:
            continue
        user = ctx.catalog.get_user(uid)
        if user and user.logged_in:
            continue
        if track.status in {"processing", "uploading"}:
            continue
        local = track.local
        if local is None or not local.exists():
            continue
        mtime = _mtime_utc(local)
        if mtime is None or mtime > cutoff:
            continue
        unlink_quiet(local)
        if track.sidecar_path:
            unlink_quiet(Path(track.sidecar_path))
        ctx.catalog.clear_local_paths(track.id)


def _purge_cache(ctx: Ctx) -> None:
    cutoff = _iso_days_ago(CACHE_TTL_DAYS)
    for sha, stored in ctx.catalog.list_cache_older_than(cutoff):
        try:
            path = cache_path(ctx, sha)
        except Exception:
            path = Path(stored)
        unlink_quiet(path)
        ctx.catalog.delete_cached_file(sha)


async def _forget_inactive(ctx: Ctx) -> None:
    months = int(getattr(ctx.settings, "user_inactive_months", 3) or 3)
    before = _iso_months_ago(months)
    for user in ctx.catalog.list_inactive_users(before):
        uid = user.telegram_user_id
        if int(getattr(ctx.settings, "admin_telegram_user_id", 0) or 0) == uid:
            continue
        actives = ctx.catalog.list_pending_by_status("processing", "uploading", "queued")
        if any(getattr(row, "user_id", 0) == uid for row in actives):
            continue
        ctx.catalog.clear_user_google(uid)
        ctx.catalog.forget_user(uid)
        for track in ctx.catalog.delete_user_tracks(uid):
            unlink_quiet(track.local)
            if track.sidecar_path:
                unlink_quiet(Path(track.sidecar_path))
        hub = getattr(ctx, "drive", None)
        drop = getattr(hub, "drop", None)
        if callable(drop):
            drop(uid)
        log.info("forgot inactive user=%s", uid)


async def run_cleanup(ctx: Ctx) -> None:
    log.info("cleanup start")
    await run_expire_pending(ctx)
    for row in ctx.catalog.list_failed():
        await _retry_row(ctx, row)
    for row in ctx.catalog.list_uploaded_with_local():
        await _purge_uploaded_local(ctx, row)
    _purge_logged_out_locals(ctx)
    _purge_cache(ctx)
    await _forget_inactive(ctx)
    rmdir_empty(ctx.settings.library_root)
    rmdir_empty(ctx.settings.review_root)
    cache_root = Path(getattr(ctx.settings, "cache_root", "/data/cache"))
    if cache_root.exists():
        rmdir_empty(cache_root)
    _purge_tmp(ctx.settings.tmp_root)
    removed = purge_stale_covers(ctx.settings.covers_root)
    if removed:
        log.info("purged %s stale album cover(s)", removed)
    leftovers = await asyncio.to_thread(sweep_downloads)
    if leftovers:
        log.info("removed %s leftover Bot API download(s)", leftovers)
    pruned = ctx.catalog.prune_finished_pending()
    if pruned:
        log.info("pruned %s finished pending row(s)", pruned)
    suggest_pruned = ctx.catalog.prune_suggest_state()
    if suggest_pruned:
        log.info("pruned %s expired suggest cache row(s)", suggest_pruned)
    await _prune_drive_review_folders(ctx)
    log.info("cleanup done")


async def _prune_drive_review_folders(ctx: Ctx) -> None:
    for user in ctx.catalog.list_active_users(_iso_months_ago(int(getattr(ctx.settings, "user_inactive_months", 3) or 3))):
        if not user.gdrive_review_folder_id:
            continue
        bound = ctx_for_user(ctx, user.telegram_user_id)
        try:
            removed = await asyncio.to_thread(bound.drive.prune_empty_folders, user.gdrive_review_folder_id)
        except Exception:
            log.warning("drive review folder prune failed user=%s", user.telegram_user_id, exc_info=True)
            continue
        if removed:
            log.info("removed %s empty Drive review folder(s) user=%s", removed, user.telegram_user_id)


async def run_expire_pending(ctx: Ctx) -> None:
    from app.queue import expire_pending

    await expire_pending(ctx)
