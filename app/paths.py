from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.errors import AppError
from app.formats import suffix_for
from app.models import Ctx

_SHA = re.compile(r"^[0-9a-f]{64}$")


def user_kind_root(ctx: Ctx, user_id: int, kind: str) -> Path:
    base = ctx.settings.library_root if kind == "library" else ctx.settings.review_root
    return Path(base) / str(int(user_id))


def cache_path(ctx: Ctx, sha256: str, ext: str | None = None) -> Path:
    digest = (sha256 or "").strip().lower()
    if not _SHA.fullmatch(digest):
        raise AppError("bad_input")
    root = Path(getattr(ctx.settings, "cache_root", None) or Path("/data/cache"))
    return root / f"{digest}{suffix_for(f'file{ext}' if ext else None)}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remember_cache(ctx: Ctx, sha256: str, path: Path) -> Path:
    dest = cache_path(ctx, sha256, path.suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != dest.resolve() and not dest.exists():
        dest.write_bytes(path.read_bytes())
    ctx.catalog.put_cached_file(sha256, str(dest), dest.stat().st_size if dest.exists() else None)
    return dest


def cached_audio(ctx: Ctx, sha256: str) -> Path | None:
    stored = ctx.catalog.get_cached_file(sha256)
    if stored:
        path = Path(stored)
        expected = cache_path(ctx, sha256, path.suffix)
        if path.exists() and path.resolve() == expected.resolve():
            return path
        if path.exists():
            return path
    from app.formats import ALL_FORMATS, extension_for

    for fmt in ALL_FORMATS:
        dest = cache_path(ctx, sha256, extension_for(fmt))
        if dest.exists():
            return dest
    return None


cached_flac = cached_audio
