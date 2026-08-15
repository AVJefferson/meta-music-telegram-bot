from __future__ import annotations

import html
import re
from pathlib import Path

_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LRC = re.compile(r"\[\d{1,2}:\d{2}")
_UA = re.compile(r"^([^/]+)/(\S+)\s*\(([^)]+)\)")


def format_artist_list(names: list[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = re.sub(r"\s+", " ", (raw or "").strip())
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} & {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} & {cleaned[-1]}"


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = _INVALID_FS.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "Unknown")[:max_len]


def is_synced_lrc(text: str | None) -> bool:
    if not text:
        return False
    return bool(_LRC.search(text))


def parse_mb_user_agent(value: str) -> tuple[str, str, str]:
    match = _UA.match((value or "").strip())
    if match:
        return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
    return "telegram-music-bot", "1.0", value.strip() or "unknown@example.com"


def html_esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def year_from_date(value: str | None) -> str:
    if not value:
        return ""
    match = re.match(r"(\d{4})", value.strip())
    return match.group(1) if match else ""


def parse_track_number(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    match = re.match(r"(\d+)", value)
    return match.group(1) if match else value[:8]


def split_artist_field(value: str) -> list[str]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if not parts:
        return []
    if " & " in parts[-1]:
        last = [p.strip() for p in parts[-1].split(" & ") if p.strip()]
        return parts[:-1] + last
    return parts


def file_stem_hints(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^\d+\s*[-._]\s*", "", stem)
    return stem.strip()
