from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.models import TagSet
from app.tags import build_filename
from app.util import sanitize_filename


def library_relative(topic: str, tags: TagSet) -> Path:
    album_artist = sanitize_filename(tags.albumartist or tags.artist or "Unknown Artist")
    album = sanitize_filename(tags.album or "Unknown Album")
    year = tags.date or "0000"
    folder = f"{year} - {album}"
    return Path(sanitize_filename(topic or "General")) / album_artist / folder / build_filename(tags)


def review_relative(original_name: str) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe = sanitize_filename(Path(original_name).stem) + ".flac"
    return Path(day) / safe


def place_file(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(src), str(dest))
    return dest


def write_sidecar(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def rmdir_empty(root: Path) -> None:
    if not root.exists():
        return
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        if current == root:
            continue
        try:
            current.rmdir()
        except OSError:
            pass


def unlink_quiet(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
