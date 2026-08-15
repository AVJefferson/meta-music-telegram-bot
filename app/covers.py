from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from app.enrich import _fit_cover, fetch_cover, list_caa_fronts, list_itunes_album_cover
from app.models import Ctx, Identity, TagSet
from app.util import format_artist_list, normalize_match_text, sanitize_filename

log = logging.getLogger(__name__)

COVER_NAME = "cover.jpg"
COVER_TTL = timedelta(days=7)
MAX_COVER_OPTIONS = 10
_PLACEHOLDER_ALBUMS = {"", "unknown", "unknown album"}


@dataclass
class CoverHit:
    data: bytes | None = None
    mime: str | None = None
    source: str = "none"
    caa_release: str | None = None


@dataclass
class CoverOption:
    data: bytes
    mime: str
    source: str
    label: str
    digest: str
    caa_release: str | None = None
    url: str | None = None


def is_shareable_album(album: str) -> bool:
    return normalize_match_text(album) not in _PLACEHOLDER_ALBUMS


def album_folder_parts(topic: str, albumartist: str, album: str) -> list[str] | None:
    if not is_shareable_album(album):
        return None
    return [
        sanitize_filename(topic or "General"),
        sanitize_filename(albumartist or "Unknown Artist"),
        sanitize_filename(album),
    ]


def cover_album_key(album: str, albumartist: str) -> str:
    raw = f"{normalize_match_text(albumartist)}|{normalize_match_text(album)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _album_key(album: str, albumartist: str) -> str:
    return cover_album_key(album, albumartist)


def _local_path(covers_root: Path, album: str, albumartist: str) -> Path:
    return covers_root / f"{_album_key(album, albumartist)}.jpg"


def _identity_albumartist(identity: Identity) -> str:
    return format_artist_list(identity.album_artists) or format_artist_list(identity.artists)


def read_local(covers_root: Path, album: str, albumartist: str) -> tuple[bytes, str] | None:
    path = _local_path(covers_root, album, albumartist)
    if not path.is_file():
        return None
    age = time.time() - path.stat().st_mtime
    if age > COVER_TTL.total_seconds():
        try:
            path.unlink()
        except OSError:
            log.debug("stale cover unlink failed %s", path)
        return None
    data = path.read_bytes()
    if not data:
        return None
    return data, "image/jpeg"


def write_local(covers_root: Path, album: str, albumartist: str, data: bytes) -> None:
    path = _local_path(covers_root, album, albumartist)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def purge_stale_covers(covers_root: Path) -> int:
    if not covers_root.is_dir():
        return 0
    cutoff = time.time() - COVER_TTL.total_seconds()
    removed = 0
    for path in covers_root.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink()
                removed += 1
        except OSError:
            log.debug("cover purge failed %s", path)
    return removed


def _jpeg_bytes(data: bytes) -> tuple[bytes, str] | None:
    try:
        return _fit_cover(data)
    except Exception:
        log.debug("cover normalize failed")
        return None


def _download_drive_cover(ctx: Ctx, topic: str, albumartist: str, album: str) -> tuple[bytes, str] | None:
    parts = album_folder_parts(topic, albumartist, album)
    if not parts:
        return None
    folder_id = ctx.drive.find_path(ctx.settings.gdrive_folder_id, parts)
    if not folder_id:
        return None
    hits = ctx.drive.find_name_conflicts(folder_id, COVER_NAME)
    if not hits:
        return None
    try:
        data = ctx.drive.download_bytes(hits[0].id)
    except Exception:
        log.debug("drive cover download failed", exc_info=True)
        return None
    if not data:
        return None
    return _jpeg_bytes(data)


async def existing_album_cover(
    ctx: Ctx,
    topic: str,
    album: str,
    albumartist: str,
) -> CoverHit | None:
    if not is_shareable_album(album):
        return None
    covers_root = ctx.settings.covers_root
    local = read_local(covers_root, album, albumartist)
    if local:
        return CoverHit(data=local[0], mime=local[1], source="cache")
    drive_hit = await asyncio.to_thread(_download_drive_cover, ctx, topic, albumartist, album)
    if drive_hit:
        try:
            write_local(covers_root, album, albumartist, drive_hit[0])
        except OSError:
            log.debug("cover cache write failed")
        return CoverHit(data=drive_hit[0], mime=drive_hit[1], source="drive")
    return None


