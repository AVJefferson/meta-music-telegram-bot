from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, fields, replace
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3

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


def _norm_key(key: str) -> str:
    return "".join(ch for ch in key.casefold() if ch.isalnum())


def overlay_tagset(base: TagSet, extra: TagSet) -> TagSet:
    updates = {item.name: getattr(extra, item.name) for item in fields(TagSet) if getattr(extra, item.name)}
    return replace(base, **updates) if updates else base


def _lookup(index: dict[str, str], *names: str) -> str:
    for name in names:
        value = index.get(_norm_key(name))
        if value:
            return value
    return ""


def _vorbis_index(audio: FLAC) -> dict[str, str]:
    index: dict[str, str] = {}
    if not audio.tags:
        return index
    for key, values in audio.tags.items():
        if not values:
            continue
        text = str(values[0]).strip()
        if text:
            index[_norm_key(str(key))] = text
    return index


def _id3_frame_text(frame: object) -> str:
    text = getattr(frame, "text", None)
    if isinstance(text, str):
        return text.strip()
    if text:
        return str(text[0]).strip()
    return ""


def _id3_index(path: Path) -> dict[str, str]:
    try:
        id3 = ID3(path)
    except Exception:
        return {}
    mapping = {
        "title": ("TIT2",),
        "album": ("TALB",),
        "artist": ("TPE1",),
        "albumartist": ("TPE2",),
        "composer": ("TCOM",),
        "genre": ("TCON",),
        "date": ("TDRC", "TYER", "TDRL", "TDAT"),
        "tracknumber": ("TRCK",),
        "discnumber": ("TPOS",),
        "lyrics": ("USLT",),
    }
    index: dict[str, str] = {}
    for field, frame_ids in mapping.items():
        for frame_id in frame_ids:
            for frame in id3.getall(frame_id):
                value = _id3_frame_text(frame)
                if value:
                    index[field] = value
                    break
            if field in index:
                break
    for frame in id3.getall("TXXX"):
        desc = _norm_key(str(getattr(frame, "desc", "") or ""))
        value = _id3_frame_text(frame)
        if not desc or not value or desc in index:
            continue
        if desc in {"albumartist", "albumartists"}:
            index.setdefault("albumartist", value)
        elif desc in mapping or desc in {"year", "date"}:
            index.setdefault("date" if desc == "year" else desc, value)
    return index


def _tagset_from_index(index: dict[str, str]) -> TagSet:
    date = _lookup(index, "date", "year", "origyear", "originaldate")
    return TagSet(
        title=_lookup(index, "title"),
        album=_lookup(index, "album"),
        artist=_lookup(index, "artist"),
        albumartist=_lookup(index, "albumartist", "albumartists"),
        composer=_lookup(index, "composer"),
        genre=_lookup(index, "genre"),
        date=year_from_date(date) or date,
        tracknumber=parse_track_number(_lookup(index, "tracknumber", "track")),
        discnumber=parse_track_number(_lookup(index, "discnumber", "disc")),
        lyrics=_lookup(index, "lyrics", "unsyncedlyrics", "unsyncedlyric"),
    )


def read_hints(path: Path, filename: str) -> TagHints:
    tags = read_tagset(path)
    return TagHints(
        title=tags.title,
        album=tags.album,
        artist=tags.artist,
        albumartist=tags.albumartist,
        composer=tags.composer,
        genre=tags.genre,
        date=tags.date,
        tracknumber=tags.tracknumber,
        discnumber=tags.discnumber,
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
    tags = TagSet()
    with suppress(Exception):
        tags = overlay_tagset(tags, _tagset_from_index(_vorbis_index(FLAC(path))))
    return overlay_tagset(tags, _tagset_from_index(_id3_index(path)))


def read_cover(path: Path) -> tuple[bytes | None, str | None]:
    audio = FLAC(path)
    pictures = audio.pictures or []
    if not pictures:
        return None, None
    front = next((pic for pic in pictures if pic.type == 3), pictures[0])
    return front.data, front.mime or "image/jpeg"


@dataclass(frozen=True)
class AudioMetrics:
    duration: float = 0.0
    bit_depth: int | None = None
    sample_rate: int | None = None
    bitrate_kbps: int | None = None


def read_audio_metrics(path: Path) -> AudioMetrics:
    audio = FLAC(path)
    info = audio.info
    duration = float(getattr(info, "length", 0.0) or 0.0)
    bit_depth = getattr(info, "bits_per_sample", None)
    sample_rate = getattr(info, "sample_rate", None)
    bitrate_bps = int(getattr(info, "bitrate", 0) or 0)
    if bitrate_bps > 0:
        kbps = max(1, round(bitrate_bps / 1000))
    elif duration > 0:
        kbps = max(1, round(path.stat().st_size * 8 / duration / 1000))
    else:
        kbps = None
    return AudioMetrics(duration, bit_depth, sample_rate, kbps)


def audio_info(path: Path) -> tuple[float, int | None, int | None]:
    metrics = read_audio_metrics(path)
    return metrics.duration, metrics.bit_depth, metrics.sample_rate


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
