from __future__ import annotations

from pathlib import Path
from typing import Any

from app.models import Enrichment, Identity, TagHints, TagSet
from app.util import file_stem_hints, format_artist_list, year_from_date


def _line(key: str, value: object) -> str:
    text = "" if value is None else str(value).strip()
    return f"{key}: {text}"


def _block(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"== {title} ==\n{body}"


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def merge_enrichment(report: dict[str, Any], enrichment: Enrichment) -> dict[str, Any]:
    out = dict(report)
    out["itunes"] = dict(enrichment.itunes_report or {})
    out["lastfm_tags"] = list(enrichment.lastfm_tags or [])
    out["cover_source"] = enrichment.cover_source or "none"
    out["coverartarchive"] = {
        "used": enrichment.cover_source == "caa",
        "release": enrichment.caa_release or "",
    }
    return out


def apply_chosen(report: dict[str, Any], tags: TagSet, identity: Identity) -> dict[str, Any]:
    out = dict(report)
    out["confidence"] = identity.confidence
    out["confidence_reason"] = identity.confidence_reason
    out["chosen_source"] = identity.source
    out["chosen_mbid"] = identity.mb_recording_id or ""
    out["chosen"] = {
        "title": tags.title,
        "artist": tags.artist,
        "album": tags.album,
        "albumartist": tags.albumartist,
        "composer": tags.composer,
        "genre": tags.genre,
        "year": tags.date,
        "track": tags.tracknumber,
        "disc": tags.discnumber,
        "cover": out.get("cover_source") or "none",
    }
    return out


def seed_report(hints: TagHints, identity: Identity) -> dict[str, Any]:
    report = dict(identity.source_report or {})
    report.setdefault("file", hints.filename)
    report.setdefault("filename_stem", Path(hints.filename).stem)
    report.setdefault("duration", identity.duration)
    report.setdefault("bit_depth", identity.bit_depth)
    report.setdefault("sample_rate", identity.sample_rate)
    report.setdefault(
        "file_tags",
        {
            "title": hints.title,
            "artist": hints.artist,
            "album": hints.album,
            "albumartist": hints.albumartist,
            "composer": hints.composer,
            "genre": hints.genre,
            "year": year_from_date(hints.date) or hints.date,
            "track": hints.tracknumber,
            "disc": hints.discnumber,
        },
    )
    report.setdefault("filename", {"stem": file_stem_hints(hints.filename) or Path(hints.filename).stem})
    return report


def render_songlog(report: dict[str, Any]) -> str:
    duration = report.get("duration") or 0
    bit_depth = report.get("bit_depth") or "?"
    sample_rate = report.get("sample_rate") or "?"
    try:
        duration_s = f"{int(round(float(duration)))}s"
    except (TypeError, ValueError):
        duration_s = f"{duration}s"

    header = [
        _line("file", report.get("file") or ""),
        f"duration: {duration_s}  {bit_depth}/{sample_rate}",
        f"confidence: {report.get('confidence') or ''}  reason: {report.get('confidence_reason') or ''}",
        f"chosen: {report.get('chosen_source') or ''} recording {report.get('chosen_mbid') or ''}",
        "",
    ]

    file_tags = report.get("file_tags") or {}
    file_block = _block(
        "file tags",
        [
            _line("title", file_tags.get("title")),
            _line("artist", file_tags.get("artist")),
            _line("album", file_tags.get("album")),
            _line("albumartist", file_tags.get("albumartist")),
            _line("composer", file_tags.get("composer")),
            _line("genre", file_tags.get("genre")),
            _line("year", file_tags.get("year")),
            _line("track", file_tags.get("track")),
            _line("disc", file_tags.get("disc")),
        ],
    )

    filename = report.get("filename") or {}
    filename_block = _block("filename", [_line("stem", filename.get("stem") or report.get("filename_stem"))])

    ac = report.get("acoustid") or {}
    ac_lines = [
        f"score: {ac.get('score') if ac.get('score') is not None else ''}  id: {ac.get('id') or ''}",
        "recordings:",
    ]
    recordings = ac.get("recordings") or []
    if recordings:
        for rec in recordings:
            ac_lines.append(
                f"  - {rec.get('mbid') or ''} {rec.get('title') or ''} "
                f"{rec.get('artist') or ''} {rec.get('duration') or ''}"
            )
    else:
        ac_lines.append("  (none)")
    if ac.get("clusters") is not None:
        ac_lines.append(_line("clusters", ac.get("clusters")))
    acoustid_block = _block("acoustid", ac_lines)

    mb = report.get("musicbrainz") or {}
    mb_block = _block(
        "musicbrainz",
        [
            f"recording {mb.get('recording') or ''} / release {mb.get('release') or ''} / "
            f"release-group {mb.get('release_group') or ''}",
            _line("title", mb.get("title")),
            _line("artist", mb.get("artist")),
            _line("album", mb.get("album")),
            _line("albumartist", mb.get("albumartist")),
            _line("composer", mb.get("composer")),
            _line("year", mb.get("year")),
            _line("track", mb.get("track")),
            _line("disc", mb.get("disc")),
            _line("genre-tags", ", ".join(mb.get("genre_tags") or [])),
        ],
    )

    itunes = report.get("itunes") or {}
    itunes_block = _block(
        "itunes",
        [
            _line("title", itunes.get("title")),
            _line("artist", itunes.get("artist")),
            _line("album", itunes.get("album")),
            _line("year", itunes.get("year")),
            _line("genre", itunes.get("genre")),
            f"artwork: {_yes_no(itunes.get('artwork'))}",
        ],
    )

    lastfm_block = _block("lastfm", [_line("tags", ", ".join(report.get("lastfm_tags") or []))])

    caa = report.get("coverartarchive") or {}
    caa_block = _block(
        "coverartarchive",
        [
            f"used: {_yes_no(caa.get('used'))}  release: {caa.get('release') or ''}",
        ],
    )

    chosen = report.get("chosen") or {}
    chosen_block = _block(
        "CHOSEN",
        [
            _line("title", chosen.get("title")),
            _line("artist", chosen.get("artist")),
            _line("album", chosen.get("album")),
            _line("albumartist", chosen.get("albumartist")),
            _line("composer", chosen.get("composer")),
            _line("genre", chosen.get("genre")),
            _line("year", chosen.get("year")),
            _line("track", chosen.get("track")),
            _line("disc", chosen.get("disc")),
            _line("cover", chosen.get("cover") or report.get("cover_source") or "none"),
        ],
    )

    return "\n".join(header) + "\n".join(
        [
            file_block,
            "",
            filename_block,
            "",
            acoustid_block,
            "",
            mb_block,
            "",
            itunes_block,
            "",
            lastfm_block,
            "",
            caa_block,
            "",
            chosen_block,
            "",
        ]
    )


def mb_snapshot(identity: Identity) -> dict[str, Any]:
    return {
        "recording": identity.mb_recording_id or "",
        "release": identity.mb_release_id or "",
        "release_group": identity.mb_release_group_id or "",
        "title": identity.title,
        "artist": format_artist_list(identity.artists),
        "album": identity.album,
        "albumartist": format_artist_list(identity.album_artists),
        "composer": format_artist_list(identity.composers),
        "year": identity.year,
        "track": identity.tracknumber,
        "disc": identity.discnumber,
        "genre_tags": list(identity.raw_genre_tags or []),
    }
