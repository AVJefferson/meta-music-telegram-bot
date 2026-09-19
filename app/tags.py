from __future__ import annotations

import base64
from contextlib import suppress
from dataclasses import dataclass, fields, replace
from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, USLT
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from app.formats import format_from_path, format_label, normalize_ext
from app.models import Enrichment, Identity, TagHints, TagSet
from app.util import format_artist_list, is_synced_lrc, normalize_name_list, parse_track_number, sanitize_filename, year_from_date

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
_ID3_CLEAR = ("TIT2", "TALB", "TPE1", "TPE2", "TCOM", "TCON", "TDRC", "TYER", "TRCK", "TPOS", "USLT", "APIC")


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


def _vorbis_index(audio) -> dict[str, str]:
    index: dict[str, str] = {}
    tags = getattr(audio, "tags", None)
    if not tags:
        return index
    items = tags.items() if hasattr(tags, "items") else []
    for key, values in items:
        if not values:
            continue
        text = str(values[0] if not isinstance(values, str) else values).strip()
        if text:
            index[_norm_key(str(key))] = text
    return index


def _id3_frame_text(frame: object) -> str:
    text = getattr(frame, "text", None)
    if isinstance(text, str):
        return text.strip()
    if text:
        return str(text[0]).strip()
    return str(getattr(frame, "text", "") or "").strip()


def _load_id3(path: Path) -> ID3 | None:
    if format_from_path(path) == "wav":
        with suppress(Exception):
            tags = WAVE(path).tags
            if tags is not None:
                return tags
    with suppress(Exception):
        return ID3(path)
    return None


def _id3_index(path: Path) -> dict[str, str]:
    id3 = _load_id3(path)
    if not id3:
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


def _mp4_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    if isinstance(value, (list, tuple)):
        return _mp4_text(value[0]) if value else ""
    return str(value).strip()


def _mp4_pair(value: object) -> str:
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, (list, tuple)) and first:
            return str(first[0])
        return str(first)
    return ""


def _mp4_index(path: Path) -> dict[str, str]:
    try:
        audio = MP4(path)
    except Exception:
        return {}
    tags = audio.tags or {}
    index: dict[str, str] = {}
    mapping = {
        "title": "\xa9nam",
        "album": "\xa9alb",
        "artist": "\xa9ART",
        "albumartist": "aART",
        "composer": "\xa9wrt",
        "genre": "\xa9gen",
        "date": "\xa9day",
        "lyrics": "\xa9lyr",
    }
    for field, atom in mapping.items():
        text = _mp4_text(tags.get(atom))
        if text:
            index[field] = text
    track = _mp4_pair(tags.get("trkn"))
    if track:
        index["tracknumber"] = track
    disc = _mp4_pair(tags.get("disk"))
    if disc:
        index["discnumber"] = disc
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


def _open_vorbis(path: Path):
    fmt = format_from_path(path)
    if fmt == "opus":
        return OggOpus(path)
    if fmt == "ogg":
        return OggVorbis(path)
    return FLAC(path)


def read_tagset(path: Path) -> TagSet:
    tags = TagSet()
    fmt = format_from_path(path)
    if fmt in {"flac", "ogg", "opus", None}:
        with suppress(Exception):
            tags = overlay_tagset(tags, _tagset_from_index(_vorbis_index(_open_vorbis(path))))
    if fmt == "m4a":
        with suppress(Exception):
            tags = overlay_tagset(tags, _tagset_from_index(_mp4_index(path)))
    return overlay_tagset(tags, _tagset_from_index(_id3_index(path)))


def _cover_flac(path: Path) -> tuple[bytes | None, str | None]:
    audio = FLAC(path)
    pictures = audio.pictures or []
    if not pictures:
        return None, None
    front = next((pic for pic in pictures if pic.type == 3), pictures[0])
    return front.data, front.mime or "image/jpeg"


def _cover_id3(path: Path) -> tuple[bytes | None, str | None]:
    id3 = _load_id3(path)
    if not id3:
        return None, None
    pictures = id3.getall("APIC")
    if not pictures:
        return None, None
    front = next((pic for pic in pictures if getattr(pic, "type", None) == 3), pictures[0])
    return front.data, front.mime or "image/jpeg"


def _cover_mp4(path: Path) -> tuple[bytes | None, str | None]:
    audio = MP4(path)
    covers = (audio.tags or {}).get("covr") or []
    if not covers:
        return None, None
    cover = covers[0]
    data = bytes(cover)
    fmt = getattr(cover, "imageformat", None)
    mime = "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
    return data, mime


