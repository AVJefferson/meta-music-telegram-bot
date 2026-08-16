from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from app.models import Ctx, TagSet
from app.util import format_bytes, format_quality, html_esc, safe_link, same_artist_names

TG_LIMIT = 4000
FIELDS: list[tuple[str, str]] = [
    ("title", "Title"),
    ("artist", "Artist"),
    ("album", "Album"),
    ("albumartist", "Album artist"),
    ("composer", "Composer"),
    ("genre", "Genre"),
    ("year", "Year"),
]
FIELD_KEYS = {key for key, _ in FIELDS}
MULTI_VALUE_FIELDS = {"artist", "albumartist", "composer"}
SPARSE_FILL_FIELDS = {"composer", "genre", "year"}
_CALLBACK = re.compile(r"^p(\d+):(ok|rev|cancel|back|dr|dk|ds|cv(\d+)|c(\d+)|t:([a-z]+)|uf|ur)$")


@dataclass
class PendingAction:
    pending_id: int
    op: str
    index: int | None = None
    field: str | None = None


def parse_callback(data: str | None) -> PendingAction | None:
    if not data:
        return None
    match = _CALLBACK.match(data)
    if not match:
        return None
    pending_id = int(match.group(1))
    rest = match.group(2)
    if rest == "ok":
        return PendingAction(pending_id, "ok")
    if rest == "rev":
        return PendingAction(pending_id, "rev")
    if rest == "cancel":
        return PendingAction(pending_id, "cancel")
    if rest == "back":
        return PendingAction(pending_id, "back")
    if rest == "dr":
        return PendingAction(pending_id, "drive_replace")
    if rest == "dk":
        return PendingAction(pending_id, "drive_keep")
    if rest == "ds":
        return PendingAction(pending_id, "drive_skip")
    if rest == "uf":
        return PendingAction(pending_id, "use_file")
    if rest == "ur":
        return PendingAction(pending_id, "use_rec")
    if rest.startswith("cv") and match.group(3) is not None:
        return PendingAction(pending_id, "cover", index=int(match.group(3)))
    if rest.startswith("c") and match.group(4) is not None:
        return PendingAction(pending_id, "cand", index=int(match.group(4)))
    if rest.startswith("t:") and match.group(5) in FIELD_KEYS:
        return PendingAction(pending_id, "toggle", field=match.group(5))
    return None


def _get_field(data: dict[str, Any] | TagSet, key: str) -> str:
    raw = asdict(data) if isinstance(data, TagSet) else data
    if key == "year":
        return str(raw.get("date") or raw.get("year") or "")
    return str(raw.get(key) or "")


def _values_match(key: str, left: str, right: str) -> bool:
    if left == right:
        return True
    return key in MULTI_VALUE_FIELDS and same_artist_names(left, right)


def _shows_choice(key: str, file_val: str, rec_val: str) -> bool:
    if key in SPARSE_FILL_FIELDS and (not file_val or not rec_val):
        return False
    return not _values_match(key, file_val, rec_val)


def cover_option_text(index: int, option: dict[str, Any]) -> str:
    label = str(option.get("label") or "")
    width = option.get("width")
    height = option.get("height")
    size = ""
    if width and height:
        size = f" ({int(width)}x{int(height)})"
    return f"{index + 1}. {label}{size}"


