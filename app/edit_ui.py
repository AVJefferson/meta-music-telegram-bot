from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.models import Ctx, PendingReview, identity_from_dict, tagset_from_dict
from app.util import clip_html, diff_credit_html, format_audio_block, html_esc, split_artist_field

log = logging.getLogger(__name__)

EDITOR_FIELDS = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("albumartist", "Album artist"),
    ("composer", "Composer"),
    ("genre", "Genre"),
    ("date", "Date / year"),
    ("tracknumber", "Track number"),
    ("discnumber", "Disc number"),
    ("lyrics", "Lyrics"),
]
FIELD_KEYS = {key for key, _ in EDITOR_FIELDS}
FIELD_LABELS = dict(EDITOR_FIELDS)
MAX_SUGGESTIONS = 6


@dataclass
class EditAction:
    pending_id: int
    op: str
    field: str | None = None
    index: int | None = None


def _loads(value: str | None, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_edit_callback(data: str | None) -> EditAction | None:
    if not data or not data.startswith("e") or ":" not in data:
        return None
    prefix, rest = data.split(":", 1)
    if not prefix[1:].isdigit():
        return None
    pending_id = int(prefix[1:])
    if rest in {"fields", "done", "cancel", "keep", "clear", "back", "cover", "lyrics_net"}:
        return EditAction(pending_id, rest)
    if rest.startswith("cv:") and rest[3:].isdigit():
        return EditAction(pending_id, "cover_pick", index=int(rest[3:]))
    if rest.startswith("f:") and rest[2:] in FIELD_KEYS:
        return EditAction(pending_id, "field", field=rest[2:])
    if rest.startswith("s:"):
        parts = rest.split(":")
        if len(parts) == 3 and parts[1] in FIELD_KEYS and parts[2].isdigit():
            return EditAction(pending_id, "suggest", field=parts[1], index=int(parts[2]))
    return None


def _add_unique(values: list[str], raw: object) -> None:
    text = str(raw or "").strip()
    if not text:
        return
    if any(text.casefold() == existing.casefold() for existing in values):
        return
    values.append(text)


def suggestions_for(report: dict, key: str, genre=None, working=None) -> list[str]:
    file_tags = report.get("file_tags") or {}
    filename = report.get("filename") or {}
    mb = report.get("musicbrainz") or {}
    itunes = report.get("itunes") or {}
    chosen = report.get("chosen") or {}
    out: list[str] = []

    if key == "title":
        for raw in (file_tags.get("title"), filename.get("stem"), mb.get("title"), itunes.get("title"), chosen.get("title")):
            _add_unique(out, raw)
    elif key == "artist":
        for raw in (file_tags.get("artist"), mb.get("artist"), itunes.get("artist"), chosen.get("artist")):
            _add_unique(out, raw)
    elif key == "album":
        for raw in (file_tags.get("album"), mb.get("album"), itunes.get("album"), chosen.get("album")):
            _add_unique(out, raw)
    elif key == "albumartist":
        for raw in (
            file_tags.get("albumartist"),
            mb.get("albumartist"),
            chosen.get("albumartist"),
            getattr(working, "albumartist", None),
        ):
            _add_unique(out, raw)
        for raw in (
            getattr(working, "artist", None),
            file_tags.get("artist"),
            mb.get("artist"),
            itunes.get("artist"),
            chosen.get("artist"),
        ):
            if not raw:
                continue
            _add_unique(out, raw)
            for name in split_artist_field(str(raw)):
                _add_unique(out, name)
    elif key == "composer":
        for raw in (file_tags.get("composer"), mb.get("composer"), chosen.get("composer")):
            _add_unique(out, raw)
    elif key == "date":
        for raw in (file_tags.get("year"), mb.get("year"), itunes.get("year"), chosen.get("year")):
            _add_unique(out, raw)
    elif key == "tracknumber":
        for raw in (file_tags.get("track"), mb.get("track"), chosen.get("track")):
            _add_unique(out, raw)
    elif key == "discnumber":
        for raw in (file_tags.get("disc"), mb.get("disc"), chosen.get("disc")):
            _add_unique(out, raw)
    elif key == "genre":
        raw_tags: list[str] = []
        for raw in (file_tags.get("genre"), itunes.get("genre"), chosen.get("genre")):
            if raw:
                raw_tags.append(str(raw))
        raw_tags.extend(str(x) for x in (mb.get("genre_tags") or []) if x)
        raw_tags.extend(str(x) for x in (report.get("lastfm_tags") or []) if x)
        if genre is not None:
            full = genre.classify(raw_tags)
            _add_unique(out, full)
            for token in raw_tags:
                _add_unique(out, genre.classify([token]))
        else:
            for token in raw_tags:
                _add_unique(out, token)
    elif key == "lyrics":
        for raw in (file_tags.get("lyrics"), chosen.get("lyrics")):
            _add_unique(out, raw)
    return out[:MAX_SUGGESTIONS]


def field_menu_keyboard(pending_id: int, *, done: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for key, label in EDITOR_FIELDS:
        pair.append(InlineKeyboardButton(text=label, callback_data=f"e{pending_id}:f:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="Cover", callback_data=f"e{pending_id}:cover")])
    controls: list[InlineKeyboardButton] = []
    if done:
        controls.append(InlineKeyboardButton(text="Done", callback_data=f"e{pending_id}:done"))
        controls.append(InlineKeyboardButton(text="Cancel", callback_data=f"e{pending_id}:cancel"))
    rows.append(controls if controls else [])
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def field_value_keyboard(pending_id: int, key: str, suggestions: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if key == "lyrics":
        rows.append(
            [InlineKeyboardButton(text="Pull from internet", callback_data=f"e{pending_id}:lyrics_net")]
        )
    for index, value in enumerate(suggestions):
        label = value if len(value) <= 60 else value[:59] + "…"
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"e{pending_id}:s:{key}:{index}")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Keep", callback_data=f"e{pending_id}:keep"),
            InlineKeyboardButton(text="Clear", callback_data=f"e{pending_id}:clear"),
        ]
    )
    rows.append([InlineKeyboardButton(text="Back", callback_data=f"e{pending_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cover_edit_keyboard(pending_id: int, options: list[dict] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, option in enumerate(options or []):
        label = str(option.get("label") or f"{index + 1}")
        rows.append(
            [InlineKeyboardButton(text=f"{index + 1} {label}", callback_data=f"e{pending_id}:cv:{index}")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Keep", callback_data=f"e{pending_id}:keep"),
            InlineKeyboardButton(text="Remove", callback_data=f"e{pending_id}:clear"),
        ]
    )
    rows.append([InlineKeyboardButton(text="Back", callback_data=f"e{pending_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def exit_edit_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Commit to library", callback_data=f"r{pending_id}:library")],
            [InlineKeyboardButton(text="Save draft", callback_data=f"r{pending_id}:draft")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"r{pending_id}:cancel")],
        ]
    )


def drive_confirm_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Confirm", callback_data=f"r{pending_id}:yes"),
                InlineKeyboardButton(text="Cancel", callback_data=f"r{pending_id}:no"),
            ]
        ]
    )


