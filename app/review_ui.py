from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.models import Candidate, Ctx, TagSet
from app.util import format_bytes, format_quality, html_esc

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
_CALLBACK = re.compile(
    r"^p(\d+):(ok|rev|back|dr|dk|ds|c(\d+)|e:([a-z]+)|uf:([a-z]+)|ur:([a-z]+))$"
)


class ReviewStates(StatesGroup):
    waiting_custom = State()


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
    if rest == "back":
        return PendingAction(pending_id, "back")
    if rest == "dr":
        return PendingAction(pending_id, "drive_replace")
    if rest == "dk":
        return PendingAction(pending_id, "drive_keep")
    if rest == "ds":
        return PendingAction(pending_id, "drive_skip")
    if rest.startswith("c") and match.group(3) is not None:
        return PendingAction(pending_id, "cand", index=int(match.group(3)))
    if rest.startswith("e:") and match.group(4) in FIELD_KEYS:
        return PendingAction(pending_id, "edit", field=match.group(4))
    if rest.startswith("uf:") and match.group(5) in FIELD_KEYS:
        return PendingAction(pending_id, "use_file", field=match.group(5))
    if rest.startswith("ur:") and match.group(6) in FIELD_KEYS:
        return PendingAction(pending_id, "use_rec", field=match.group(6))
    return None


def _get_field(data: dict[str, Any] | TagSet, key: str) -> str:
    raw = asdict(data) if isinstance(data, TagSet) else data
    if key == "year":
        return str(raw.get("date") or raw.get("year") or "")
    return str(raw.get(key) or "")


def set_field(data: dict[str, Any], key: str, value: str) -> dict[str, Any]:
    out = dict(data)
    if key == "year":
        out["date"] = value
    else:
        out[key] = value
    return out


def _clip(text: str) -> str:
    if len(text) <= TG_LIMIT:
        return text
    return text[: TG_LIMIT - 20] + "\n…"


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
        if file_val == rec_val:
            line = f"<b>{label}</b>: {html_esc(file_val) or '—'}"
            if now_val != rec_val:
                line += f"\n  now: {html_esc(now_val) or '—'}"
            lines.append(line)
        else:
            block = (
                f"<b>{label}</b>\n"
                f"  file: {html_esc(file_val) or '—'}\n"
                f"  rec:  {html_esc(rec_val) or '—'}"
            )
            if now_val not in {file_val, rec_val}:
                block += f"\n  now:  {html_esc(now_val) or '—'}"
            lines.append(block)
    return _clip("\n".join(lines))


def format_field_prompt(
    field: str,
    original: dict[str, Any] | TagSet,
    recommended: dict[str, Any] | TagSet,
) -> str:
    label = next((name for key, name in FIELDS if key == field), field)
    file_val = _get_field(original, field) or "(none)"
    rec_val = _get_field(recommended, field) or "(none)"
    return _clip(
        f"<b>Edit {html_esc(label)}</b>\n\n"
        f"Original in file: {html_esc(file_val)}\n"
        f"Recommended: {html_esc(rec_val)}\n\n"
        "Tap a button, or send a custom value as the next message."
    )


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


def review_keyboard(pending_id: int, candidates: list[Candidate] | list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Confirm", callback_data=f"p{pending_id}:ok")]
    ]
    for i, cand in enumerate(candidates[:5]):
        if isinstance(cand, Candidate):
            title, artist = cand.title, cand.artist
        else:
            title = str(cand.get("title") or "")
            artist = str(cand.get("artist") or "")
        label = f"{title} — {artist}".strip(" —")[:64] or f"Candidate {i + 1}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"p{pending_id}:c{i}")])
    field_buttons = [
        InlineKeyboardButton(text=f"Edit {label.lower()}", callback_data=f"p{pending_id}:e:{key}")
        for key, label in FIELDS
    ]
    for i in range(0, len(field_buttons), 2):
        rows.append(field_buttons[i : i + 2])
    rows.append([InlineKeyboardButton(text="Send to review", callback_data=f"p{pending_id}:rev")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def field_keyboard(pending_id: int, field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Use file", callback_data=f"p{pending_id}:uf:{field}"),
                InlineKeyboardButton(text="Use recommended", callback_data=f"p{pending_id}:ur:{field}"),
            ],
            [InlineKeyboardButton(text="Back", callback_data=f"p{pending_id}:back")],
        ]
    )


def conflict_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Replace", callback_data=f"p{pending_id}:dr"),
                InlineKeyboardButton(text="Keep both", callback_data=f"p{pending_id}:dk"),
            ],
            [InlineKeyboardButton(text="Skip", callback_data=f"p{pending_id}:ds")],
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

    @router.message(ReviewStates.waiting_custom, F.text)
    async def on_custom_tag(message: Message, ctx: Ctx, state: FSMContext) -> None:
        from app.queue import handle_custom_tag

        if message.text and message.text.startswith("/"):
            return
        await handle_custom_tag(message, ctx, state)

    return router