def _cover_vorbis_pic(path: Path) -> tuple[bytes | None, str | None]:
    audio = _open_vorbis(path)
    raws = []
    tags = getattr(audio, "tags", None)
    if tags:
        raws = list(tags.get("metadata_block_picture") or [])
    for raw in raws:
        picture = Picture(base64.b64decode(str(raw)))
        if picture.data:
            return picture.data, picture.mime or "image/jpeg"
    return None, None


def read_cover(path: Path) -> tuple[bytes | None, str | None]:
    fmt = format_from_path(path)
    readers = {
        "flac": (_cover_flac,),
        "mp3": (_cover_id3,),
        "wav": (_cover_id3,),
        "m4a": (_cover_mp4,),
        "ogg": (_cover_vorbis_pic,),
        "opus": (_cover_vorbis_pic,),
    }
    for reader in readers.get(fmt, (_cover_flac, _cover_id3, _cover_mp4, _cover_vorbis_pic)):
        with suppress(Exception):
            data, mime = reader(path)
            if data:
                return data, mime
    for reader in (_cover_flac, _cover_id3, _cover_mp4, _cover_vorbis_pic):
        with suppress(Exception):
            data, mime = reader(path)
            if data:
                return data, mime
    return None, None


@dataclass(frozen=True)
class AudioMetrics:
    duration: float = 0.0
    bit_depth: int | None = None
    sample_rate: int | None = None
    bitrate_kbps: int | None = None
    format_name: str | None = None


def read_audio_metrics(path: Path) -> AudioMetrics:
    from mutagen.mp3 import MP3

    fmt = format_from_path(path)
    loaders = {
        "flac": FLAC,
        "mp3": MP3,
        "wav": WAVE,
        "m4a": MP4,
        "ogg": OggVorbis,
        "opus": OggOpus,
    }
    loader = loaders.get(fmt)
    if loader is not None:
        audio = loader(path)
    else:
        from mutagen import File

        audio = File(path)
        if audio is None:
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
    return AudioMetrics(duration, bit_depth, sample_rate, kbps, format_label(path))


def audio_info(path: Path) -> tuple[float, int | None, int | None]:
    metrics = read_audio_metrics(path)
    return metrics.duration, metrics.bit_depth, metrics.sample_rate


def identity_to_tags(identity: Identity, enrichment: Enrichment) -> TagSet:
    lyrics = enrichment.lyrics if is_synced_lrc(enrichment.lyrics) else ""
    albumartist = format_artist_list(identity.album_artists) or format_artist_list(identity.artists)
    return TagSet(
        title=identity.title,
        album=identity.album,
        artist=format_artist_list(identity.artists, lead=albumartist),
        albumartist=albumartist,
        composer=format_artist_list(identity.composers, lead=albumartist),
        genre=enrichment.genre,
        date=year_from_date(identity.year) or identity.year,
        tracknumber=identity.tracknumber,
        discnumber=identity.discnumber,
        lyrics=lyrics or "",
    )


def normalize_tagset(tags: TagSet, mapper=None) -> TagSet:
    albumartist = normalize_name_list(tags.albumartist)
    genre = mapper.compose(tags.genre) if mapper is not None else tags.genre
    return replace(
        tags,
        albumartist=albumartist,
        artist=normalize_name_list(tags.artist, lead=albumartist),
        composer=normalize_name_list(tags.composer, lead=albumartist),
        genre=genre,
    )


def fill_sparse_tags(file_tags: TagSet, rec: TagSet) -> TagSet:
    updates = {}
    for key in SPARSE_TAG_FIELDS:
        if not (getattr(rec, key) or "") and (getattr(file_tags, key) or ""):
            updates[key] = getattr(file_tags, key)
    return replace(rec, **updates) if updates else rec