def cache_album_cover(ctx: Ctx, album: str, albumartist: str, data: bytes) -> None:
    if not is_shareable_album(album) or not data:
        return
    try:
        write_local(ctx.settings.covers_root, album, albumartist, data)
    except OSError:
        log.debug("cover cache write failed")


async def list_cover_candidates(
    ctx: Ctx,
    identity: Identity,
    file_cover: tuple[bytes, str] | None,
) -> list[CoverOption]:
    options: list[CoverOption] = []
    seen: set[str] = set()

    def add(
        data: bytes,
        _mime: str | None,
        source: str,
        label: str,
        caa_release: str | None = None,
        url: str | None = None,
    ) -> None:
        if len(options) >= MAX_COVER_OPTIONS:
            return
        fitted = _jpeg_bytes(data)
        if not fitted:
            return
        payload, out_mime = fitted
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        options.append(
            CoverOption(
                data=payload,
                mime=out_mime,
                source=source,
                label=label,
                digest=digest,
                caa_release=caa_release,
                url=url or None,
            )
        )

    if file_cover and file_cover[0]:
        add(file_cover[0], file_cover[1], "file", "file")
    for cover, mbid, url in await list_caa_fronts(ctx.http, identity):
        add(cover[0], cover[1], "caa", "CAA", mbid, url)
    itunes = await list_itunes_album_cover(ctx.http, identity)
    if itunes:
        cover, url = itunes
        add(cover[0], cover[1], "itunes", "iTunes", url=url)
    caa_opts = [opt for opt in options if opt.source == "caa"]
    if len(caa_opts) > 1:
        for index, opt in enumerate(caa_opts, start=1):
            opt.label = f"CAA {index}"
    return options


def upload_album_cover_if_missing(ctx: Ctx, parent_id: str, data: bytes, mime: str | None) -> None:
    hits = ctx.drive.find_name_conflicts(parent_id, COVER_NAME)
    if hits:
        return
    payload = data
    out_mime = mime or "image/jpeg"
    if out_mime != "image/jpeg":
        fitted = _jpeg_bytes(data)
        if not fitted:
            return
        payload, out_mime = fitted
    ctx.drive.upload_bytes(payload, parent_id, COVER_NAME, out_mime)


async def resolve_album_cover(
    ctx: Ctx,
    identity: Identity,
    topic: str,
    *,
    album: str | None = None,
    albumartist: str | None = None,
) -> CoverHit:
    album_name = album if album is not None else identity.album
    artist_name = albumartist if albumartist is not None else _identity_albumartist(identity)
    shareable = is_shareable_album(album_name)
    covers_root = ctx.settings.covers_root

    if shareable:
        local = read_local(covers_root, album_name, artist_name)
        if local:
            return CoverHit(data=local[0], mime=local[1], source="cache")
        drive_hit = await asyncio.to_thread(
            _download_drive_cover, ctx, topic, artist_name, album_name
        )
        if drive_hit:
            try:
                write_local(covers_root, album_name, artist_name, drive_hit[0])
            except OSError:
                log.debug("cover cache write failed")
            return CoverHit(data=drive_hit[0], mime=drive_hit[1], source="drive")

    cover, source, caa_release = await fetch_cover(
        ctx.http, identity, album_search=shareable
    )
    if cover is None:
        return CoverHit(source="none")
    data, mime = cover
    if shareable:
        try:
            write_local(covers_root, album_name, artist_name, data)
        except OSError:
            log.debug("cover cache write failed")
    return CoverHit(data=data, mime=mime, source=source, caa_release=caa_release)


def cover_identity(identity: Identity, tags: TagSet) -> Identity:
    artists = [tags.albumartist] if tags.albumartist else list(identity.album_artists)
    return replace(
        identity,
        album=tags.album,
        album_artists=artists or list(identity.album_artists),
    )


def album_changed(identity: Identity, tags: TagSet) -> bool:
    old_album = identity.album or ""
    old_artist = _identity_albumartist(identity)
    new_album = tags.album or ""
    new_artist = tags.albumartist or tags.artist or ""
    return old_album != new_album or old_artist != new_artist
