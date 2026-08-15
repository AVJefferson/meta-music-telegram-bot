from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import acoustid
import musicbrainzngs

from app.config import Settings
from app.models import Candidate, Identity, TagHints
from app.tags import audio_info
from app.util import file_stem_hints, parse_mb_user_agent, parse_track_number, split_artist_field, year_from_date

log = logging.getLogger(__name__)

COMPOSER_TYPES = {"composer", "lyricist", "writer", "songwriter"}
ACOUSTID_META = "recordings releases releasegroups tracks compress"


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def artist_names_from_credit(credit: Any) -> list[str]:
    names: list[str] = []
    for part in _as_list(credit):
        if not isinstance(part, dict):
            continue
        name = part.get("name")
        if not name:
            artist = part.get("artist") or {}
            name = artist.get("name")
        if name:
            names.append(str(name))
    return names


def artist_names_from_acoustid(artists: Any) -> list[str]:
    names: list[str] = []
    for part in _as_list(artists):
        if isinstance(part, dict) and part.get("name"):
            names.append(str(part["name"]))
        elif isinstance(part, str):
            names.append(part)
    return names


class MBClient:
    def __init__(self, user_agent: str) -> None:
        name, version, contact = parse_mb_user_agent(user_agent)
        musicbrainzngs.set_useragent(name, version, contact)
        musicbrainzngs.set_rate_limit(limit_or_interval=1.0, new_requests=1)
        self._lock = threading.Lock()

    def _call(self, fn, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)

    def recording(self, mbid: str) -> dict:
        return self._call(
            musicbrainzngs.get_recording_by_id,
            mbid,
            includes=["artists", "artist-credits", "releases", "tags", "work-rels", "isrcs"],
        )["recording"]

    def release(self, mbid: str) -> dict:
        return self._call(
            musicbrainzngs.get_release_by_id,
            mbid,
            includes=["artist-credits", "release-groups", "media", "recordings", "tags"],
        )["release"]

    def work(self, mbid: str) -> dict:
        return self._call(
            musicbrainzngs.get_work_by_id,
            mbid,
            includes=["artist-rels"],
        )["work"]

    def search_recordings(self, title: str, artist: str | None, limit: int = 5) -> list[dict]:
        kwargs: dict[str, Any] = {"recording": title, "limit": limit}
        if artist:
            kwargs["artist"] = artist
        data = self._call(musicbrainzngs.search_recordings, **kwargs)
        return _as_list(data.get("recording-list"))


def _score_release(rel: dict) -> int:
    score = 0
    status = str(rel.get("status") or "").lower()
    if status == "official":
        score += 10
    elif status == "promotion":
        score += 2
    rg = rel.get("release-group") or {}
    primary = str(rg.get("type") or rg.get("primary-type") or "").lower()
    if primary == "album":
        score += 6
    elif primary == "ep":
        score += 4
    elif primary == "single":
        score += 3
    if rel.get("date") or rg.get("first-release-date"):
        score += 1
    return score


def _track_position(release: dict, recording_id: str) -> tuple[str, str]:
    for medium in _as_list(release.get("medium-list")):
        disc = str(medium.get("position") or "")
        for track in _as_list(medium.get("track-list")):
            rec = track.get("recording") or {}
            if rec.get("id") == recording_id:
                number = parse_track_number(str(track.get("number") or track.get("position") or ""))
                disc_out = "" if disc in {"", "1"} else parse_track_number(disc)
                return number, disc_out
    return "", ""


def _composers_from_recording(mb: MBClient, recording: dict) -> list[str]:
    names: list[str] = []
    for rel in _as_list(recording.get("work-relation-list")):
        work = rel.get("work") or {}
        work_id = work.get("id")
        if not work_id:
            continue
        try:
            full = mb.work(work_id)
        except musicbrainzngs.WebServiceError:
            log.debug("work lookup failed for %s", work_id)
            continue
        for artist_rel in _as_list(full.get("artist-relation-list")):
            rel_type = str(artist_rel.get("type") or "").lower()
            if rel_type not in COMPOSER_TYPES:
                continue
            artist = artist_rel.get("artist") or {}
            name = artist.get("name")
            if name:
                names.append(str(name))
    return names


