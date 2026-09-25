from __future__ import annotations

from app.models import TagSet, TrackRecord
from app.util import html_esc


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
