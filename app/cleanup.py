from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from app.botapi import sweep_downloads
from app.covers import purge_stale_covers, upload_album_cover_if_missing
from app.library import rmdir_empty, unlink_quiet
from app.models import Ctx, TrackRecord
from app.tags import read_cover
from app.util import html_esc

log = logging.getLogger(__name__)


async def alert_general(ctx: Ctx, text: str, fallback_thread_id: int | None = None) -> None:
    chat_id = ctx.settings.allowed_chat_id
    candidates: list[int | None] = [ctx.settings.alert_thread_id]
    if fallback_thread_id and fallback_thread_id not in candidates:
        candidates.append(fallback_thread_id)
    candidates.append(None)
    seen: set[int | None] = set()
    for thread in candidates:
        if thread in seen:
            continue
        seen.add(thread)
        try:
            kwargs: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if thread:
                kwargs["message_thread_id"] = thread
            await ctx.bot.send_message(**kwargs)
            return
        except Exception as exc:
            log.warning("alert failed thread_id=%s: %s", thread, exc)
    log.error("could not send Drive alert to Telegram")


async def _retry_row(ctx: Ctx, row: TrackRecord) -> None:
    local = row.local
    if local is None or not local.exists():
        await alert_general(
            ctx,
            f"<b>Upload retry failed</b>\nLocal file missing for <code>{html_esc(row.title or row.id)}</code>",
        )
        return
    root = (
        ctx.settings.gdrive_folder_id
        if row.kind == "library"
        else ctx.settings.gdrive_review_folder_id
    )
    relative = Path(row.relative_path or local.name)
    mime = "application/json" if local.suffix.lower() == ".json" else "audio/flac"
    try:
        file_id, url = await _upload(ctx, local, root, relative, mime)
        ctx.catalog.mark_uploaded(row.id, file_id, url)
        if row.sidecar_path:
            sidecar = Path(row.sidecar_path)
            if sidecar.exists():
                await _upload(
                    ctx,
                    sidecar,
                    root,
                    relative.with_suffix(".json"),
                    "application/json",
                )
        if row.kind == "library" and local.suffix.lower() == ".flac":
            try:
                parent = await asyncio.to_thread(ctx.drive.ensure_parent, root, relative)
                cover, cover_mime = await asyncio.to_thread(read_cover, local)
                if cover:
                    await asyncio.to_thread(
                        upload_album_cover_if_missing, ctx, parent, cover, cover_mime
                    )
            except Exception:
                log.warning("album cover retry upload failed", exc_info=True)
        log.info("retried upload ok id=%s", row.id)
    except Exception as exc:
        ctx.catalog.mark_failed(row.id, str(exc))
        await alert_general(
            ctx,
            "<b>Drive upload failed</b> (retry)\n"
            f"Title: {html_esc(row.title or local.name)}\n"
            f"Local: <code>{html_esc(local)}</code>\n"
            f"Error: {html_esc(exc)}",
        )


async def _upload(ctx: Ctx, local: Path, root: str, relative: Path, mime: str) -> tuple[str, str | None]:
    return await asyncio.to_thread(ctx.drive.upload_with_retry, local, root, relative, mime)


async def _purge_uploaded_local(ctx: Ctx, row: TrackRecord) -> None:
    if not row.drive_file_id:
        return
    exists = await asyncio.to_thread(ctx.drive.file_exists, row.drive_file_id)
    if not exists:
        log.warning("drive file missing for track %s, re-uploading", row.id)
        ctx.catalog.mark_failed(row.id, "drive file missing on cleanup")
        await _retry_row(ctx, row)
        return
    unlink_quiet(row.local)
    if row.sidecar_path:
        unlink_quiet(Path(row.sidecar_path))
    ctx.catalog.clear_local_paths(row.id)


def _purge_tmp(tmp_root: Path) -> None:
    if not tmp_root.exists():
        return
    for child in tmp_root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            unlink_quiet(child)


async def run_cleanup(ctx: Ctx) -> None:
    log.info("weekly cleanup start")
    await run_expire_pending(ctx)
    for row in ctx.catalog.list_failed():
        await _retry_row(ctx, row)
    for row in ctx.catalog.list_uploaded_with_local():
        await _purge_uploaded_local(ctx, row)
    rmdir_empty(ctx.settings.library_root)
    rmdir_empty(ctx.settings.review_root)
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
    log.info("weekly cleanup done")


async def _prune_drive_review_folders(ctx: Ctx) -> None:
    try:
        removed = await asyncio.to_thread(
            ctx.drive.prune_empty_folders, ctx.settings.gdrive_review_folder_id
        )
    except Exception:
        log.warning("drive review folder prune failed", exc_info=True)
        return
    if removed:
        log.info("removed %s empty Drive review folder(s)", removed)


async def run_expire_pending(ctx: Ctx) -> None:
    from app.queue import expire_pending

    await expire_pending(ctx)