def is_post_save(row: PendingReview) -> bool:
    return bool(row.track_id) or row.phase.startswith("react")


def _set_edit_field(report: dict, field: str | None) -> dict:
    out = dict(report)
    if field:
        out["edit_field"] = field
    else:
        out.pop("edit_field", None)
    return out


PREVIEW_FIELDS = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("albumartist", "Album artist"),
    ("composer", "Composer"),
    ("genre", "Genre"),
    ("date", "Year"),
    ("tracknumber", "Track"),
    ("discnumber", "Disc"),
    ("lyrics", "Lyrics"),
]


def _preview_value(key: str, value: str) -> str:
    return (value or "").strip()


def format_edit_card(
    original,
    working,
    *,
    header: str,
    cover_line: str | None = None,
    hint: str = "",
    footer: str = "",
    genre_mapper=None,
    tech: str = "",
) -> str:
    from app.genre import diff_genre_html
    from app.tags import normalize_tagset

    original = normalize_tagset(original, genre_mapper)
    working = normalize_tagset(working, genre_mapper)
    lines = [header, ""]
    for key, label in PREVIEW_FIELDS:
        old_raw = getattr(original, key, "") or ""
        new_raw = getattr(working, key, "") or ""
        if key == "lyrics":
            from app.enrich import lyrics_card_text

            lines.append(html_esc(lyrics_card_text(new_raw)))
            continue
        old = _preview_value(key, old_raw)
        new = _preview_value(key, new_raw)
        changed = old_raw != new_raw
        old_html = html_esc(old) if old else "—"
        new_html = html_esc(new) if new else "—"
        if key == "composer":
            lines.append(f"{label}: {diff_credit_html(old_raw, new_raw)}")
            continue
        if key == "genre" and genre_mapper is not None:
            lines.append(f"{label}: {diff_genre_html(old_raw, new_raw, genre_mapper)}")
            continue
        if key == "title":
            if changed:
                lines.append(f"<s>{old_html}</s> → <b>{new_html}</b>")
            else:
                lines.append(f"<b>{new_html}</b>")
            continue
        if changed:
            lines.append(f"{label}: <s>{old_html}</s> → {new_html}")
        else:
            lines.append(f"{label}: {new_html}")
    if tech:
        lines.append(tech)
    if cover_line:
        lines.append(cover_line)
    if hint:
        lines.append("")
        lines.append(hint)
    if footer:
        lines.append("")
        lines.append(footer)
    text = "\n".join(lines)
    return clip_html(text)


