from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, MessageReactionUpdated

from app.card_resolve import resolve_track_for_reaction
from app.edit_ui import (
    drive_confirm_keyboard,
    exit_edit_keyboard,
    show_field_menu,
)
from app.genre import genre_tokens
from app.membership import is_forum_member
from app.models import Ctx, Job, PendingReview, TrackRecord, identity_from_dict, tagset_from_dict
from app.relocate import (
    delete_track,
    ensure_local_flac,
    identity_from_track,
    read_tags_for_card,
    relocate_track,
)
from app.tags import normalize_tagset, read_cover, read_tagset, write_tags
from app.util import sanitize_filename

log = logging.getLogger(__name__)

THUMBS_UP = "👍"
THUMBS_DOWN = "👎"
POO = "💩"
MONKEY = "🙉"
FOLDED = "🙏"
WRITING = "✍"

ADD_ONLY = {THUMBS_UP, THUMBS_DOWN, POO, MONKEY, FOLDED}
DELETE_EMOJIS = {POO, MONKEY}

_OP_LABELS = {
    "library": "Move this track to the library on Google Drive?",
    "review": "Move this track to the review folder on Google Drive?",
    "delete": "Delete this track from local disk and Google Drive?",
    "restart": "Re-identify from Telegram, local, or Drive? A successful run replaces the Drive copy.",
}


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(text: str | None, default):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return default


