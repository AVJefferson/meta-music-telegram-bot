from __future__ import annotations

import json
from pathlib import Path

ALL_FORMATS = ("flac", "mp3", "m4a", "ogg", "opus", "wav")
ALL_FORMAT_SET = frozenset(ALL_FORMATS)
DEFAULT_ALLOWED = ("flac", "mp3")
DEFAULT_ALLOWED_SET = frozenset(DEFAULT_ALLOWED)

_LABELS = {
    "flac": "FLAC",
    "mp3": "MP3",
    "m4a": "M4A",
    "ogg": "OGG",
    "opus": "Opus",
    "wav": "WAV",
}
_EXTENSIONS = {
    "flac": ".flac",
    "mp3": ".mp3",
    "m4a": ".m4a",
    "ogg": ".ogg",
    "opus": ".opus",
    "wav": ".wav",
}
_EXT_TO_FORMAT = {ext: fmt for fmt, ext in _EXTENSIONS.items()}
_DRIVE_MIME = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "wav": "audio/wav",
}
_ALIASES = {
    "mpeg": "mp3",
    "mpga": "mp3",
    "m4a": "m4a",
    "mp4": "m4a",
    "aac": "m4a",
    "x-m4a": "m4a",
    "vorbis": "ogg",
    "x-flac": "flac",
    "x-wav": "wav",
    "wave": "wav",
    "vnd.wave": "wav",
}


def extension_for(format_id: str) -> str:
    return _EXTENSIONS.get(format_id, ".flac")


def format_label(format_id: str | Path | None) -> str:
    if isinstance(format_id, Path):
        fmt = format_from_path(format_id)
    else:
        key = str(format_id or "").strip().casefold()
        if key.startswith("."):
            fmt = _EXT_TO_FORMAT.get(key)
        elif "/" in key or "\\" in key:
            fmt = format_from_path(key)
        else:
            fmt = key if key in ALL_FORMAT_SET else format_from_path(key)
    return _LABELS.get(fmt or "", "FLAC")


def format_from_path(path: str | Path) -> str | None:
    return detect_format(str(path), "")


def extension_from_path(path: str | Path | None) -> str:
    if not path:
        return ".flac"
    fmt = format_from_path(path)
    return extension_for(fmt) if fmt else ".flac"


def suffix_for(*names: str | Path | None) -> str:
    for name in names:
        if not name:
            continue
        fmt = detect_format(str(name), "")
        if fmt:
            return extension_for(fmt)
    return ".flac"


def normalize_ext(ext: str | None) -> str:
    text = str(ext or "").strip()
    if not text:
        return ".flac"
    if not text.startswith("."):
        text = f".{text}"
    fmt = detect_format(f"file{text}", "")
    return extension_for(fmt) if fmt else ".flac"


def mime_for_path(path: str | Path) -> str:
    fmt = format_from_path(path)
    return _DRIVE_MIME.get(fmt or "", "audio/flac")


def _from_ext(name: str) -> str | None:
    suffix = Path(name or "").suffix.casefold()
    if suffix in _EXT_TO_FORMAT:
        return _EXT_TO_FORMAT[suffix]
    return None


def _from_mime(mime: str) -> str | None:
    text = (mime or "").casefold()
    if not text:
        return None
    subtype = text.split("/", 1)[-1]
    subtype = subtype.split(";", 1)[0].strip()
    if subtype.startswith("x-"):
        subtype = subtype[2:]
    if subtype in ALL_FORMAT_SET:
        return subtype
    if subtype in _ALIASES:
        return _ALIASES[subtype]
    if "flac" in text:
        return "flac"
    if "opus" in text:
        return "opus"
    if "vorbis" in text or text.endswith("/ogg") or "ogg" in subtype:
        return "ogg"
    if "mpeg" in text or "mp3" in text:
        return "mp3"
    if "mp4" in text or "m4a" in text or "aac" in text:
        return "m4a"
    if "wav" in text or "wave" in text:
        return "wav"
    return None


def detect_format(file_name: str, mime: str = "") -> str | None:
    return _from_ext(file_name) or _from_mime(mime)


def is_allowed_audio(file_name: str, mime: str, allowed: object) -> bool:
    fmt = detect_format(file_name, mime)
    if fmt is None:
        return False
    return fmt in set(normalize_allowed(allowed))


def normalize_allowed(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return list(DEFAULT_ALLOWED)
    seen: set[str] = set()
    for item in raw:
        key = str(item or "").strip().casefold()
        if key.startswith("."):
            key = key[1:]
        key = _ALIASES.get(key, key)
        if key in ALL_FORMAT_SET:
            seen.add(key)
    ordered = [fmt for fmt in ALL_FORMATS if fmt in seen]
    return ordered if ordered else list(DEFAULT_ALLOWED)


def user_settings_dict(user: object | None) -> dict:
    raw = getattr(user, "settings_json", None) if user is not None else None
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clamp_suggest_similarity(value: object, default: float = 0.5) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = default
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def suggest_allow_dissimilar(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def user_allowed_formats(ctx: object, user_id: int) -> list[str]:
    catalog = getattr(ctx, "catalog", None) if ctx is not None else None
    user = None
    if catalog is not None and user_id:
        getter = getattr(catalog, "get_user", None)
        if callable(getter):
            user = getter(user_id)
    return normalize_allowed(user_settings_dict(user).get("allowed_formats"))


def message_name_mime(message: object) -> tuple[str, str]:
    for attr in ("document", "audio"):
        media = getattr(message, attr, None)
        if media:
            return (
                str(getattr(media, "file_name", None) or ""),
                str(getattr(media, "mime_type", None) or ""),
            )
    return "", ""


def is_known_audio_message(message: object) -> bool:
    return detect_format(*message_name_mime(message)) is not None


def is_allowed_audio_message(message: object, allowed: object) -> bool:
    name, mime = message_name_mime(message)
    return is_allowed_audio(name, mime, allowed)


def default_filename(mime: str = "", *, fallback: str = "track") -> str:
    fmt = detect_format("", mime)
    return f"{fallback}{extension_for(fmt) if fmt else '.flac'}"


def stored_download_name(file_name: str, mime: str = "") -> str:
    from app.util import sanitize_filename

    fmt = detect_format(file_name, mime)
    ext = extension_for(fmt) if fmt else suffix_for(file_name)
    stem = sanitize_filename(Path(file_name or "track").stem)
    return f"{stem}{ext}"