def _cover_line(report: dict) -> str | None:
    manual = report.get("manual_cover") or {}
    mode = str(manual.get("mode") or "keep")
    if mode == "remove":
        return "Cover: <s>current</s> → none"
    if mode == "replace":
        label = html_esc(str(manual.get("label") or "new image"))
        return f"Cover: <s>current</s> → {label}"
    return None


def _card_header(row: PendingReview) -> str:
    if is_post_save(row):
        kind = "library" if row.kind == "library" else "review"
        return f"<b>{kind}</b>"
    return "<b>Edit tags</b>"


def _card_footer(row: PendingReview) -> str:
    relative = html_esc(row.relative_path or "")
    return f"<code>{relative}</code>" if relative else ""


def _card_hint(row: PendingReview, field: str | None) -> str:
    if field == "cover":
        return "Pick a cover photo, Keep / Remove, or reply to this card with an image."
    if field == "genre":
        prefix = (
            "Reply to this card with text, or tap a suggestion."
            if is_post_save(row)
            else "Type a value or tap a suggestion."
        )
        return (
            f"Editing <b>Genre</b>. {prefix}\n"
            "Start with + , or | to add tags. Start with - to remove."
        )
    if field == "lyrics":
        from app.enrich import lyrics_preview

        report = _loads(row.source_report_json, {})
        working = tagset_from_dict(_loads(row.working_json, {}))
        hit = report.get("lyrics_fetch") or {}
        status = str(hit.get("status") or "")
        preview = str(hit.get("preview") or lyrics_preview(working.lyrics) or "")
        length = str(hit.get("duration_label") or "")
        lines = [
            "Editing <b>Lyrics</b>. Pull from internet, reply with text, or tap a suggestion."
        ]
        if status:
            lines.append(html_esc(status))
        if preview:
            lines.append(html_esc(preview))
        if length:
            lines.append(f"Length: {html_esc(length)}")
        return "\n".join(lines)
    if field:
        label = FIELD_LABELS.get(field, field)
        if is_post_save(row):
            return f"Editing <b>{html_esc(label)}</b>. Reply to this card with text, or tap a suggestion."
        return f"Editing <b>{html_esc(label)}</b>. Type a value or tap a suggestion."
    if is_post_save(row):
        return "Tap a field. Reply to this card to type. Cover: tap Cover or reply with a photo."
    return "Tap a field, then type or pick a suggestion. Done writes tags."


