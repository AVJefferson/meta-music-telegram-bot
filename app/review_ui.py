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

from app.genre import diff_genre_html
from app.models import Ctx, TagSet
from app.util import (
    diff_credit_html,
    format_bytes,
    format_quality,
    html_esc,
    normalize_name_list,
    same_artist_names,
)

TG_LIMIT = 4000
FIELDS: list[tuple[str, str]] = [
    ("title", "Title"),
    ("tracknumber", "Track Number"),
    ("discnumber", "Disc Number"),
    ("album", "Album"),
    ("artist", "Artist"),
    ("albumartist", "Album Artist"),
    ("composer", "Composers"),
    ("year", "Year"),
    ("genre", "Genre"),
]
FIELD_KEYS = {key for key, _ in FIELDS}
MULTI_VALUE_FIELDS = {"artist", "albumartist", "composer"}
SPARSE_FILL_FIELDS = {"composer", "genre", "year"}
_CALLBACK = re.compile(
    r"^p(\d+):(ok|rev|cancel|back|dr|dk|ds|cv(\d+)|c(\d+)|t:([a-z]+)|uf|ur|dl|dv|dn|dt|da|do|lg:([a-z]+)|mp(\d+))$"
)


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
    if rest == "dl":
        return PendingAction(pending_id, "dest_library")
    if rest == "dv":
        return PendingAction(pending_id, "dest_review")
    if rest == "dn":
        return PendingAction(pending_id, "dest_none")
    if rest == "dt":
        return PendingAction(pending_id, "dest_telegram")
    if rest == "da":
        return PendingAction(pending_id, "skip_ask")
    if rest == "do":
        return PendingAction(pending_id, "delete_original")
    if rest.startswith("lg:") and match.group(6):
        return PendingAction(pending_id, "lang", field=match.group(6))
    if rest.startswith("cv") and match.group(3) is not None:
        return PendingAction(pending_id, "cover", index=int(match.group(3)))
    if rest.startswith("c") and match.group(4) is not None:
        return PendingAction(pending_id, "cand", index=int(match.group(4)))
    if rest.startswith("t:") and match.group(5) in FIELD_KEYS:
        return PendingAction(pending_id, "toggle", field=match.group(5))
    if rest.startswith("mp") and match.group(7) is not None:
        return PendingAction(pending_id, "match_page", index=int(match.group(7)))
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


def cover_image_caption(option: dict[str, Any]) -> str:
    """Photo caption is the source name and pixel size."""
    label = str(option.get("label") or "Cover")
    width = option.get("width")
    height = option.get("height")
    if width and height:
        return f"{label} ({int(width)}x{int(height)})"[:1024]
    return label[:1024]


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
    tech: str = "",
    genre_mapper=None,
) -> str:
    del reason
    lines: list[str] = []
    lead = normalize_name_list(
        _get_field(working, "albumartist")
        or _get_field(recommended, "albumartist")
        or _get_field(original, "albumartist")
    )
    for key, label in FIELDS:
        file_val = _get_field(original, key)
        rec_val = _get_field(recommended, key)
        now_val = _get_field(working, key)
        if key == "albumartist":
            file_val = normalize_name_list(file_val)
            rec_val = normalize_name_list(rec_val)
            now_val = normalize_name_list(now_val)
        elif key in {"artist", "composer"}:
            file_val = normalize_name_list(file_val, lead=lead)
            rec_val = normalize_name_list(rec_val, lead=lead)
            now_val = normalize_name_list(now_val, lead=lead)
        elif key == "genre" and genre_mapper is not None:
            file_val = genre_mapper.compose(file_val)
            rec_val = genre_mapper.compose(rec_val)
            now_val = genre_mapper.compose(now_val)
        if key == "composer":
            if not file_val or not rec_val:
                shown = now_val or rec_val or file_val
                if file_val and not _values_match(key, file_val, shown):
                    lines.append(
                        f"{label}: <s>{html_esc(file_val) or '—'}</s> -> {html_esc(shown) or '—'}"
                    )
                else:
                    lines.append(f"<b>{label}</b>: {html_esc(shown) or '—'}")
            else:
                lines.append(f"<b>{label}</b>: {diff_credit_html(file_val, now_val)}")
            continue
        if key == "genre":
            if not file_val or not rec_val:
                shown = now_val or rec_val or file_val
                if file_val and not _values_match(key, file_val, shown):
                    lines.append(
                        f"{label}: <s>{html_esc(file_val) or '—'}</s> -> {html_esc(shown) or '—'}"
                    )
                else:
                    shown_html = (
                        genre_mapper.format_html(shown)
                        if genre_mapper is not None
                        else html_esc(shown)
                    )
                    lines.append(f"<b>{label}</b>: {shown_html or '—'}")
            else:
                lines.append(f"<b>{label}</b>: {diff_genre_html(file_val, now_val, genre_mapper)}")
            continue
        if key in SPARSE_FILL_FIELDS and (not file_val or not rec_val):
            shown = now_val or rec_val or file_val
            if file_val and not _values_match(key, file_val, shown):
                lines.append(
                    f"{label}: <s>{html_esc(file_val) or '—'}</s> -> {html_esc(shown) or '—'}"
                )
            else:
                lines.append(f"<b>{label}</b>: {html_esc(shown) or '—'}")
        elif _values_match(key, file_val, rec_val) and _values_match(key, now_val or rec_val, rec_val):
            lines.append(f"<b>{label}</b>: {html_esc(now_val or rec_val) or '—'}")
        else:
            lines.append(
                f"{label}: <s>{html_esc(file_val) or '—'}</s> -> {html_esc(now_val) or '—'}"
            )
    if tech:
        lines.append("")
        lines.append(tech)
    return _clip("\n".join(lines))