def _tags_from(obj: dict) -> list[str]:
    out: list[str] = []
    for tag in _as_list(obj.get("tag-list")):
        name = tag.get("name") if isinstance(tag, dict) else None
        if name:
            out.append(str(name))
    return out


def _identity_from_recording(
    mb: MBClient,
    mbid: str,
    *,
    duration: float,
    bit_depth: int | None,
    sample_rate: int | None,
    acoustid: str | None,
    acoustid_score: float | None,
    source: str,
) -> Identity:
    recording = mb.recording(mbid)
    artists = artist_names_from_credit(recording.get("artist-credit"))
    title = str(recording.get("title") or "")
    raw_tags = _tags_from(recording)

    releases = _as_list(recording.get("release-list"))
    releases_sorted = sorted(releases, key=_score_release, reverse=True)
    album = ""
    album_artists: list[str] = []
    year = ""
    tracknumber = ""
    discnumber = ""
    release_id = None
    rg_id = None

    for thin in releases_sorted[:8]:
        rid = thin.get("id")
        if not rid:
            continue
        try:
            release = mb.release(rid)
        except musicbrainzngs.WebServiceError:
            continue
        release_id = rid
        rg = release.get("release-group") or {}
        rg_id = rg.get("id")
        album = str(release.get("title") or thin.get("title") or "")
        album_artists = artist_names_from_credit(release.get("artist-credit"))
        year = year_from_date(rg.get("first-release-date")) or year_from_date(release.get("date"))
        tracknumber, discnumber = _track_position(release, mbid)
        raw_tags.extend(_tags_from(release))
        raw_tags.extend(_tags_from(rg))
        break

    if not album and releases:
        album = str(releases[0].get("title") or "")
        year = year_from_date(releases[0].get("date"))
        release_id = releases[0].get("id")

    composers = _composers_from_recording(mb, recording)

    return Identity(
        confidence="low",
        title=title,
        album=album,
        artists=artists,
        album_artists=album_artists or artists,
        composers=composers,
        year=year,
        tracknumber=tracknumber,
        discnumber=discnumber,
        duration=duration,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        raw_genre_tags=raw_tags,
        acoustid=acoustid,
        acoustid_score=acoustid_score,
        mb_recording_id=mbid,
        mb_release_id=release_id,
        mb_release_group_id=rg_id,
        source=source,
    )


def _acoustid_lookup(path: Path, api_key: str) -> dict | None:
    duration, fingerprint = acoustid.fingerprint_file(str(path))
    response = acoustid.lookup(api_key, fingerprint, duration, meta=ACOUSTID_META)
    if response.get("status") != "ok":
        return None
    results = response.get("results") or []
    if not results:
        return None
    results = sorted(results, key=lambda r: float(r.get("score") or 0), reverse=True)
    best = results[0]
    best["_fp_duration"] = duration
    return best


def _unique_recordings(ac_result: dict) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for rec in _as_list(ac_result.get("recordings")):
        rid = rec.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(rec)
    return out


def _candidates_from_acoustid(recordings: list[dict], score: float) -> list[Candidate]:
    out: list[Candidate] = []
    for rec in recordings[:8]:
        out.append(
            Candidate(
                mb_recording_id=rec.get("id") or "",
                title=str(rec.get("title") or ""),
                artist=" / ".join(artist_names_from_acoustid(rec.get("artists"))),
                score=score,
            )
        )
    return out