def _tech_for_row(row: PendingReview) -> str:
    from app.tags import AudioMetrics, read_audio_metrics

    if row.local_path:
        path = Path(row.local_path)
        if path.is_file():
            try:
                return format_audio_block(read_audio_metrics(path))
            except Exception:
                log.debug("edit card audio metrics failed", exc_info=True)
    raw = _loads(row.identity_json, {}) or {}
    report = raw.get("source_report") if isinstance(raw, dict) else None
    if not isinstance(report, dict):
        report = _loads(row.source_report_json, {}) or {}
    bitrate = report.get("bitrate") if isinstance(report, dict) else None
    try:
        bitrate_kbps = int(bitrate) if bitrate else None
    except (TypeError, ValueError):
        bitrate_kbps = None
    duration = 0.0
    if isinstance(raw, dict):
        duration = float(raw.get("duration") or 0)
    if not duration and isinstance(report, dict):
        duration = float(report.get("duration") or 0)
    return format_audio_block(
        AudioMetrics(
            duration=duration,
            bit_depth=raw.get("bit_depth") if isinstance(raw, dict) else None,
            sample_rate=raw.get("sample_rate") if isinstance(raw, dict) else None,
            bitrate_kbps=bitrate_kbps or None,
        )
    )


def edit_card_text(row: PendingReview, *, field: str | None = None, genre=None) -> str:
    original = tagset_from_dict(_loads(row.original_json, {}))
    working = tagset_from_dict(_loads(row.working_json, {}))
    report = _loads(row.source_report_json, {})
    return format_edit_card(
        original,
        working,
        header=_card_header(row),
        cover_line=_cover_line(report),
        hint=_card_hint(row, field),
        footer=_card_footer(row),
        genre_mapper=genre,
        tech=_tech_for_row(row),
    )


async def _clear_edit_cover(ctx: Ctx, row: PendingReview) -> dict:
    from app.queue import _cleanup_cover_option_files, _delete_cover_gallery

    report = _loads(row.source_report_json, {})
    picker = report.get("edit_cover") or {}
    media_ids = list(picker.get("media_message_ids") or [])
    if media_ids:
        await _delete_cover_gallery(ctx, row.chat_id, media_ids)
    if row.local_path and picker.get("options"):
        _cleanup_cover_option_files(Path(row.local_path), picker.get("options") or [])
    report.pop("edit_cover", None)
    ctx.catalog.update_pending_review(row.id, source_report_json=_dumps(report))
    return report


async def show_saved_card(ctx: Ctx, row: PendingReview, *, prefix: str = "", fallback_send: bool = True) -> None:
    from app.queue import _job_from_pending, edit_status
    from app.review_cmd import format_song_card

    track = ctx.catalog.get_track(row.track_id) if row.track_id else None
    if track is None:
        await edit_status(ctx, _job_from_pending(row), prefix or "Done.", fallback_send=fallback_send)
        return
    text = format_song_card(track)
    if prefix:
        text = f"{prefix}\n\n{text}"
    await edit_status(ctx, _job_from_pending(row), text, fallback_send=fallback_send)


async def show_field_menu(ctx: Ctx, row: PendingReview) -> None:
    from app.queue import _job_from_pending, edit_status

    report = await _clear_edit_cover(ctx, row)
    report = _set_edit_field(report, None)
    post = is_post_save(row)
    phase = "react_edit" if post else "edit:fields"
    ctx.catalog.update_pending_review(
        row.id,
        phase=phase,
        source_report_json=_dumps(report),
        status="waiting",
    )
    refreshed = ctx.catalog.get_pending_review(row.id) or row
    status_id = await edit_status(
        ctx,
        _job_from_pending(refreshed),
        edit_card_text(refreshed, genre=ctx.genre),
        field_menu_keyboard(row.id, done=not post),
    )
    if status_id != row.status_message_id:
        ctx.catalog.update_pending_review(row.id, status_message_id=status_id)
    if status_id and refreshed.track_id:
        ctx.catalog.bind_track_message(refreshed.track_id, refreshed.chat_id, status_id)


async def show_field_prompt(ctx: Ctx, row: PendingReview, key: str) -> None:
    from app.queue import _job_from_pending, edit_status

    report = _loads(row.source_report_json, {})
    working = tagset_from_dict(_loads(row.working_json, {}))
    suggestions = suggestions_for(report, key, ctx.genre, working)
    report = _set_edit_field(report, key)
    report["edit_suggestions"] = suggestions
    phase = "react_edit" if is_post_save(row) else "edit:fields"
    ctx.catalog.update_pending_review(
        row.id,
        phase=phase,
        source_report_json=_dumps(report),
        status="waiting",
    )
    refreshed = ctx.catalog.get_pending_review(row.id) or row
    await edit_status(
        ctx,
        _job_from_pending(refreshed),
        edit_card_text(refreshed, field=key, genre=ctx.genre),
        field_value_keyboard(row.id, key, suggestions),
    )