def format_conflict(
    filename: str,
    conflicts: list[dict[str, Any]],
    *,
    bit_depth: int | None,
    sample_rate: int | None,
    new_size: int | None,
    catalog_note: bool = False,
    authenticity: str = "",
) -> str:
    newest = conflicts[0] if conflicts else {}
    existing_size = format_bytes(newest.get("size"))
    modified = str(newest.get("modified") or "")[:10] or "?"
    lines = [
        f"Drive already has: <code>{html_esc(filename)}</code>",
        f"Existing: {html_esc(existing_size)}, {html_esc(modified)}",
        f"New:      {html_esc(format_quality(bit_depth, sample_rate, new_size))}",
    ]
    if authenticity:
        lines.append(html_esc(authenticity))
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


def match_prompt_text(acoustid: str, count: int) -> str:
    link = f"https://acoustid.org/track/{acoustid}" if acoustid else ""
    head = f'AcousticId: <a href="{html_esc(link)}">{html_esc(acoustid)}</a>' if link else "AcousticId: —"
    return (
        f"{head}\n"
        f"Song Matches: {count}\n\n"
        "Unable to pinpoint the song. Please pick the correct song from the possible matches."
    )


def match_keyboard(
    pending_id: int,
    candidates: list,
    page: int,
    *,
    page_size: int = 5,
    selected: int = 0,
) -> InlineKeyboardMarkup:
    total = len(candidates)
    page = max(0, page)
    start = page * page_size
    if start >= total and total:
        page = max(0, (total - 1) // page_size)
        start = page * page_size
    rows: list[list[InlineKeyboardButton]] = []
    for offset, cand in enumerate(candidates[start : start + page_size]):
        if isinstance(cand, dict):
            album = str(cand.get("album") or "—")
            artist = str(cand.get("artist") or "—")
            title = str(cand.get("title") or "—")
        else:
            album = str(getattr(cand, "album", "") or "—")
            artist = str(getattr(cand, "artist", "") or "—")
            title = str(getattr(cand, "title", "") or "—")
        mark = "● " if start + offset == selected else ""
        label = f"{mark}{album} - {artist} - {title}"[:64]
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"p{pending_id}:c{start + offset}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"p{pending_id}:mp{page - 1}"))
    if start + page_size < total:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"p{pending_id}:mp{page + 1}"))
    controls = [
        InlineKeyboardButton(text="Confirm", callback_data=f"p{pending_id}:ok"),
        InlineKeyboardButton(text="Cancel", callback_data=f"p{pending_id}:cancel"),
        *nav,
    ]
    rows.append(controls)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cover_keyboard(
    pending_id: int,
    options: list[dict[str, Any]],
    *,
    from_review: bool = False,
    waiting: bool = False,
    selected: int | None = None,
) -> InlineKeyboardMarkup:
    chosen = 0 if selected is None else int(selected)
    controls: list[InlineKeyboardButton] = []
    if not waiting:
        controls.append(InlineKeyboardButton(text="Confirm", callback_data=f"p{pending_id}:ok"))
    if from_review:
        controls.append(InlineKeyboardButton(text="Back", callback_data=f"p{pending_id}:back"))
    controls.append(InlineKeyboardButton(text="Cancel", callback_data=f"p{pending_id}:cancel"))
    rows: list[list[InlineKeyboardButton]] = [controls]
    if not waiting:
        for index, option in enumerate(options):
            label = str(option.get("label") or f"{index + 1}")
            width = option.get("width")
            height = option.get("height")
            size = f" ({int(width)}x{int(height)})" if width and height else ""
            mark = "● " if index == chosen else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{mark}{label}{size}"[:64],
                        callback_data=f"p{pending_id}:cv{index}",
                    )
                ]
            )
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
            f"<i>Album cover</i>\n"
            f"{html_esc(albumartist)} — {html_esc(album)}\n"
            "Waiting until the other album cover pick in this chat is done."
        )
    if waiting:
        return _clip(
            f"<i>Album cover</i>\n"
            f"{html_esc(albumartist)} — {html_esc(album)}\n"
            "Using the cover from the first track of this album."
        )
    lines = ["<i>Pick Album Cover</i>"]
    for index, option in enumerate(options):
        lines.append(html_esc(cover_option_text(index, option)))
    if rights_warning:
        lines.append("")
        lines.append(
            "Bot cannot send photos in this group. "
            "Make it admin, or enable Photos and Files in group permissions."
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


def dest_keyboard(
    pending_id: int,
    dest: str,
    correct_telegram: bool,
    skip_ask: bool = False,
    delete_original: bool = False,
    *,
    ask_dest: bool = True,
    ask_correct: bool = True,
    ask_delete: bool = True,
    ask_skip: bool = True,
) -> InlineKeyboardMarkup:
    def mark(active: bool, label: str) -> str:
        return f"● {label}" if active else label

    rows: list[list[InlineKeyboardButton]] = []
    if ask_dest:
        rows.append(
            [InlineKeyboardButton(text=mark(dest == "library", "Library"), callback_data=f"p{pending_id}:dl")]
        )
        rows.append(
            [InlineKeyboardButton(text=mark(dest == "review", "Review"), callback_data=f"p{pending_id}:dv")]
        )
        rows.append(
            [InlineKeyboardButton(text=mark(dest == "none", "None"), callback_data=f"p{pending_id}:dn")]
        )
    if ask_correct:
        rows.append(
            [
                InlineKeyboardButton(
                    text=mark(correct_telegram, "Correct Telegram file"),
                    callback_data=f"p{pending_id}:dt",
                )
            ]
        )
    if ask_delete:
        rows.append(
            [
                InlineKeyboardButton(
                    text=mark(delete_original, "Delete original file"),
                    callback_data=f"p{pending_id}:do",
                )
            ]
        )
    if ask_skip:
        rows.append(
            [
                InlineKeyboardButton(
                    text=mark(skip_ask, "Do not ask again"),
                    callback_data=f"p{pending_id}:da",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Confirm", callback_data=f"p{pending_id}:ok"),
            InlineKeyboardButton(text="Cancel", callback_data=f"p{pending_id}:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dest_prompt_text(
    dest: str,
    correct_telegram: bool,
    skip_ask: bool = False,
    delete_original: bool = False,
    *,
    show_skip: bool = True,
) -> str:
    lines = [
        "<b>Save options</b>",
        f"Drive: <code>{html_esc(dest)}</code>",
        f"Correct Telegram file: {'on' if correct_telegram else 'off (first save)'}",
        f"Delete original file: {'on' if delete_original else 'off'}",
    ]
    if show_skip:
        lines.append(f"Do not ask again: {'on' if skip_ask else 'off'}")
    lines.append("Confirm writes local + Drive per the toggles.")
    return "\n".join(lines)


def language_keyboard(pending_id: int, languages: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for lang in languages:
        slug = lang.casefold()
        if not slug.isalpha():
            continue
        rows.append([InlineKeyboardButton(text=lang, callback_data=f"p{pending_id}:lg:{slug}")])
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=f"p{pending_id}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_prompt_text(languages: list[str]) -> str:
    listed = " | ".join(html_esc(item) for item in languages) or "—"
    return (
        "<i>Pick the primary language</i>\n"
        f"{listed}\n"
        "The one you tap stays first in the genre list."
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
