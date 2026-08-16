from __future__ import annotations

import html
import re
from pathlib import Path

_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LRC = re.compile(r"\[\d{1,2}:\d{2}")
_UA = re.compile(r"^([^/]+)/(\S+)\s*\(([^)]+)\)")
# Slashes only split when spaced, so band names like AC/DC survive.
_ARTIST_SEPARATORS = re.compile(
    r"\s*;\s*|\s+/\s+|\s+&\s+|\s+(?:feat|ft|featuring)\.?\s+",
    re.IGNORECASE,
)


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


def safe_link(value: object) -> str:
    """Escape a URL for use inside an href attribute, or return "" if unusable.

    Cover art URLs come from iTunes and Cover Art Archive, so quotes must be
    escaped as well to keep them inside the attribute.
    """
    url = str(value or "").strip()
    if not url or "\n" in url or "\r" in url:
        return ""
    if not url.startswith(("http://", "https://")):
        return ""
    return html.escape(url, quote=True)


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
    out: list[str] = []
    for chunk in value.split(","):
        for name in _ARTIST_SEPARATORS.split(chunk):
            name = name.strip()
            if name:
                out.append(name)
    return out


def artist_name_set(value: str) -> frozenset[str]:
    names = {normalize_match_text(name) for name in split_artist_field(value)}
    return frozenset(name for name in names if name)


def same_artist_names(left: str, right: str) -> bool:
    return artist_name_set(left) == artist_name_set(right)


def file_stem_hints(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^\d+\s*[-._]\s*", "", stem)
    return stem.strip()


def format_clock(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = round(float(seconds))
    if total < 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_tech_lines(
    duration: float | None = None,
    bit_depth: int | None = None,
    sample_rate: int | None = None,
    bitrate_kbps: int | None = None,
) -> str:
    lines = ["Format: FLAC"]
    clock = format_clock(duration) if duration else ""
    if clock:
        lines.append(f"Duration: {clock}")
    parts: list[str] = []
    if bitrate_kbps:
        parts.append(f"{bitrate_kbps} kbps")
    if sample_rate:
        parts.append(f"{sample_rate / 1000:g} kHz")
    if bit_depth:
        parts.append(f"{bit_depth} bits")
    if parts:
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def format_audio_block(metrics: object | None = None) -> str:
    if metrics is None:
        return format_tech_lines()
    duration = getattr(metrics, "duration", None) or None
    return format_tech_lines(
        duration=duration,
        bit_depth=getattr(metrics, "bit_depth", None),
        sample_rate=getattr(metrics, "sample_rate", None),
        bitrate_kbps=getattr(metrics, "bitrate_kbps", None),
    )


def format_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f} MB"
    if n >= 1000:
        return f"{n / 1000:.0f} KB"
    return f"{n} B"


def format_quality(bit_depth: int | None, sample_rate: int | None, size: int | None = None) -> str:
    depth = f"{bit_depth}-bit" if bit_depth else "?-bit"
    if sample_rate:
        rate = f"{sample_rate / 1000:g}kHz"
    else:
        rate = "?kHz"
    parts = [depth, rate]
    if size is not None:
        parts.append(format_bytes(size))
    return ", ".join(parts)


def normalize_match_text(value: str) -> str:
    text = (value or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()