async def show_cover_prompt(ctx: Ctx, row: PendingReview) -> None:
    from app.covers import list_edit_cover_candidates
    from app.queue import (
        _job_from_pending,
        _send_cover_gallery,
        _write_cover_option_files,
        edit_status,
    )
    from app.tags import read_cover

    report = await _clear_edit_cover(ctx, row)
    report = _set_edit_field(report, "cover")
    phase = "edit:cover" if not is_post_save(row) else "react_edit"
    ctx.catalog.update_pending_review(
        row.id,
        phase=phase,
        source_report_json=_dumps(report),
        status="waiting",
    )
    refreshed = ctx.catalog.get_pending_review(row.id) or row
    job = _job_from_pending(refreshed)
    await edit_status(
        ctx,
        job,
        edit_card_text(refreshed, field="cover", genre=ctx.genre),
        cover_edit_keyboard(row.id),
    )

    file_cover = None
    if row.local_path:
        try:
            data, mime = await asyncio.to_thread(read_cover, Path(row.local_path))
            if data:
                file_cover = (data, mime or "image/jpeg")
        except Exception:
            log.debug("read embedded cover failed", exc_info=True)

    tags = tagset_from_dict(_loads(row.working_json, {}))
    identity = identity_from_dict(_loads(row.identity_json, {}))
    try:
        options = await list_edit_cover_candidates(
            ctx, identity, tags, row.topic_name or "General", file_cover
        )
    except Exception:
        log.exception("edit cover fetch failed id=%s", row.id)
        options = []

    option_meta: list[dict] = []
    media_ids: list[int] = []
    if options and row.local_path:
        option_meta = _write_cover_option_files(options, Path(row.local_path).parent)
        media_ids = await _send_cover_gallery(
            ctx,
            job,
            Path(row.local_path).parent,
            option_meta,
            tags.album,
            tags.albumartist or tags.artist,
            reply_to_message_id=row.status_message_id or None,
        )
    report = _loads((ctx.catalog.get_pending_review(row.id) or row).source_report_json, {})
    report = _set_edit_field(report, "cover")
    report["edit_cover"] = {"options": option_meta, "media_message_ids": media_ids}
    ctx.catalog.update_pending_review(row.id, source_report_json=_dumps(report), status="waiting")
    latest = ctx.catalog.get_pending_review(row.id) or row
    await edit_status(
        ctx,
        _job_from_pending(latest),
        edit_card_text(latest, field="cover", genre=ctx.genre),
        cover_edit_keyboard(row.id, option_meta),
    )


