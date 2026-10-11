from __future__ import annotations

from app.enrich import lyrics_card_text
from app.models import TagSet, TrackRecord
from app.util import html_esc, year_from_date


def music_caption(
    *,
    tags: TagSet | None = None,
    track: TrackRecord | None = None,
    relative_path: str = "",
    extra: str = "",
    last_editor_user_id: int | None = None,
) -> str:
    title = (tags.title if tags else None) or (track.title if track else "") or "Untitled"
    artist = (tags.artist if tags else None) or (track.artist if track else "") or ""
    album = (tags.album if tags else None) or (track.album if track else "") or ""
    genre = (tags.genre if tags else "") or ""
    # Path and editor id stay off public captions. Callers may still pass them.
    del relative_path, last_editor_user_id
    lines = [f"<b>{html_esc(title)}</b>"]
    if artist or album:
        lines.append(html_esc(" — ".join(part for part in (artist, album) if part)))
    if genre:
        lines.append(html_esc(genre))
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _clock(duration: float | None) -> str:
    if not duration or duration <= 0:
        return "—"
    total = round(duration)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _size_mb(size: int | None) -> str:
    if not size or size <= 0:
        return "—"
    return f"{size / 1_000_000:.2f}MB"


def tech_line(metrics: object | None, *, size_bytes: int | None = None) -> str:
    fmt = "FLAC"
    duration = None
    bitrate = None
    rate = None
    depth = None
    channels = None
    size = size_bytes
    if metrics is not None:
        fmt = str(getattr(metrics, "format_name", None) or "FLAC")
        duration = getattr(metrics, "duration", None)
        bitrate = getattr(metrics, "bitrate_kbps", None)
        rate = getattr(metrics, "sample_rate", None)
        depth = getattr(metrics, "bit_depth", None)
        channels = getattr(metrics, "channels", None)
        if size is None:
            size = getattr(metrics, "size_bytes", None)
    rate_text = f"{rate / 1000:g}KHz" if rate else "—"
    depth_text = f"{int(depth)}bit" if depth else "—"
    bit_text = f"{int(bitrate)}kbps" if bitrate else "—"
    chan_text = f"{int(channels)}CH" if channels else "—"
    return " | ".join(
        [fmt, _clock(duration), _size_mb(size), bit_text, rate_text, depth_text, chan_text]
    )


def song_card(
    tags: TagSet | None = None,
    metrics: object | None = None,
    *,
    track: TrackRecord | None = None,
    size_bytes: int | None = None,
    messages: str = "",
) -> str:
    title = (tags.title if tags else None) or (track.title if track else "") or "Untitled"
    album = (tags.album if tags else None) or (track.album if track else "") or ""
    artist = (tags.artist if tags else None) or (track.artist if track else "") or ""
    albumartist = (tags.albumartist if tags else "") or ""
    composer = (tags.composer if tags else "") or ""
    year = year_from_date(tags.date if tags else "")
    genre = (tags.genre if tags else "") or ""
    lyrics = lyrics_card_text(tags.lyrics if tags else "")
    if lyrics == "Lyrics: none":
        lyrics = "Lyrics:\n—"
    lines = [
        f"<i>{html_esc(title)}</i>",
        f"Album: {html_esc(album)}",
        f"Artist: {html_esc(artist)}",
        f"Album Artist: {html_esc(albumartist)}",
        f"Composers: {html_esc(composer)}",
        f"Year: {html_esc(year)}",
        f"Genre: {html_esc(genre)}",
        lyrics,
        tech_line(metrics, size_bytes=size_bytes),
    ]
    note = (messages or "").strip()
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)
