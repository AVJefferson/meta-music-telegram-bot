from __future__ import annotations

from dataclasses import dataclass

from app.formats import (
    DEFAULT_ALLOWED,
    SavePrefs,
    normalize_allowed,
    normalize_save_dest,
    truthy_setting,
    user_settings_dict,
)
from app.util import html_esc

SAVE_ASK = "ask"


@dataclass(frozen=True)
class ProcessPrefs:
    save: str = SAVE_ASK
    auto_forward: tuple[int, ...] = ()
    correct: str = "yes"
    delete: str = "no"
    notifications: str = "all"
    channel: str = "current"
    formats: tuple[str, ...] = DEFAULT_ALLOWED


def tri_state(value: object, default: str = "no") -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value or "").strip().casefold().replace(" ", "_")
    if text in {"1", "true", "yes", "on"}:
        return "yes"
    if text in {"0", "false", "no", "off"}:
        return "no"
    if text in {"ask", "ask_each_time", "each_time"}:
        return "ask"
    return default if default in {"yes", "no", "ask"} else "no"


def resolve_save(data: dict) -> str:
    if "default_dest" not in data and "skip_save_prompt" not in data:
        return SAVE_ASK
    raw = str(data.get("default_dest") or "").strip().casefold().replace(" ", "_")
    if raw in {"ask", "ask_each_time", "each_time"}:
        return SAVE_ASK
    if "default_dest" not in data:
        return SAVE_ASK if not truthy_setting(data.get("skip_save_prompt")) else "none"
    dest = normalize_save_dest(raw, default="none")
    if "skip_save_prompt" in data and not truthy_setting(data.get("skip_save_prompt")):
        return SAVE_ASK
    return dest


def resolve_forward(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() == "none":
            return ()
        parts: list[object] = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return ()
    out: list[int] = []
    for part in parts:
        try:
            out.append(int(str(part).strip()))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def resolve_notifications(value: object) -> str:
    text = str(value or "all").strip().casefold().replace(" ", "_")
    if text in {"errors", "errors_only", "error"}:
        return "errors"
    if text in {"none", "off", "silent"}:
        return "none"
    return "all"


def resolve_channel(value: object) -> str:
    text = str(value or "current").strip().casefold().replace(" ", "_")
    if text in {"private", "private_chat", "dm"}:
        return "private"
    if text in {"reactions", "reactions_only", "reaction"}:
        return "reactions"
    return "current"


def process_prefs_from_settings(settings: object) -> ProcessPrefs:
    data = settings if isinstance(settings, dict) else {}
    correct = tri_state(data["correct_telegram"], "yes") if "correct_telegram" in data else "yes"
    delete = tri_state(data["delete_original"], "no") if "delete_original" in data else "no"
    return ProcessPrefs(
        save=resolve_save(data),
        auto_forward=resolve_forward(data.get("auto_forward")),
        correct=correct,
        delete=delete,
        notifications=resolve_notifications(data.get("notifications", "all")),
        channel=resolve_channel(data.get("notification_channel", "current")),
        formats=tuple(normalize_allowed(data.get("allowed_formats"))),
    )


def process_prefs_from_user(user: object | None) -> ProcessPrefs:
    return process_prefs_from_settings(user_settings_dict(user))


def load_user_prefs(ctx: object, user_id: int) -> ProcessPrefs:
    catalog = getattr(ctx, "catalog", None)
    user = None
    if catalog is not None and user_id:
        getter = getattr(catalog, "get_user", None)
        if callable(getter):
            user = getter(user_id)
    return process_prefs_from_user(user)


def as_save_prefs(prefs: ProcessPrefs) -> SavePrefs:
    asking = prefs.save == SAVE_ASK
    return SavePrefs(
        dest="none" if asking else prefs.save,
        correct_telegram=prefs.correct == "yes",
        skip_save_prompt=not asking,
        delete_original=prefs.delete == "yes",
    )


def asks_questions(prefs: ProcessPrefs, *, logged_in: bool) -> bool:
    if not logged_in:
        return False
    return prefs.save == SAVE_ASK or prefs.correct == "ask" or prefs.delete == "ask"


def save_label(value: str) -> str:
    return {
        "library": "Library",
        "review": "Review",
        "none": "None",
        "ask": "Ask each time",
    }.get(value, "Ask each time")


def tri_label(value: str) -> str:
    return {"yes": "Yes", "no": "No", "ask": "Ask each time"}.get(value, "No")


def notify_label(value: str) -> str:
    return {"all": "All", "errors": "Errors Only", "none": "None"}.get(value, "All")


def channel_label(value: str) -> str:
    return {
        "current": "Current Chat",
        "private": "Private Chat",
        "reactions": "Reactions Only",
    }.get(value, "Current Chat")


def forward_label(ids: tuple[int, ...]) -> str:
    if not ids:
        return "None"
    return ",".join(str(item) for item in ids)


def format_settings_text(*, email: str | None, logged_in: bool, prefs: ProcessPrefs) -> str:
    login = "<code>/login</code> to upload files to your drive."
    if email and logged_in:
        drive = html_esc(email)
    elif email:
        drive = f"{html_esc(email)} | {login}"
    else:
        drive = login
    formats = ", ".join(item.upper() for item in prefs.formats) or "FLAC, MP3, M4A, WAV"
    return "\n".join(
        [
            f"Drive: {drive}",
            f"Default Save: {save_label(prefs.save)}",
            f"Auto Forward: {forward_label(prefs.auto_forward)}",
            f"Correct telegram file: {tri_label(prefs.correct)}",
            f"Delete Original File: {tri_label(prefs.delete)}",
            f"Notifications: {notify_label(prefs.notifications)}",
            f"Notification Channel: {channel_label(prefs.channel)}",
            f"Music Files: {formats}",
        ]
    )