async def _pull_lyrics(ctx: Ctx, row: PendingReview) -> None:
    from dataclasses import replace

    from app.enrich import fetch_lrclib, lyrics_preview
    from app.queue import _job_from_pending, edit_status
    from app.tags import audio_info
    from app.util import format_clock

    report = _set_edit_field(_loads(row.source_report_json, {}), "lyrics")
    report["lyrics_fetch"] = {"status": "Fetching lyrics…"}
    ctx.catalog.update_pending_review(row.id, source_report_json=_dumps(report))
    refreshed = ctx.catalog.get_pending_review(row.id) or row
    working = tagset_from_dict(_loads(refreshed.working_json, {}))
    await edit_status(
        ctx,
        _job_from_pending(refreshed),
        edit_card_text(refreshed, field="lyrics", genre=ctx.genre),
        field_value_keyboard(row.id, "lyrics", suggestions_for(report, "lyrics", ctx.genre, working)),
    )

    tags = working
    raw_ident = _loads(row.identity_json, {}) or {}
    if "confidence" not in raw_ident:
        raw_ident["confidence"] = "low"
    ident = identity_from_dict(raw_ident)
    artists = split_artist_field(tags.artist) or list(ident.artists)
    duration = ident.duration or 0.0
    if not duration and row.local_path:
        try:
            duration, _, _ = await asyncio.to_thread(audio_info, Path(row.local_path))
        except Exception:
            duration = 0.0
    ident = replace(
        ident,
        title=tags.title or ident.title,
        artists=artists,
        album=tags.album or ident.album,
        duration=duration or ident.duration,
    )
    if not ident.title or not ident.artists:
        report = _set_edit_field(_loads((ctx.catalog.get_pending_review(row.id) or row).source_report_json, {}), "lyrics")
        report["lyrics_fetch"] = {"status": "Need title and artist first."}
        ctx.catalog.update_pending_review(row.id, source_report_json=_dumps(report), status="waiting")
        latest = ctx.catalog.get_pending_review(row.id) or row
        latest_working = tagset_from_dict(_loads(latest.working_json, {}))
        suggestions = suggestions_for(
            _loads(latest.source_report_json, {}), "lyrics", ctx.genre, latest_working
        )
        await edit_status(
            ctx,
            _job_from_pending(latest),
            edit_card_text(latest, field="lyrics", genre=ctx.genre),
            field_value_keyboard(row.id, "lyrics", suggestions),
        )
        return
    hit = None
    try:
        hit = await fetch_lrclib(ctx.http, ident)
    except Exception:
        log.exception("lyrics fetch failed id=%s", row.id)

    report = _set_edit_field(_loads((ctx.catalog.get_pending_review(row.id) or row).source_report_json, {}), "lyrics")
    working_json = (ctx.catalog.get_pending_review(row.id) or row).working_json
    if hit is None:
        report["lyrics_fetch"] = {"status": "No lyrics found."}
    elif hit.instrumental:
        report["lyrics_fetch"] = {
            "status": "Track is instrumental.",
            "duration_label": format_clock(hit.duration),
        }
    else:
        report["lyrics_fetch"] = {
            "status": "",
            "preview": lyrics_preview(hit.lyrics),
            "duration_label": format_clock(hit.duration),
        }
        working_json = set_working_field(row, "lyrics", hit.lyrics, ctx.genre)
    ctx.catalog.update_pending_review(
        row.id,
        working_json=working_json,
        source_report_json=_dumps(report),
        status="waiting",
    )
    latest = ctx.catalog.get_pending_review(row.id) or row
    latest_working = tagset_from_dict(_loads(latest.working_json, {}))
    suggestions = suggestions_for(_loads(latest.source_report_json, {}), "lyrics", ctx.genre, latest_working)
    await edit_status(
        ctx,
        _job_from_pending(latest),
        edit_card_text(latest, field="lyrics", genre=ctx.genre),
        field_value_keyboard(row.id, "lyrics", suggestions),
    )


def set_working_field(row: PendingReview, key: str, value: str, genre=None) -> str:
    from app.tags import normalize_tagset

    tags = replace(tagset_from_dict(_loads(row.working_json, {})), **{key: value})
    return _dumps(asdict(normalize_tagset(tags, genre)))


def current_edit_field(row: PendingReview) -> str | None:
    report = _loads(row.source_report_json, {})
    field = report.get("edit_field")
    if field == "cover":
        return "cover"
    if field in FIELD_KEYS:
        return str(field)
    return None


def apply_suggestion(row: PendingReview, key: str, index: int, genre=None) -> str | None:
    report = _loads(row.source_report_json, {})
    stored = report.get("edit_suggestions")
    if isinstance(stored, list) and stored:
        suggestions = [str(item) for item in stored]
    else:
        working = tagset_from_dict(_loads(row.working_json, {}))
        suggestions = suggestions_for(report, key, genre, working)
    if index < 0 or index >= len(suggestions):
        return None
    return set_working_field(row, key, suggestions[index], genre)


async def _apply_cover_option(ctx: Ctx, row: PendingReview, index: int) -> None:
    report = _loads(row.source_report_json, {})
    options = list((report.get("edit_cover") or {}).get("options") or [])
    if index < 0 or index >= len(options) or not row.local_path:
        ctx.catalog.update_pending_review(row.id, status="waiting")
        await show_field_menu(ctx, row)
        return
    option = options[index]
    src = Path(row.local_path).parent / str(option.get("path") or "")
    dest = Path(row.local_path).parent / "manual-cover.jpg"
    if not src.is_file():
        ctx.catalog.update_pending_review(row.id, status="waiting")
        await show_field_menu(ctx, row)
        return
    await asyncio.to_thread(shutil.copy2, src, dest)
    report["manual_cover"] = {
        "mode": "replace",
        "path": dest.name,
        "label": str(option.get("label") or "cover"),
    }
    ctx.catalog.update_pending_review(row.id, source_report_json=_dumps(report), status="waiting")
    refreshed = ctx.catalog.get_pending_review(row.id) or row
    await show_field_menu(ctx, refreshed)