def _vorbis_mapping(tags: TagSet) -> dict[str, str]:
    return {
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


def _write_flac(path: Path, tags: TagSet, cover: bytes | None, cover_mime: str | None) -> None:
    audio = FLAC(path)
    if audio.tags is None:
        audio.add_tags()
    else:
        audio.tags.clear()
    audio.clear_pictures()
    mapping = _vorbis_mapping(tags)
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


def _fill_id3(id3: ID3, tags: TagSet, cover: bytes | None, cover_mime: str | None) -> None:
    for frame_id in _ID3_CLEAR:
        id3.delall(frame_id)
    if tags.title:
        id3.add(TIT2(encoding=3, text=tags.title))
    if tags.album:
        id3.add(TALB(encoding=3, text=tags.album))
    if tags.artist:
        id3.add(TPE1(encoding=3, text=tags.artist))
    if tags.albumartist:
        id3.add(TPE2(encoding=3, text=tags.albumartist))
    if tags.composer:
        id3.add(TCOM(encoding=3, text=tags.composer))
    if tags.genre:
        id3.add(TCON(encoding=3, text=tags.genre))
    if tags.date:
        id3.add(TDRC(encoding=3, text=tags.date))
    if tags.tracknumber:
        id3.add(TRCK(encoding=3, text=tags.tracknumber))
    if tags.discnumber:
        id3.add(TPOS(encoding=3, text=tags.discnumber))
    if tags.lyrics:
        id3.add(USLT(encoding=3, lang="eng", desc="", text=tags.lyrics))
    if cover:
        id3.add(
            APIC(
                encoding=3,
                mime=cover_mime or "image/jpeg",
                type=3,
                desc="Cover",
                data=cover,
            )
        )


def _write_id3(path: Path, tags: TagSet, cover: bytes | None, cover_mime: str | None) -> None:
    try:
        id3 = ID3(path)
    except Exception:
        id3 = ID3()
    _fill_id3(id3, tags, cover, cover_mime)
    id3.save(path)


def _write_wav(path: Path, tags: TagSet, cover: bytes | None, cover_mime: str | None) -> None:
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    _fill_id3(audio.tags, tags, cover, cover_mime)
    audio.save()


def _write_mp4(path: Path, tags: TagSet, cover: bytes | None, cover_mime: str | None) -> None:
    audio = MP4(path)
    mapping = {
        "\xa9nam": tags.title,
        "\xa9alb": tags.album,
        "\xa9ART": tags.artist,
        "aART": tags.albumartist,
        "\xa9wrt": tags.composer,
        "\xa9gen": tags.genre,
        "\xa9day": tags.date,
        "\xa9lyr": tags.lyrics,
    }
    for atom, value in mapping.items():
        if value:
            audio[atom] = [value]
        elif atom in audio:
            del audio[atom]
    track_digits = "".join(ch for ch in (tags.tracknumber or "") if ch.isdigit())
    if track_digits:
        audio["trkn"] = [(int(track_digits), 0)]
    elif "trkn" in audio:
        del audio["trkn"]
    disc_digits = "".join(ch for ch in (tags.discnumber or "") if ch.isdigit())
    if disc_digits:
        audio["disk"] = [(int(disc_digits), 0)]
    elif "disk" in audio:
        del audio["disk"]
    if cover:
        kind = MP4Cover.FORMAT_PNG if (cover_mime or "").casefold().endswith("png") else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover, imageformat=kind)]
    elif "covr" in audio:
        del audio["covr"]
    audio.save()


def _write_ogg(path: Path, tags: TagSet, cover: bytes | None, cover_mime: str | None, *, opus: bool) -> None:
    audio = OggOpus(path) if opus else OggVorbis(path)
    if audio.tags is None:
        audio.add_tags()
    for key in list(audio.keys()):
        del audio[key]
    mapping = _vorbis_mapping(tags)
    for key in ALLOWED:
        value = mapping[key]
        if value:
            audio[key] = [value]
    if cover:
        picture = Picture()
        picture.type = 3
        picture.mime = cover_mime or "image/jpeg"
        picture.desc = "Cover"
        picture.data = cover
        audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def write_tags(path: Path, tags: TagSet, cover: bytes | None, cover_mime: str | None) -> None:
    fmt = format_from_path(path)
    if fmt == "mp3":
        _write_id3(path, tags, cover, cover_mime)
        return
    if fmt == "wav":
        _write_wav(path, tags, cover, cover_mime)
        return
    if fmt == "m4a":
        _write_mp4(path, tags, cover, cover_mime)
        return
    if fmt == "ogg":
        _write_ogg(path, tags, cover, cover_mime, opus=False)
        return
    if fmt == "opus":
        _write_ogg(path, tags, cover, cover_mime, opus=True)
        return
    _write_flac(path, tags, cover, cover_mime)


def padded_track(tracknumber: str) -> str:
    digits = "".join(ch for ch in (tracknumber or "") if ch.isdigit())
    if not digits:
        return ""
    return digits.zfill(2)


def build_filename(tags: TagSet, ext: str = ".flac") -> str:
    title = sanitize_filename(tags.title or "Unknown Title")
    album_artist = sanitize_filename(tags.albumartist or tags.artist or "Unknown Artist")
    suffix = normalize_ext(ext)
    track = padded_track(tags.tracknumber)
    if track:
        return f"{album_artist} - {track} - {title}{suffix}"
    return f"{album_artist} - {title}{suffix}"