def _identity_from_hints(
    hints: TagHints,
    duration: float,
    bit_depth: int | None,
    sample_rate: int | None,
) -> Identity:
    artists = split_artist_field(hints.artist)
    album_artists = split_artist_field(hints.albumartist)
    composers = split_artist_field(hints.composer)
    title = hints.title or file_stem_hints(hints.filename)
    return Identity(
        confidence="low",
        title=title,
        album=hints.album,
        artists=artists,
        album_artists=album_artists or artists,
        composers=composers,
        year=year_from_date(hints.date),
        tracknumber=parse_track_number(hints.tracknumber),
        discnumber=parse_track_number(hints.discnumber),
        duration=duration,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        raw_genre_tags=[g.strip() for g in hints.genre.replace("|", ",").split(",") if g.strip()],
        source="existing-tags",
    )


def identify_file(path: Path, hints: TagHints, settings: Settings, mb: MBClient) -> Identity:
    duration, bit_depth, sample_rate = audio_info(path)
    if duration <= 0:
        duration = 0.0

    ac_result = None
    try:
        ac_result = _acoustid_lookup(path, settings.acoustid_api_key)
    except acoustid.NoBackendError:
        log.error("fpcalc not found — install libchromaprint-tools")
    except acoustid.FingerprintGenerationError:
        log.warning("chromaprint failed for %s", path)
    except acoustid.WebServiceError as exc:
        log.warning("acoustid lookup failed: %s", exc)
    except FileNotFoundError:
        log.error("fpcalc not found — install libchromaprint-tools")
    except Exception:
        log.exception("acoustid unexpected error")

    if ac_result:
        score = float(ac_result.get("score") or 0)
        acoustid_id = ac_result.get("id")
        recordings = _unique_recordings(ac_result)
        if recordings and score >= settings.acoustid_min_score:
            chosen = recordings[0]
            try:
                identity = _identity_from_recording(
                    mb,
                    chosen["id"],
                    duration=duration,
                    bit_depth=bit_depth,
                    sample_rate=sample_rate,
                    acoustid=acoustid_id,
                    acoustid_score=score,
                    source="acoustid",
                )
                identity.candidates = _candidates_from_acoustid(recordings, score)
                identity.confidence = "high" if len(recordings) == 1 else "low"
                if not identity.title:
                    identity.title = str(chosen.get("title") or hints.title)
                if not identity.artists:
                    identity.artists = artist_names_from_acoustid(chosen.get("artists"))
                return identity
            except musicbrainzngs.WebServiceError:
                log.warning("mb recording lookup failed for %s", chosen.get("id"))
        if recordings:
            chosen = recordings[0]
            try:
                identity = _identity_from_recording(
                    mb,
                    chosen["id"],
                    duration=duration,
                    bit_depth=bit_depth,
                    sample_rate=sample_rate,
                    acoustid=acoustid_id,
                    acoustid_score=score,
                    source="acoustid-low",
                )
                identity.candidates = _candidates_from_acoustid(recordings, score)
                identity.confidence = "low"
                return identity
            except musicbrainzngs.WebServiceError:
                log.warning("mb recording lookup failed for %s", chosen.get("id"))

    title = hints.title or file_stem_hints(hints.filename)
    artist = hints.artist or hints.albumartist
    if title:
        try:
            found = mb.search_recordings(title, artist or None)
        except musicbrainzngs.WebServiceError:
            log.warning("mb search failed for %s / %s", title, artist)
            found = []
        if found:
            top = found[0]
            try:
                identity = _identity_from_recording(
                    mb,
                    top["id"],
                    duration=duration,
                    bit_depth=bit_depth,
                    sample_rate=sample_rate,
                    acoustid=None,
                    acoustid_score=None,
                    source="mb-search",
                )
                identity.confidence = "low"
                identity.candidates = [
                    Candidate(
                        mb_recording_id=r.get("id") or "",
                        title=str(r.get("title") or ""),
                        artist=" / ".join(artist_names_from_credit(r.get("artist-credit"))),
                        score=float(r.get("ext:score") or 0) / 100.0 if r.get("ext:score") else None,
                    )
                    for r in found
                    if r.get("id")
                ]
                return identity
            except musicbrainzngs.WebServiceError:
                log.warning("mb recording lookup failed after search")

    identity = _identity_from_hints(hints, duration, bit_depth, sample_rate)
    return identity
