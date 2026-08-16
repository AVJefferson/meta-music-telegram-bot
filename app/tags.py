from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mutagen.flac import FLAC, Picture

from app.models import Enrichment, Identity, TagHints, TagSet
from app.util import format_artist_list, is_synced_lrc, parse_track_number, sanitize_filename, year_from_date

ALLOWED = (
    "TITLE",
    "ALBUM",
    "ARTIST",
    "ALBUMARTIST",
    "COMPOSER",
    "GENRE",
    "DATE",
    "TRACKNUMBER",
    "DISCNUMBER",
    "LYRICS",
)
SPARSE_TAG_FIELDS = ("composer", "genre", "date")


def _first(audio: FLAC, *keys: str) -> str:
    for key in keys:
        values = audio.get(key)
        if values:
            return str(values[0]).strip()
    return ""


def read_hints(path: Path, filename: str) -> TagHints:
    audio = FLAC(path)
    return TagHints(
        title=_first(audio, "title", "TITLE"),
        album=_first(audio, "album", "ALBUM"),
        artist=_first(audio, "artist", "ARTIST"),
        albumartist=_first(audio, "albumartist", "ALBUM ARTIST", "ALBUMARTIST"),
        composer=_first(audio, "composer", "COMPOSER"),
        genre=_first(audio, "genre", "GENRE"),
        date=_first(audio, "date", "DATE", "year", "YEAR"),
        tracknumber=_first(audio, "tracknumber", "TRACKNUMBER", "track"),
        discnumber=_first(audio, "discnumber", "DISCNUMBER"),
        filename=filename,
    )


def hints_to_tagset(hints: TagHints) -> TagSet:
    return TagSet(
        title=hints.title,
        album=hints.album,
        artist=hints.artist,
        albumartist=hints.albumartist,
        composer=hints.composer,
        genre=hints.genre,
        date=year_from_date(hints.date) or hints.date,
        tracknumber=parse_track_number(hints.tracknumber),
        discnumber=parse_track_number(hints.discnumber),
        lyrics="",
    )


def read_tagset(path: Path) -> TagSet:
    audio = FLAC(path)
    return TagSet(
        title=_first(audio, "title", "TITLE"),
        album=_first(audio, "album", "ALBUM"),
        artist=_first(audio, "artist", "ARTIST"),
        albumartist=_first(audio, "albumartist", "ALBUM ARTIST", "ALBUMARTIST"),
        composer=_first(audio, "composer", "COMPOSER"),
        genre=_first(audio, "genre", "GENRE"),
        date=_first(audio, "date", "DATE", "year", "YEAR"),
        tracknumber=_first(audio, "tracknumber", "TRACKNUMBER", "track"),
        discnumber=_first(audio, "discnumber", "DISCNUMBER"),
        lyrics=_first(audio, "lyrics", "LYRICS"),
    )


def read_cover(path: Path) -> tuple[bytes | None, str | None]:
    audio = FLAC(path)
    pictures = audio.pictures or []
    if not pictures:
        return None, None
    front = next((pic for pic in pictures if pic.type == 3), pictures[0])
    return front.data, front.mime or "image/jpeg"


def audio_info(path: Path) -> tuple[float, int | None, int | None]:
    audio = FLAC(path)
    info = audio.info
    return (
        float(getattr(info, "length", 0.0) or 0.0),
        getattr(info, "bits_per_sample", None),
        getattr(info, "sample_rate", None),
    )


def identity_to_tags(identity: Identity, enrichment: Enrichment) -> TagSet:
    lyrics = enrichment.lyrics if is_synced_lrc(enrichment.lyrics) else ""
    return TagSet(
        title=identity.title,
        album=identity.album,
        artist=format_artist_list(identity.artists),
        albumartist=format_artist_list(identity.album_artists) or format_artist_list(identity.artists),
        composer=format_artist_list(identity.composers),
        genre=enrichment.genre,
        date=year_from_date(identity.year) or identity.year,
        tracknumber=identity.tracknumber,
        discnumber=identity.discnumber,
        lyrics=lyrics or "",
    )


def fill_sparse_tags(file_tags: TagSet, rec: TagSet) -> TagSet:
    updates = {}
    for key in SPARSE_TAG_FIELDS:
        if not (getattr(rec, key) or "") and (getattr(file_tags, key) or ""):
            updates[key] = getattr(file_tags, key)
    return replace(rec, **updates) if updates else rec


def write_tags(path: Path, tags: TagSet, cover: bytes | None, cover_mime: str | None) -> None:
    audio = FLAC(path)
    if audio.tags is None:
        audio.add_tags()
    else:
        audio.tags.clear()
    audio.clear_pictures()
    mapping = {
        "TITLE": tags.title,
        "ALBUM": tags.album,
        "ARTIST": tags.artist,
        "ALBUMARTIST": tags.albumartist,
        "COMPOSER": tags.composer,
        "GENRE": tags.genre,
        "DATE": tags.date,
        "TRACKNUMBER": tags.tracknumber,
        "DISCNUMBER": tags.discnumber,
        "LYRICS": tags.lyrics,
    }
    for key in ALLOWED:
        value = mapping[key]
        if value:
            audio[key] = value
    if cover:
        picture = Picture()
        picture.type = 3
        picture.mime = cover_mime or "image/jpeg"
        picture.desc = "Cover"
        picture.data = cover
        audio.add_picture(picture)
    audio.save()


def padded_track(tracknumber: str) -> str:
    digits = "".join(ch for ch in (tracknumber or "") if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(2)


def build_filename(tags: TagSet) -> str:
    title = sanitize_filename(tags.title or "Unknown Title")
    album_artist = sanitize_filename(tags.albumartist or tags.artist or "Unknown Artist")
    track = padded_track(tags.tracknumber)
    if track:
        return f"{album_artist} - {track} - {title}.flac"
    return f"{album_artist} - {title}.flac"