def row_is_editing(row: PendingReview) -> bool:
    if row.status != "waiting":
        return False
    if row.phase in {"react_edit", "edit:fields", "edit:cover"}:
        return True
    return row.phase.startswith("edit:")


async def handle_edit_callback(callback, ctx: Ctx) -> None:
    from app.membership import is_forum_member
    from app.queue import cancel_pending

    action = parse_edit_callback(callback.data)
    if action is None:
        await callback.answer()
        return
    row = ctx.catalog.get_pending_review(action.pending_id)
    if row is None or row.status != "waiting" or not row_is_editing(row):
        await callback.answer("Already handled.")
        return
    if callback.message and callback.message.chat.id != row.chat_id:
        await callback.answer()
        return
    if not callback.from_user or not await is_forum_member(ctx, callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    if action.op in {
        "done",
        "cancel",
        "keep",
        "clear",
        "back",
        "cover",
        "cover_pick",
        "field",
        "suggest",
        "lyrics_net",
    } and not ctx.catalog.claim_pending(row.id, "processing"):
        await callback.answer("Already handled.")
        return
    await callback.answer()
    try:
        if action.op == "cancel":
            if is_post_save(row):
                ctx.catalog.update_pending_review(row.id, status="waiting")
                await show_field_menu(ctx, row)
                return
            await cancel_pending(ctx, row)
            return
        if action.op == "done":
            if is_post_save(row):
                ctx.catalog.update_pending_review(row.id, status="waiting")
                await show_field_menu(ctx, row)
                return
            from app.private_ui import _confirm_typed_review, _run_private_claimed

            await _run_private_claimed(ctx, row, _confirm_typed_review(ctx, row))
            return
        if action.op == "back" or action.op == "fields":
            ctx.catalog.update_pending_review(row.id, status="waiting")
            refreshed = ctx.catalog.get_pending_review(row.id)
            if refreshed:
                await show_field_menu(ctx, refreshed)
            return
        if action.op == "cover":
            ctx.catalog.update_pending_review(row.id, status="waiting")
            refreshed = ctx.catalog.get_pending_review(row.id)
            if refreshed:
                await show_cover_prompt(ctx, refreshed)
            return
        if action.op == "cover_pick" and action.index is not None:
            await _apply_cover_option(ctx, row, action.index)
            return
        if action.op == "field" and action.field:
            ctx.catalog.update_pending_review(row.id, status="waiting")
            refreshed = ctx.catalog.get_pending_review(row.id)
            if refreshed:
                await show_field_prompt(ctx, refreshed, action.field)
            return
        if action.op == "lyrics_net":
            await _pull_lyrics(ctx, row)
            return
        if action.op == "suggest" and action.field is not None and action.index is not None:
            working = apply_suggestion(row, action.field, action.index, ctx.genre)
            if working is None:
                ctx.catalog.update_pending_review(row.id, status="waiting")
                return
            ctx.catalog.update_pending_review(row.id, working_json=working, status="waiting")
            refreshed = ctx.catalog.get_pending_review(row.id)
            if refreshed:
                await show_field_menu(ctx, refreshed)
            return
        if action.op == "keep":
            field = current_edit_field(row)
            if field == "cover":
                report = _loads(row.source_report_json, {})
                report["manual_cover"] = {"mode": "keep"}
                ctx.catalog.update_pending_review(
                    row.id, source_report_json=_dumps(report), status="waiting"
                )
            else:
                ctx.catalog.update_pending_review(row.id, status="waiting")
            refreshed = ctx.catalog.get_pending_review(row.id)
            if refreshed:
                await show_field_menu(ctx, refreshed)
            return
        if action.op == "clear":
            field = current_edit_field(row)
            if field == "cover":
                report = _loads(row.source_report_json, {})
                report["manual_cover"] = {"mode": "remove"}
                ctx.catalog.update_pending_review(
                    row.id, source_report_json=_dumps(report), status="waiting"
                )
            elif field:
                ctx.catalog.update_pending_review(
                    row.id, working_json=set_working_field(row, field, "", ctx.genre), status="waiting"
                )
            else:
                ctx.catalog.update_pending_review(row.id, status="waiting")
            refreshed = ctx.catalog.get_pending_review(row.id)
            if refreshed:
                await show_field_menu(ctx, refreshed)
            return
        ctx.catalog.update_pending_review(row.id, status="waiting")
    except Exception:
        log.exception("edit callback failed id=%s op=%s", row.id, action.op)
        ctx.catalog.update_pending_review(row.id, status="waiting")


async def handle_edit_text(message, ctx: Ctx) -> bool:
    from app.membership import is_forum_member

    if not message.text or message.text.startswith("/"):
        return False
    row = None
    if message.reply_to_message:
        row = ctx.catalog.get_pending_by_message(message.chat.id, message.reply_to_message.message_id)
    if row is None and message.chat.type == "private":
        row = ctx.catalog.get_waiting_for_chat(message.chat.id)
    if row is None or not row_is_editing(row):
        return False
    field = current_edit_field(row)
    if field is None:
        return False
    if not message.from_user or not await is_forum_member(ctx, message.from_user.id):
        return False
    if message.chat.type != "private" and not message.reply_to_message:
        return False
    if field == "cover":
        text = (message.text or "").strip()
        if not text.startswith(("http://", "https://")):
            return False
        if not ctx.catalog.claim_pending(row.id, "processing"):
            return True
        from app.private_ui import _fetch_image, _store_manual_cover

        try:
            data = await _fetch_image(text)
            await _store_manual_cover(ctx, row, data, normalized=True)
        except Exception as exc:
            ctx.catalog.update_pending_review(row.id, status="waiting")
            await message.reply(f"Could not load image: {html_esc(exc)}", parse_mode="HTML")
        return True
    if not ctx.catalog.claim_pending(row.id, "processing"):
        return True
    value = message.text
    if field == "genre":
        tags = tagset_from_dict(_loads(row.working_json, {}))
        value = ctx.genre.merge_typed(tags.genre, message.text)
    ctx.catalog.update_pending_review(
        row.id, working_json=set_working_field(row, field, value, ctx.genre), status="waiting"
    )
    refreshed = ctx.catalog.get_pending_review(row.id)
    if refreshed:
        await show_field_menu(ctx, refreshed)
    return True


def build_edit_router() -> Router:
    router = Router()

    @router.callback_query(F.data.regexp(r"^e\d+:"))
    async def on_edit_callback(callback: CallbackQuery, ctx: Ctx) -> None:
        await handle_edit_callback(callback, ctx)

    @router.message(F.text, ~F.text.startswith("/"))
    async def on_edit_text(message: Message, ctx: Ctx) -> None:
        handled = await handle_edit_text(message, ctx)
        if not handled:
            raise SkipHandler()

    @router.message(F.photo | F.document)
    async def on_edit_cover(message: Message, ctx: Ctx) -> None:
        from io import BytesIO

        from app.botapi import discard_download
        from app.membership import is_forum_member
        from app.private_ui import _store_manual_cover

        if message.chat.type == "private":
            raise SkipHandler()
        row = None
        if message.reply_to_message:
            row = ctx.catalog.get_pending_by_message(message.chat.id, message.reply_to_message.message_id)
        if row is None or not row_is_editing(row):
            raise SkipHandler()
        if not message.from_user or not await is_forum_member(ctx, message.from_user.id):
            raise SkipHandler()
        media = message.photo[-1] if message.photo else message.document
        if not media or (message.document and not (message.document.mime_type or "").startswith("image/")):
            raise SkipHandler()
        if not ctx.catalog.claim_pending(row.id, "processing"):
            return
        try:
            file = await ctx.bot.get_file(media.file_id)
            buffer = BytesIO()
            await ctx.bot.download(file, destination=buffer)
            await asyncio.to_thread(discard_download, file.file_path)
            await _store_manual_cover(ctx, row, buffer.getvalue())
        except Exception:
            ctx.catalog.update_pending_review(row.id, status="waiting")
            await message.reply("Invalid image. Send JPEG, PNG, or WebP.")

    return router