def _expires_at(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _far_expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat(timespec="seconds")


def normalize_emoji(raw: str) -> str:
    return (raw or "").replace("\uFE0F", "").replace("\uFE0E", "")


def reaction_emoji_set(reactions) -> set[str]:
    out: set[str] = set()
    for item in reactions or []:
        emoji = getattr(item, "emoji", None)
        if emoji:
            out.add(normalize_emoji(str(emoji)))
    return out


def added_emojis(old, new) -> set[str]:
    return reaction_emoji_set(new) - reaction_emoji_set(old)


def removed_emojis(old, new) -> set[str]:
    return reaction_emoji_set(old) - reaction_emoji_set(new)


def parse_react_callback(data: str | None) -> tuple[int, str] | None:
    if not data or not data.startswith("r") or ":" not in data:
        return None
    prefix, rest = data.split(":", 1)
    if not prefix[1:].isdigit():
        return None
    if rest not in {"yes", "no", "cancel", "draft", "library"}:
        return None
    return int(prefix[1:]), rest


def build_reactions_router() -> Router:
    router = Router()

    @router.message_reaction()
    async def on_reaction(event: MessageReactionUpdated, ctx: Ctx) -> None:
        user = event.user
        if user is None or user.is_bot:
            return
        if not await is_forum_member(ctx, user.id):
            return
        added = added_emojis(event.old_reaction, event.new_reaction)
        removed = removed_emojis(event.old_reaction, event.new_reaction)
        if not added and not removed:
            return
        want = bool(added & (ADD_ONLY | {WRITING})) or WRITING in removed
        track = ctx.catalog.get_track_by_message(event.chat.id, event.message_id)
        if track is None or track.status == "deleted":
            if want:
                track = await resolve_track_for_reaction(ctx, event)
            if track is None or track.status == "deleted":
                log.debug(
                    "reaction ignored chat=%s message=%s added=%s removed=%s",
                    event.chat.id,
                    event.message_id,
                    added,
                    removed,
                )
                return
        try:
            if WRITING in added:
                await _enter_edit(ctx, event, track)
                return
            if WRITING in removed:
                await _exit_edit_prompt(ctx, event, track)
                return
            for emoji in added & ADD_ONLY:
                await _confirm_reaction(ctx, event, track, emoji)
                return
        except Exception:
            log.exception("reaction handler failed track=%s", track.id)

    @router.callback_query(F.data.regexp(r"^r\d+:"))
    async def on_react_callback(callback: CallbackQuery, ctx: Ctx) -> None:
        parsed = parse_react_callback(callback.data)
        if parsed is None:
            await callback.answer()
            return
        pending_id, action = parsed
        row = ctx.catalog.get_pending_review(pending_id)
        if row is None or row.status != "waiting":
            await callback.answer("Already handled.")
            return
        if callback.message and callback.message.chat.id != row.chat_id:
            await callback.answer()
            return
        if not callback.from_user or not await is_forum_member(ctx, callback.from_user.id):
            await callback.answer("Access denied.", show_alert=True)
            return
        if not ctx.catalog.claim_pending(row.id, "processing"):
            await callback.answer("Already handled.")
            return
        await callback.answer()
        try:
            if row.phase == "react_exit":
                await _handle_exit(ctx, row, action)
            elif row.phase == "react_confirm":
                await _handle_confirm(ctx, row, action)
            else:
                ctx.catalog.update_pending_review(row.id, status="waiting")
        except Exception:
            log.exception("react callback failed id=%s action=%s", row.id, action)
            ctx.catalog.update_pending_review(row.id, status="waiting")
            if callback.message:
                await callback.message.reply("Action failed. Nothing was discarded; retry.")

    return router


def _report_op(report: dict, op: str) -> dict:
    out = dict(report)
    out["react_op"] = op
    return out


async def _reply_card(
    ctx: Ctx, event: MessageReactionUpdated, text: str, markup, *, thread_id: int | None = None
) -> int | None:
    kwargs: dict = {
        "chat_id": event.chat.id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": markup,
    }
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    attempts = [
        {**kwargs, "reply_to_message_id": event.message_id},
        kwargs,
    ]
    if thread_id:
        bare = dict(kwargs)
        bare.pop("message_thread_id", None)
        attempts.append(bare)
    last_exc: Exception | None = None
    for send_kwargs in attempts:
        try:
            sent = await ctx.bot.send_message(**send_kwargs)
            return sent.message_id
        except Exception as exc:
            last_exc = exc
            continue
    log.warning("reaction reply failed: %s", last_exc)
    return None


async def _upsert_react_pending(
    ctx: Ctx,
    track: TrackRecord,
    event: MessageReactionUpdated,
    *,
    phase: str,
    op: str | None,
    staged: str | None,
    status_message_id: int,
    expires_at: str,
    working_json: str,
    original_json: str,
    identity_json: str,
    source_report_json: str,
) -> PendingReview | None:
    existing = ctx.catalog.get_waiting_for_track(track.id)
    thread_id = track.thread_id
    fields = dict(
        phase=phase,
        status="waiting",
        local_path=staged or track.local_path or "",
        sidecar_path=track.sidecar_path,
        relative_path=track.relative_path,
        kind=track.kind,
        original_json=original_json,
        recommended_json=working_json,
        working_json=working_json,
        identity_json=identity_json,
        source_report_json=source_report_json,
        chat_id=event.chat.id,
        thread_id=thread_id,
        status_message_id=status_message_id,
        topic_name=track.topic_name or "",
        file_name=track.file_name or Path(track.relative_path or "track.flac").name,
        track_id=track.id,
        old_drive_id=track.drive_file_id,
        telegram_file_id=track.telegram_file_id,
        expires_at=expires_at,
    )
    if existing and existing.phase.startswith("react"):
        update_fields = {k: v for k, v in fields.items() if k != "chat_id"}
        ctx.catalog.update_pending_review(existing.id, **update_fields)
        return ctx.catalog.get_pending_review(existing.id)
    pending_id = ctx.catalog.insert_pending_review(
        candidates_json=_dumps({"op": op} if op else {}),
        drive_conflicts_json="[]",
        **fields,
    )
    return ctx.catalog.get_pending_review(pending_id)


async def _confirm_reaction(ctx: Ctx, event: MessageReactionUpdated, track: TrackRecord, emoji: str) -> None:
    existing = ctx.catalog.get_waiting_for_track(track.id)
    if existing and existing.phase in {"react_edit", "react_exit"}:
        await _reply_card(
            ctx,
            event,
            "Finish or cancel ✍️ first.",
            None,
            thread_id=track.thread_id,
        )
        return
    if emoji == THUMBS_UP:
        op = "library"
        if track.kind == "library" and track.status == "uploaded":
            await _reply_card(
                ctx, event, "Already in library. No Drive change.", None, thread_id=track.thread_id
            )
            return
    elif emoji == THUMBS_DOWN:
        op = "review"
        if track.kind == "review" and track.status == "uploaded":
            await _reply_card(
                ctx, event, "Already in review. No Drive change.", None, thread_id=track.thread_id
            )
            return
    elif emoji in DELETE_EMOJIS:
        op = "delete"
    else:
        op = "restart"
    tags = read_tags_for_card(track)
    identity = identity_from_track(track)
    report = _report_op(_loads(track.source_report_json, {}), op)
    row = await _upsert_react_pending(
        ctx,
        track,
        event,
        phase="react_confirm",
        op=op,
        staged=None,
        status_message_id=event.message_id if op == "restart" else 0,
        expires_at=_far_expires(),
        working_json=_dumps(asdict(tags)),
        original_json=_dumps(asdict(tags)),
        identity_json=_dumps(asdict(identity)),
        source_report_json=_dumps(report),
    )
    if not row:
        return
    ctx.catalog.update_pending_review(row.id, candidates_json=_dumps({"op": op}))
    if op == "restart":
        from app.queue import _job_from_pending, edit_status

        refreshed = ctx.catalog.get_pending_review(row.id) or row
        await edit_status(
            ctx,
            _job_from_pending(refreshed),
            _OP_LABELS[op],
            drive_confirm_keyboard(row.id),
            fallback_send=event.chat.id > 0,
        )
        ctx.catalog.bind_track_message(track.id, event.chat.id, event.message_id)
        return
    status_id = await _reply_card(
        ctx, event, _OP_LABELS[op], drive_confirm_keyboard(row.id), thread_id=track.thread_id
    )
    if status_id:
        ctx.catalog.update_pending_review(row.id, status_message_id=status_id)


async def _stage_copy(ctx: Ctx, track: TrackRecord) -> Path:
    source = await ensure_local_flac(ctx, track)
    pending_dir = ctx.settings.pending_root / str(uuid.uuid4())
    pending_dir.mkdir(parents=True, exist_ok=True)
    dest = pending_dir / sanitize_filename(source.name)
    await asyncio.to_thread(shutil.copy2, source, dest)
    return dest


async def _enter_edit(ctx: Ctx, event: MessageReactionUpdated, track: TrackRecord) -> None:
    existing = ctx.catalog.get_waiting_for_track(track.id)
    if existing and existing.phase in {"react_edit", "react_exit"}:
        if existing.phase == "react_edit":
            refreshed = ctx.catalog.get_pending_review(existing.id)
            if refreshed:
                try:
                    await show_field_menu(ctx, refreshed)
                except Exception:
                    log.exception("edit UI refresh failed track=%s", track.id)
        return
    try:
        staged = await _stage_copy(ctx, track)
    except Exception:
        log.exception("stage for edit failed track=%s", track.id)
        await _reply_card(ctx, event, "Could not load this file for editing.", None, thread_id=track.thread_id)
        return
    tags = normalize_tagset(await asyncio.to_thread(read_tagset, staged), ctx.genre)
    identity = identity_from_track(track)
    report = _loads(track.source_report_json, {})
    row = await _upsert_react_pending(
        ctx,
        track,
        event,
        phase="react_edit",
        op=None,
        staged=str(staged),
        status_message_id=event.message_id,
        expires_at=_far_expires(),
        working_json=_dumps(asdict(tags)),
        original_json=_dumps(asdict(tags)),
        identity_json=_dumps(asdict(identity)),
        source_report_json=_dumps(report),
    )
    if row:
        try:
            await show_field_menu(ctx, row)
        except Exception:
            log.exception("edit UI failed track=%s", track.id)
            await _reply_card(
                ctx, event, "Could not open the editor. Remove ✍️ and try again.", None, thread_id=track.thread_id
            )


async def _exit_edit_prompt(ctx: Ctx, event: MessageReactionUpdated, track: TrackRecord) -> None:
    existing = ctx.catalog.get_waiting_for_track(track.id)
    if existing is None or existing.phase != "react_edit":
        return
    from app.edit_ui import _clear_edit_cover

    await _clear_edit_cover(ctx, existing)
    ctx.catalog.update_pending_review(
        existing.id,
        phase="react_exit",
        status="waiting",
        expires_at=_expires_at(24),
    )
    from app.queue import _job_from_pending, edit_status

    row = ctx.catalog.get_pending_review(existing.id)
    if row:
        await edit_status(
            ctx,
            _job_from_pending(row),
            "<b>Finish editing</b>\n"
            "Cancel discards this session.\n"
            "Save draft writes to Drive review.\n"
            "Commit to library writes to Drive library.",
            exit_edit_keyboard(row.id),
        )


def _drop_stage(ctx: Ctx, row: PendingReview) -> None:
    if not row.local_path:
        return
    path = Path(row.local_path)
    try:
        path.resolve().relative_to(Path(ctx.settings.pending_root).resolve())
    except ValueError:
        return
    shutil.rmtree(path.parent, ignore_errors=True)


async def _handle_confirm(ctx: Ctx, row: PendingReview, action: str) -> None:
    from app.queue import _job_from_pending, edit_status

    job = _job_from_pending(row)
    op = str((_loads(row.candidates_json, {}) or {}).get("op") or _loads(row.source_report_json, {}).get("react_op") or "")
    if action == "no":
        ctx.catalog.update_pending_review(row.id, status="cancelled")
        if op == "restart" and row.track_id:
            from app.edit_ui import show_saved_card

            await show_saved_card(ctx, row, fallback_send=row.chat_id > 0)
        else:
            await edit_status(ctx, job, "Cancelled. No Drive change.")
        return
    if action != "yes":
        ctx.catalog.update_pending_review(row.id, status="waiting")
        return
    track = ctx.catalog.get_track(row.track_id) if row.track_id else None
    if track is None:
        ctx.catalog.update_pending_review(row.id, status="failed")
        await edit_status(ctx, job, "Track is gone.")
        return
    tags = tagset_from_dict(_loads(row.working_json, {}))
    identity = identity_from_dict(_loads(row.identity_json, {}))
    report = _loads(row.source_report_json, {})
    try:
        if op == "library":
            if track.kind == "library" and track.status == "uploaded":
                ctx.catalog.update_pending_review(row.id, status="done")
                await edit_status(ctx, job, "Already in library.")
                return
            await relocate_track(
                ctx,
                track,
                kind="library",
                tags=tags,
                identity=identity,
                source_report=report,
                topic_name=row.topic_name or track.topic_name or "General",
                file_name=row.file_name or track.file_name or "track.flac",
            )
            await edit_status(ctx, job, "Moved to library on Google Drive.")
        elif op == "review":
            if track.kind == "review" and track.status == "uploaded":
                ctx.catalog.update_pending_review(row.id, status="done")
                await edit_status(ctx, job, "Already in review.")
                return
            await relocate_track(
                ctx,
                track,
                kind="review",
                tags=tags,
                identity=identity,
                source_report=report,
                topic_name=row.topic_name or track.topic_name or "General",
                file_name=row.file_name or track.file_name or "track.flac",
            )
            await edit_status(ctx, job, "Moved to review on Google Drive.")
        elif op == "delete":
            await delete_track(ctx, track)
            await edit_status(ctx, job, "Deleted from disk and Google Drive.")
        elif op == "restart":
            await _restart_track(ctx, row, track)
            return
        else:
            ctx.catalog.update_pending_review(row.id, status="waiting")
            await edit_status(ctx, job, "Unknown action.")
            return
    except Exception:
        log.exception("react confirm op=%s failed track=%s", op, track.id)
        ctx.catalog.update_pending_review(row.id, status="waiting")
        await edit_status(ctx, job, "Drive action failed. Check logs.")
        return
    ctx.catalog.update_pending_review(row.id, status="done")


async def _restart_source(ctx: Ctx, track: TrackRecord) -> Path:
    from app.botapi import discard_download

    name = sanitize_filename(Path(track.file_name or track.relative_path or "track.flac").name)
    if not name.lower().endswith(".flac"):
        name = f"{name}.flac"
    dest = ctx.settings.pending_root / str(uuid.uuid4()) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if track.telegram_file_id:
        try:
            file = await ctx.bot.get_file(track.telegram_file_id)
            await ctx.bot.download(file, destination=dest)
            await asyncio.to_thread(discard_download, file.file_path)
            if dest.is_file() and dest.stat().st_size > 0:
                log.info("restart source=telegram track=%s", track.id)
                return dest
        except Exception:
            log.warning("telegram original unavailable track=%s", track.id, exc_info=True)
            shutil.rmtree(dest.parent, ignore_errors=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
    local = await ensure_local_flac(ctx, track)
    await asyncio.to_thread(shutil.copy2, local, dest)
    log.info("restart source=local/drive track=%s path=%s", track.id, dest)
    return dest


async def _send_listen_copy(ctx: Ctx, chat_id: int, path: Path) -> None:
    try:
        await ctx.bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(path, filename=path.name),
        )
    except Exception:
        log.warning("dm listen send failed chat=%s", chat_id, exc_info=True)


async def _restart_track(ctx: Ctx, row: PendingReview, track: TrackRecord) -> None:
    from app.edit_ui import show_saved_card
    from app.queue import _job_from_pending, edit_status

    job = _job_from_pending(row)
    reuse_card = row.chat_id < 0
    if ctx.jobs is None:
        ctx.catalog.update_pending_review(row.id, status="waiting")
        await show_saved_card(ctx, row, prefix="Cannot restart: worker unavailable.", fallback_send=not reuse_card)
        return
    await edit_status(ctx, job, "Looking up original file…", fallback_send=not reuse_card)
    try:
        source = await _restart_source(ctx, track)
    except Exception:
        log.exception("restart source failed track=%s", track.id)
        ctx.catalog.update_pending_review(row.id, status="waiting")
        await show_saved_card(ctx, row, prefix="Cannot restart: no Telegram, local, or Drive file.", fallback_send=not reuse_card)
        return
    if row.chat_id > 0:
        await _send_listen_copy(ctx, row.chat_id, source)
    ctx.catalog.update_pending_review(
        row.id,
        phase="intake",
        status="queued",
        local_path=str(source),
        telegram_file_id=track.telegram_file_id,
        replace_id=track.id,
        old_drive_id=track.drive_file_id,
        expires_at=_expires_at(24),
    )
    await ctx.jobs.put(
        Job(
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            topic_name=row.topic_name or track.topic_name or "General",
            file_id="",
            file_name=row.file_name or track.file_name or source.name,
            status_message_id=row.status_message_id,
            local_path=str(source),
            private=row.chat_id > 0,
            source_pending_id=row.id,
            fallback_send=not reuse_card,
            source_message_id=getattr(track, "source_message_id", None) or 0,
        )
    )
    await edit_status(ctx, job, "Restarting from the original file…", fallback_send=not reuse_card)


async def _apply_manual_cover(row: PendingReview, tags, local: Path) -> None:
    report = _loads(row.source_report_json, {})
    manual = report.get("manual_cover") or {"mode": "keep"}
    cover, mime = await asyncio.to_thread(read_cover, local)
    if manual.get("mode") == "remove":
        cover, mime = None, None
    elif manual.get("mode") == "replace":
        cover_path = Path(row.local_path).parent / str(manual.get("path") or "")
        if cover_path.is_file():
            cover, mime = cover_path.read_bytes(), "image/jpeg"
    await asyncio.to_thread(write_tags, local, tags, cover, mime)


async def _handle_exit(ctx: Ctx, row: PendingReview, action: str) -> None:
    from app.edit_ui import _clear_edit_cover, show_saved_card
    from app.queue import _job_from_pending, edit_status

    job = _job_from_pending(row)
    track = ctx.catalog.get_track(row.track_id) if row.track_id else None
    if action == "cancel":
        await _clear_edit_cover(ctx, row)
        _drop_stage(ctx, row)
        ctx.catalog.update_pending_review(row.id, status="cancelled")
        await show_saved_card(ctx, row, prefix="Edits discarded. Files unchanged.")
        return
    if track is None:
        ctx.catalog.update_pending_review(row.id, status="failed")
        await edit_status(ctx, job, "Track is gone.")
        return
    if action not in {"draft", "library"}:
        ctx.catalog.update_pending_review(row.id, status="waiting")
        return
    tags = tagset_from_dict(_loads(row.working_json, {}))
    identity = identity_from_dict(_loads(row.identity_json, {}))
    report = _loads(row.source_report_json, {})
    staged = Path(row.local_path) if row.local_path else None
    if staged is None or not staged.is_file():
        ctx.catalog.update_pending_review(row.id, status="waiting")
        await edit_status(ctx, job, "Staged file missing. Add ✍️ and try again.")
        return
    if tags.genre:
        tags = replace(tags, genre=ctx.genre.classify(genre_tokens(tags.genre)))
    try:
        await _clear_edit_cover(ctx, row)
        await _apply_manual_cover(row, tags, staged)
        kind = "review" if action == "draft" else "library"
        await relocate_track(
            ctx,
            track,
            kind=kind,
            tags=tags,
            identity=identity,
            source_report=report,
            topic_name=row.topic_name or track.topic_name or "General",
            file_name=row.file_name or track.file_name or "track.flac",
            staged=staged,
        )
        shutil.rmtree(staged.parent, ignore_errors=True)
    except Exception:
        log.exception("edit exit %s failed track=%s", action, track.id)
        ctx.catalog.update_pending_review(row.id, status="waiting")
        await edit_status(ctx, job, "Could not write to Drive. Check logs.")
        return
    ctx.catalog.update_pending_review(row.id, status="done")
    dest = "review" if action == "draft" else "library"
    await show_saved_card(ctx, row, prefix=f"Saved to Drive {dest}.")


async def expire_react_exit(ctx: Ctx, row: PendingReview) -> None:
    from app.edit_ui import _clear_edit_cover, show_saved_card

    await _clear_edit_cover(ctx, row)
    _drop_stage(ctx, row)
    ctx.catalog.update_pending_review(row.id, status="cancelled")
    await show_saved_card(ctx, row, prefix="Edit timed out. Changes discarded.")
