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
from aiogram.types import CallbackQuery, MessageReactionUpdated

from app.card_resolve import resolve_track_for_reaction
from app.edit_ui import (
    drive_confirm_keyboard,
    exit_edit_keyboard,
    show_field_menu,
)
from app.genre import genre_tokens
from app.membership import allow_from_callback, allow_user, touch, user_is_bot
from app.models import Ctx, Job, PendingReview, TrackRecord, tagset_from_dict
from app.relocate import (
    copy_track_for_user,
    ctx_for_user,
    delete_track,
    ensure_local_flac,
    identity_from_track,
    read_tags_for_card,
    user_copy_of,
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
    "library": "Copy the current Telegram file into your library?",
    "review": "Copy the current Telegram file into your review folder?",
    "move_library": "This is in your review folder. Move that Drive copy to your library?",
    "move_review": "This is in your library. Move that Drive copy to your review folder?",
    "delete": "Delete this track from your library/review only?",
    "restart": "Re-identify this group/channel file? Drive copies stay as they are until someone 👍 or 👎.",
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
    if rest not in {"yes", "no", "cancel", "draft", "library", "commit"}:
        return None
    return int(prefix[1:]), rest


def thumb_plan(want: str, mine: TrackRecord | None) -> tuple[str, str]:
    """Map 👍/👎 plus the user's existing copy to an op and confirm prompt."""
    have = mine is not None and mine.status == "uploaded"
    if have and mine.kind == want:
        place = "library" if want == "library" else "review folder"
        return "skip", f"Already in your {place}."
    if have and mine.kind == "review" and want == "library":
        return "move_library", _OP_LABELS["move_library"]
    if have and mine.kind == "library" and want == "review":
        return "move_review", _OP_LABELS["move_review"]
    if want == "library":
        return "library", _OP_LABELS["library"]
    return "review", _OP_LABELS["review"]


def build_reactions_router() -> Router:
    router = Router()

    @router.message_reaction()
    async def on_reaction(event: MessageReactionUpdated, ctx: Ctx) -> None:
        user = event.user
        if user is None or user_is_bot(user, ctx.bot):
            return
        if not await allow_user(ctx, user.id, event.chat):
            return
        touch(ctx, user.id, user)
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
        if not await allow_from_callback(ctx, callback):
            await callback.answer("Access denied.", show_alert=True)
            return
        if row.user_id and callback.from_user.id != row.user_id:
            await callback.answer("Not your prompt.", show_alert=True)
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
    from app.ephemeral import send_private

    user_id = event.user.id if event.user else 0
    try:
        sent = await send_private(
            ctx,
            chat_id=event.chat.id,
            user_id=user_id,
            text=text,
            thread_id=thread_id,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return sent.message_id if sent else None
    except Exception as exc:
        log.warning("reaction reply failed: %s", exc)
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
    existing = ctx.catalog.get_waiting_for_track(track.id, event.user.id if event.user else None)
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
        user_id=event.user.id if event.user else 0,
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
    user_id = event.user.id if event.user else 0
    existing = ctx.catalog.get_waiting_for_track(track.id, user_id)
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
        want = "library"
        mine = user_copy_of(ctx, track, user_id)
        op, prompt = thumb_plan(want, mine)
        if op == "skip":
            await _reply_card(ctx, event, prompt, None, thread_id=track.thread_id)
            return
    elif emoji == THUMBS_DOWN:
        want = "review"
        mine = user_copy_of(ctx, track, user_id)
        op, prompt = thumb_plan(want, mine)
        if op == "skip":
            await _reply_card(ctx, event, prompt, None, thread_id=track.thread_id)
            return
    elif emoji in DELETE_EMOJIS:
        op = "delete"
        prompt = _OP_LABELS[op]
        mine = None
    else:
        op = "restart"
        prompt = _OP_LABELS[op]
        mine = None
    tags = read_tags_for_card(track)
    identity = identity_from_track(track)
    report = _report_op(_loads(track.source_report_json, {}), op)
    if op == "restart":
        report["drive_dest"] = "none"
        report["dest_confirmed"] = True
        report["correct_telegram"] = True
    payload = {"op": op}
    if mine is not None and op.startswith("move_"):
        payload["copy_id"] = mine.id
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
    ctx.catalog.update_pending_review(row.id, candidates_json=_dumps(payload))
    if op == "restart":
        from app.queue import _job_from_pending, edit_status

        refreshed = ctx.catalog.get_pending_review(row.id) or row
        await edit_status(
            ctx,
            _job_from_pending(refreshed),
            prompt,
            drive_confirm_keyboard(row.id),
            fallback_send=event.chat.id > 0,
        )
        ctx.catalog.bind_track_message(track.id, event.chat.id, event.message_id)
        return
    status_id = await _reply_card(
        ctx, event, prompt, drive_confirm_keyboard(row.id), thread_id=track.thread_id
    )
    if status_id:
        ctx.catalog.update_pending_review(row.id, status_message_id=status_id)


async def _stage_copy(ctx: Ctx, track: TrackRecord, user_id: int = 0) -> Path:
    uid = getattr(track, "user_id", 0) or user_id or 0
    if uid:
        ctx = ctx_for_user(ctx, uid)
    source = await ensure_local_flac(ctx, track)
    pending_dir = ctx.settings.pending_root / str(uuid.uuid4())
    pending_dir.mkdir(parents=True, exist_ok=True)
    dest = pending_dir / sanitize_filename(source.name)
    await asyncio.to_thread(shutil.copy2, source, dest)
    return dest


async def _enter_edit(ctx: Ctx, event: MessageReactionUpdated, track: TrackRecord) -> None:
    existing = ctx.catalog.get_waiting_for_track(track.id, event.user.id if event.user else None)
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
        staged = await _stage_copy(ctx, track, user_id=event.user.id if event.user else 0)
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
    existing = ctx.catalog.get_waiting_for_track(track.id, event.user.id if event.user else None)
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
            "Commit replaces the audio in this group or channel.\n"
            "Drive copies stay as they are until someone 👍 or 👎.\n"
            "Cancel discards this session.",
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
    payload = _loads(row.candidates_json, {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    op = str(payload.get("op") or _loads(row.source_report_json, {}).get("react_op") or "")
    copy_id = payload.get("copy_id")
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
    try:
        if op == "library":
            await copy_track_for_user(ctx, track, row.user_id or 0, kind="library")
            await edit_status(ctx, job, "Copied the Telegram file to your library.")
        elif op == "review":
            await copy_track_for_user(ctx, track, row.user_id or 0, kind="review")
            await edit_status(ctx, job, "Copied the Telegram file to your review folder.")
        elif op in {"move_library", "move_review"}:
            from app.relocate import hydrate_track_tags, identity_from_track, relocate_track, tags_from_track

            mine = None
            if copy_id is not None:
                try:
                    mine = ctx.catalog.get_track(int(copy_id))
                except (TypeError, ValueError):
                    mine = None
            if mine is None:
                mine = user_copy_of(ctx, track, row.user_id or 0)
            if mine is None or getattr(mine, "user_id", 0) != (row.user_id or 0):
                ctx.catalog.update_pending_review(row.id, status="failed")
                await edit_status(ctx, job, "Nothing to move in your library.")
                return
            kind = "library" if op == "move_library" else "review"
            try:
                mine = await hydrate_track_tags(ctx, mine)
            except Exception:
                log.debug("hydrate before move failed track=%s", mine.id, exc_info=True)
                mine = ctx.catalog.get_track(mine.id) or mine
            await relocate_track(
                ctx,
                mine,
                kind=kind,
                tags=tags_from_track(mine),
                identity=identity_from_track(mine),
                source_report=_loads(mine.source_report_json, {}) or {},
                topic_name=mine.topic_name or "Unknown",
                file_name=mine.file_name or "track.flac",
            )
            dest = "library" if kind == "library" else "review folder"
            await edit_status(ctx, job, f"Moved to your {dest}.")
        elif op == "delete":
            mine = user_copy_of(ctx, track, row.user_id or 0)
            if mine is None:
                ctx.catalog.update_pending_review(row.id, status="done")
                await edit_status(ctx, job, "Nothing to delete in your library.")
                return
            await delete_track(ctx, mine)
            await edit_status(ctx, job, "Removed from your library/review.")
        elif op == "restart":
            await _restart_track(ctx, row, track)
            return
        else:
            ctx.catalog.update_pending_review(row.id, status="waiting")
            await edit_status(ctx, job, "Unknown action.")
            return
    except Exception as exc:
        log.exception("react confirm op=%s failed track=%s", op, track.id)
        ctx.catalog.update_pending_review(row.id, status="waiting")
        from app.errors import to_app_error

        await edit_status(ctx, job, to_app_error(exc).user_message)
        return
    ctx.catalog.update_pending_review(row.id, status="done")


async def _restart_source(ctx: Ctx, track: TrackRecord) -> Path:
    from app.relocate import stage_track_flac

    return await stage_track_flac(ctx, track)


async def _send_listen_copy(ctx: Ctx, chat_id: int, path: Path) -> None:
    from app.telegram_file import send_public_audio

    try:
        await send_public_audio(ctx, chat_id, path)
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
    prev = _loads(getattr(row, "source_report_json", None), {})
    if not isinstance(prev, dict):
        prev = {}
    ctx.catalog.update_pending_review(
        row.id,
        phase="intake",
        status="queued",
        local_path=str(source),
        telegram_file_id=track.telegram_file_id,
        replace_id=track.id,
        old_drive_id=None,
        expires_at=_expires_at(24),
        source_report_json=_dumps(
            {
                **prev,
                "drive_dest": "none",
                "dest_confirmed": True,
                "correct_telegram": True,
                "react_op": "restart",
            }
        ),
    )
    await ctx.jobs.put(
        Job(
            chat_id=row.chat_id,
            thread_id=row.thread_id,
            topic_name=row.topic_name or track.topic_name or "Unknown",
            file_id="",
            file_name=row.file_name or track.file_name or source.name,
            status_message_id=row.status_message_id,
            local_path=str(source),
            private=row.chat_id > 0,
            source_pending_id=row.id,
            fallback_send=not reuse_card,
            source_message_id=getattr(track, "source_message_id", None) or 0,
            user_id=row.user_id or 0,
            public_message_id=getattr(track, "source_message_id", None) or 0,
            drive_dest="none",
            correct_telegram=True,
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


async def _publish_group_edit(
    ctx: Ctx,
    *,
    track: TrackRecord,
    row: PendingReview,
    staged: Path,
    tags,
    editor_user_id: int,
) -> None:
    from app.captions import music_caption
    from app.telegram_file import update_public_audio

    ctx.catalog.update_track(
        track.id,
        tags_json=_dumps(asdict(tags)),
        identity_json=row.identity_json,
        title=tags.title or track.title,
        artist=tags.artist or track.artist,
        album=tags.album or track.album,
        last_editor_user_id=editor_user_id or None,
        source_report_json=row.source_report_json,
    )
    refreshed = ctx.catalog.get_track(track.id) or track
    chat_id = refreshed.source_chat_id or row.chat_id
    message_id = refreshed.source_message_id or 0
    if not message_id:
        message_id = row.status_message_id
    caption = music_caption(tags=tags, track=refreshed, last_editor_user_id=editor_user_id or None)
    new_id = await update_public_audio(
        ctx,
        chat_id=chat_id,
        message_id=message_id,
        path=staged,
        caption=caption,
        correct_media=True,
        tags=tags,
    )
    if new_id:
        ctx.catalog.update_track(track.id, telegram_file_id=new_id)
    if editor_user_id:
        ctx.catalog.increment_songs_edited(editor_user_id)


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
    if action not in {"draft", "library", "commit"}:
        ctx.catalog.update_pending_review(row.id, status="waiting")
        return
    tags = tagset_from_dict(_loads(row.working_json, {}))
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
        await _publish_group_edit(
            ctx,
            track=track,
            row=row,
            staged=staged,
            tags=tags,
            editor_user_id=row.user_id or 0,
        )
        shutil.rmtree(staged.parent, ignore_errors=True)
    except Exception as exc:
        log.exception("edit exit %s failed track=%s", action, track.id)
        ctx.catalog.update_pending_review(row.id, status="waiting")
        from app.errors import to_app_error

        await edit_status(ctx, job, to_app_error(exc).user_message)
        return
    ctx.catalog.update_pending_review(row.id, status="done")
    await show_saved_card(
        ctx,
        row,
        prefix="Group file updated. Drive copies stay until someone 👍 or 👎.",
    )


async def expire_react_exit(ctx: Ctx, row: PendingReview) -> None:
    from app.edit_ui import _clear_edit_cover, show_saved_card

    await _clear_edit_cover(ctx, row)
    _drop_stage(ctx, row)
    ctx.catalog.update_pending_review(row.id, status="cancelled")
    await show_saved_card(ctx, row, prefix="Edit timed out. Changes discarded.")