def set_field(data: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    out = dict(data)
    if key == "year":
        out["date"] = value
    else:
        out[key] = value
    return out


def toggle_working_field(
    original: dict[str, Any] | TagSet,
    recommended: dict[str, Any] | TagSet,
    working: dict[str, Any],
    field: str,
) -> dict[str, Any]:
    file_val = _get_field(original, field)
    rec_val = _get_field(recommended, field)
    now_val = _get_field(working, field)
    next_val = rec_val if now_val == file_val else file_val
    return set_field(working, field, next_val)


def _clip(text: str) -> str:
    if len(text) <= TG_LIMIT:
        return text
    return text[: TG_LIMIT - 20] + "\n…"


def _choice_line(kind: str, value: str, *, selected: bool, strike: bool) -> str:
    text = html_esc(value) or "—"
    if selected:
        return f"  <b>{kind}:</b> {text}"
    if strike:
        return f"  <s>{kind}: {text}</s>"
    return f"  {kind}: {text}"


def format_summary(
    original: dict[str, Any] | TagSet,
    recommended: dict[str, Any] | TagSet,
    working: dict[str, Any] | TagSet,
    *,
    reason: str = "",
) -> str:
    lines = ["<b>Low confidence — check tags</b>"]
    if reason:
        lines.append(f"Reason: {html_esc(reason)}")
    lines.append("")
    for key, label in FIELDS:
        file_val = _get_field(original, key)
        rec_val = _get_field(recommended, key)
        now_val = _get_field(working, key)
        if key in SPARSE_FILL_FIELDS and (not file_val or not rec_val):
            shown = rec_val or file_val
            line = f"<b>{label}</b>: {html_esc(shown) or '—'}"
            if now_val != shown:
                line += f"\n  now: {html_esc(now_val) or '—'}"
            lines.append(line)
        elif _values_match(key, file_val, rec_val):
            line = f"<b>{label}</b>: {html_esc(rec_val) or '—'}"
            if file_val != rec_val:
                line += " (reordered)"
            if not _values_match(key, now_val, rec_val):
                line += f"\n  now: {html_esc(now_val) or '—'}"
            lines.append(line)
        else:
            file_sel = now_val == file_val
            rec_sel = now_val == rec_val
            block = (
                f"<b>{label}</b>\n"
                f"{_choice_line('file', file_val, selected=file_sel, strike=rec_sel)}\n"
                f"{_choice_line('rec', rec_val, selected=rec_sel, strike=file_sel)}"
            )
            if now_val not in {file_val, rec_val}:
                block += f"\n  now: {html_esc(now_val) or '—'}"
            lines.append(block)
    return _clip("\n".join(lines))


def format_conflict(
    filename: str,
    conflicts: list[dict[str, Any]],
    *,
    bit_depth: int | None,
    sample_rate: int | None,
    new_size: int | None,
    catalog_note: bool = False,
) -> str:
    newest = conflicts[0] if conflicts else {}
    existing_size = format_bytes(newest.get("size"))
    modified = str(newest.get("modified") or "")[:10] or "?"
    lines = [
        f"Drive already has: <code>{html_esc(filename)}</code>",
        f"Existing: {html_esc(existing_size)}, {html_esc(modified)}",
        f"New:      {html_esc(format_quality(bit_depth, sample_rate, new_size))}",
    ]
    if len(conflicts) > 1:
        lines.append(f"\n{len(conflicts)} files with this name — Replace updates the newest.")
    if catalog_note:
        lines.append(
            "\nNote: Drive listing may miss files this app did not create; "
            "a second copy could appear."
        )
    return _clip("\n".join(lines))


def _any_field_differs(
    original: dict[str, Any] | TagSet,
    recommended: dict[str, Any] | TagSet,
) -> bool:
    return any(
        _shows_choice(key, _get_field(original, key), _get_field(recommended, key))
        for key, _ in FIELDS
    )


def bulk_choice(
    original: dict[str, Any] | TagSet,
    recommended: dict[str, Any] | TagSet,
    *,
    use_file: bool,
) -> dict[str, Any]:
    source = original if use_file else recommended
    chosen = asdict(source) if isinstance(source, TagSet) else dict(source)
    if not use_file:
        for key in SPARSE_FILL_FIELDS:
            rec_val = _get_field(recommended, key)
            file_val = _get_field(original, key)
            if not rec_val and file_val:
                chosen = set_field(chosen, key, file_val)
        return chosen
    for key in MULTI_VALUE_FIELDS:
        file_val = _get_field(original, key)
        rec_val = _get_field(recommended, key)
        if file_val != rec_val and _values_match(key, file_val, rec_val):
            chosen = set_field(chosen, key, rec_val)
    for key in SPARSE_FILL_FIELDS:
        file_val = _get_field(original, key)
        rec_val = _get_field(recommended, key)
        if not file_val or not rec_val:
            chosen = set_field(chosen, key, rec_val or file_val)
    return chosen


def review_keyboard(
    pending_id: int,
    original: dict[str, Any] | TagSet,
    recommended: dict[str, Any] | TagSet,
    working: dict[str, Any] | TagSet,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Send to review", callback_data=f"p{pending_id}:rev")]
    ]
    if _any_field_differs(original, recommended):
        rows.append(
            [
                InlineKeyboardButton(text="Use File", callback_data=f"p{pending_id}:uf"),
                InlineKeyboardButton(text="Use recommended", callback_data=f"p{pending_id}:ur"),
            ]
        )
    toggles: list[InlineKeyboardButton] = []
    for key, label in FIELDS:
        file_val = _get_field(original, key)
        rec_val = _get_field(recommended, key)
        if not _shows_choice(key, file_val, rec_val):
            continue
        now_val = _get_field(working, key)
        pick = "file" if now_val == file_val else "rec"
        toggles.append(
            InlineKeyboardButton(text=f"{label}: {pick}", callback_data=f"p{pending_id}:t:{key}")
        )
    for i in range(0, len(toggles), 2):
        rows.append(toggles[i : i + 2])
    rows.append(
        [
            InlineKeyboardButton(text="Confirm", callback_data=f"p{pending_id}:ok"),
            InlineKeyboardButton(text="Cancel", callback_data=f"p{pending_id}:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cover_keyboard(
    pending_id: int,
    options: list[dict[str, Any]],
    *,
    from_review: bool = False,
    waiting: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not waiting:
        for index, option in enumerate(options):
            label = str(option.get("label") or f"{index + 1}")
            row = [
                InlineKeyboardButton(
                    text=f"{index + 1} {label}",
                    callback_data=f"p{pending_id}:cv{index}",
                )
            ]
            url = str(option.get("url") or "")
            if url.startswith("http://") or url.startswith("https://"):
                row.append(InlineKeyboardButton(text="view", url=url))
            rows.append(row)
    controls: list[InlineKeyboardButton] = []
    if from_review:
        controls.append(InlineKeyboardButton(text="Back", callback_data=f"p{pending_id}:back"))
    controls.append(InlineKeyboardButton(text="Cancel", callback_data=f"p{pending_id}:cancel"))
    rows.append(controls)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_cover_prompt(
    album: str,
    albumartist: str,
    options: list[dict[str, Any]],
    filename: str,
    *,
    waiting: bool = False,
    queued: bool = False,
    rights_warning: bool = False,
) -> str:
    if queued:
        return _clip(
            f"<b>Waiting for album cover</b>\n"
            f"{html_esc(albumartist)} — {html_esc(album)}\n"
            f"File: <code>{html_esc(filename)}</code>\n\n"
            "Waiting — another cover pick is in progress."
        )
    if waiting:
        return _clip(
            f"<b>Waiting for album cover</b>\n"
            f"{html_esc(albumartist)} — {html_esc(album)}\n"
            f"File: <code>{html_esc(filename)}</code>\n\n"
            "Pick the cover on the first track of this album."
        )
    lines = [
        "<b>Pick album cover</b>",
        f"{html_esc(albumartist)} — {html_esc(album)}",
        f"File: <code>{html_esc(filename)}</code>",
        "",
    ]
    for index, option in enumerate(options):
        desc = html_esc(cover_option_text(index, option))
        href = safe_link(option.get("url"))
        if href:
            lines.append(f'{desc} — <a href="{href}">preview</a>')
        else:
            lines.append(desc)
    lines.append("")
    lines.append("Later tracks of this album reuse the pick.")
    if rights_warning:
        lines.append("")
        lines.append(
            "Bot cannot send photos in this group. "
            "Make it admin, or enable Photos and Files in group permissions. "
            "Tap <b>view</b> / preview links until then."
        )
    return _clip("\n".join(lines))


def conflict_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Replace", callback_data=f"p{pending_id}:dr"),
                InlineKeyboardButton(text="Keep both", callback_data=f"p{pending_id}:dk"),
            ],
            [InlineKeyboardButton(text="Skip", callback_data=f"p{pending_id}:ds")],
            [InlineKeyboardButton(text="Cancel", callback_data=f"p{pending_id}:cancel")],
        ]
    )


def empty_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


def build_review_router() -> Router:
    router = Router()

    @router.callback_query(F.data.regexp(r"^p\d+:"))
    async def on_pending_callback(callback: CallbackQuery, ctx: Ctx, state: FSMContext) -> None:
        from app.queue import handle_pending_callback

        await handle_pending_callback(callback, ctx, state)

    return router
