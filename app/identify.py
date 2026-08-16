from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import acoustid
import musicbrainzngs

from app.config import Settings
from app.models import Candidate, Identity, TagHints
from app.songlog import mb_snapshot
from app.tags import read_audio_metrics
from app.util import (
    artist_name_set,
    file_stem_hints,
    format_artist_list,
    normalize_match_text,
    parse_mb_user_agent,
    parse_track_number,
    split_artist_field,
    year_from_date,
)

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
    """Rank a release before deciding which one to fetch in full.

    The recording endpoint's release list carries no release-group ("release-groups"
    is not a valid include for recordings), so the type bonus only applies to the
    full releases fetched later. Thin entries are ranked on status and date.
    """
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

    mb_duration = duration
    raw_length = recording.get("length")
    if raw_length:
        try:
            mb_duration = int(raw_length) / 1000.0
        except (TypeError, ValueError):
            mb_duration = duration

    identity = Identity(
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
    identity.source_report["musicbrainz"] = mb_snapshot(identity)
    identity.source_report["mb_duration"] = mb_duration
    return identity


def _fpcalc_stderr(path: Path) -> str:
    fpcalc = os.environ.get("FPCALC", "fpcalc")
    try:
        proc = subprocess.run(
            [fpcalc, "-length", "120", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return "fpcalc not found"
    except subprocess.TimeoutExpired:
        return "fpcalc timed out"
    err = (proc.stderr or "").strip().replace("\n", " | ")[:500]
    has_fp = "FINGERPRINT=" in (proc.stdout or "")
    return f"exit={proc.returncode} fingerprint={'yes' if has_fp else 'no'} stderr={err or '(empty)'}"


def _acoustid_lookup(path: Path, api_key: str) -> dict | None:
    duration, fingerprint = acoustid.fingerprint_file(str(path), force_fpcalc=True)
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


def identity_from_mbid(
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
    return _identity_from_recording(
        mb,
        mbid,
        duration=duration,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        acoustid=acoustid,
        acoustid_score=acoustid_score,
        source=source,
    )


def _rec_duration_seconds(rec: dict) -> float | None:
    raw = rec.get("duration")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 10_000:
        value /= 1000.0
    return value


def _cluster_key(rec: dict) -> str:
    title = normalize_match_text(str(rec.get("title") or ""))
    artists = artist_names_from_acoustid(rec.get("artists"))
    primary = normalize_match_text(artists[0] if artists else "")
    return f"{title}|{primary}"


def _cluster_recordings(recordings: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for rec in recordings:
        key = _cluster_key(rec) or rec.get("id") or str(len(order))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(rec)
    return [groups[key] for key in order]


def _best_in_cluster(cluster: list[dict], file_duration: float) -> dict:
    def sort_key(rec: dict) -> tuple[float, int]:
        rec_dur = _rec_duration_seconds(rec)
        if rec_dur is None or file_duration <= 0:
            return (9999.0, 0)
        return (abs(file_duration - rec_dur), 0)

    return sorted(cluster, key=sort_key)[0]


def _duration_status(file_duration: float, rec_duration: float | None) -> str:
    if rec_duration is None or file_duration <= 0:
        return "unknown"
    delta = abs(file_duration - rec_duration)
    if delta > 8:
        return "mismatch"
    if delta <= 3 or delta <= 0.05 * file_duration:
        return "match"
    return "close"


def _text_agrees(left: str, right: str) -> bool:
    a = normalize_match_text(left)
    b = normalize_match_text(right)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _filename_tag_agrees(hints: TagHints, title: str, artists: list[str]) -> bool:
    artist = artists[0] if artists else ""
    stem = file_stem_hints(hints.filename)
    title_ok = False
    if hints.title and _text_agrees(hints.title, title):
        title_ok = True
    if stem and (_text_agrees(stem, title) or (title and title.casefold() in stem.casefold())):
        title_ok = True
    artist_ok = True
    if hints.artist and artist:
        mb_names = {normalize_match_text(name) for name in artists}
        artist_ok = (
            _text_agrees(hints.artist, artist)
            or artist.casefold() in hints.artist.casefold()
            or bool(artist_name_set(hints.artist) & mb_names)
        )
    if stem and artist and not artist_ok:
        artist_ok = artist.casefold() in stem.casefold()
    return title_ok and artist_ok


def _candidates_from_clusters(clusters: list[list[dict]], score: float, file_duration: float) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    ranked = sorted(clusters, key=len, reverse=True)
    for cluster in ranked:
        rec = _best_in_cluster(cluster, file_duration)
        mbid = rec.get("id") or ""
        if not mbid or mbid in seen:
            continue
        seen.add(mbid)
        out.append(
            Candidate(
                mb_recording_id=mbid,
                title=str(rec.get("title") or ""),
                artist=" / ".join(artist_names_from_acoustid(rec.get("artists"))),
                score=score,
            )
        )
        if len(out) >= 5:
            break
    return out


def _acoustid_report(ac_result: dict, recordings: list[dict], clusters: list[list[dict]], score: float) -> dict:
    rows = []
    for rec in recordings:
        rows.append(
            {
                "mbid": rec.get("id") or "",
                "title": rec.get("title") or "",
                "artist": format_artist_list(artist_names_from_acoustid(rec.get("artists"))),
                "duration": _rec_duration_seconds(rec),
            }
        )
    return {
        "score": score,
        "id": ac_result.get("id") or "",
        "recordings": rows,
        "clusters": len(clusters),
    }


def _score_confidence(
    *,
    clusters: list[list[dict]],
    chosen: dict,
    file_duration: float,
    hints: TagHints,
    mb_duration: float | None,
    score: float,
    min_score: float,
) -> tuple[str, str]:
    if score < min_score:
        return "low", "acoustid score below threshold"
    rec_duration = mb_duration if mb_duration is not None else _rec_duration_seconds(chosen)
    status = _duration_status(file_duration, rec_duration)
    delta = None
    if rec_duration is not None and file_duration > 0:
        delta = abs(file_duration - rec_duration)
    agrees = _filename_tag_agrees(
        hints,
        str(chosen.get("title") or ""),
        artist_names_from_acoustid(chosen.get("artists")),
    )
    cluster_count = len(clusters)
    log.debug(
        "confidence inputs score=%.3f clusters=%s duration_delta=%s status=%s agrees=%s mbid=%s",
        score,
        cluster_count,
        f"{delta:.2f}" if delta is not None else "unknown",
        status,
        agrees,
        chosen.get("id"),
    )
    if cluster_count > 1:
        return "low", "acoustid multi-cluster"
    if status == "mismatch":
        return "low", "duration mismatch"
    if status == "close" and not agrees:
        return "low", "duration close, filename disagree"
    return "high", "acoustid single-cluster"


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
        confidence_reason="existing-tags",
    )


def _attach_report(
    identity: Identity,
    hints: TagHints,
    acoustid_report: dict | None = None,
    *,
    bitrate_kbps: int | None = None,
) -> Identity:
    report = dict(identity.source_report or {})
    if acoustid_report is not None:
        report["acoustid"] = acoustid_report
    report["file"] = hints.filename
    report["filename_stem"] = Path(hints.filename).stem
    report["duration"] = identity.duration
    report["bit_depth"] = identity.bit_depth
    report["sample_rate"] = identity.sample_rate
    if bitrate_kbps:
        report["bitrate"] = bitrate_kbps
    identity.source_report = report
    return identity


def identify_file(path: Path, hints: TagHints, settings: Settings, mb: MBClient) -> Identity:
    metrics = read_audio_metrics(path)
    duration, bit_depth, sample_rate = metrics.duration, metrics.bit_depth, metrics.sample_rate
    if duration <= 0:
        duration = 0.0

    ac_result = None
    try:
        ac_result = _acoustid_lookup(path, settings.acoustid_api_key)
    except acoustid.NoBackendError:
        log.error("fpcalc not found — install libchromaprint-tools")
    except acoustid.FingerprintGenerationError as exc:
        log.warning("chromaprint failed for %s: %s (%s)", path, exc, _fpcalc_stderr(path))
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
        clusters = _cluster_recordings(recordings) if recordings else []
        ac_report = _acoustid_report(ac_result, recordings, clusters, score)
        if recordings:
            ranked = sorted(clusters, key=len, reverse=True)
            chosen = _best_in_cluster(ranked[0], duration)
            source = "acoustid" if score >= settings.acoustid_min_score else "acoustid-low"
            try:
                identity = _identity_from_recording(
                    mb,
                    chosen["id"],
                    duration=duration,
                    bit_depth=bit_depth,
                    sample_rate=sample_rate,
                    acoustid=acoustid_id,
                    acoustid_score=score,
                    source=source,
                )
                identity.candidates = _candidates_from_clusters(clusters, score, duration)
                chosen_for_score = {
                    "id": chosen.get("id"),
                    "title": identity.title or chosen.get("title"),
                    "artists": chosen.get("artists") or [{"name": n} for n in identity.artists],
                }
                mb_duration = identity.source_report.get("mb_duration")
                try:
                    mb_duration_f = float(mb_duration) if mb_duration is not None else None
                except (TypeError, ValueError):
                    mb_duration_f = None
                identity.confidence, identity.confidence_reason = _score_confidence(
                    clusters=clusters,
                    chosen=chosen_for_score,
                    file_duration=duration,
                    hints=hints,
                    mb_duration=mb_duration_f,
                    score=score,
                    min_score=settings.acoustid_min_score,
                )
                if source == "acoustid-low":
                    identity.confidence = "low"
                    identity.confidence_reason = "acoustid score below threshold"
                if not identity.title:
                    identity.title = str(chosen.get("title") or hints.title)
                if not identity.artists:
                    identity.artists = artist_names_from_acoustid(chosen.get("artists"))
                _attach_report(identity, hints, ac_report, bitrate_kbps=metrics.bitrate_kbps)
                log.debug(
                    "identified mbid=%s confidence=%s reason=%s score=%.3f clusters=%s",
                    identity.mb_recording_id,
                    identity.confidence,
                    identity.confidence_reason,
                    score,
                    len(clusters),
                )
                log.info(
                    "identified %s confidence=%s reason=%s",
                    hints.filename,
                    identity.confidence,
                    identity.confidence_reason,
                )
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
                identity.confidence_reason = "mb-search"
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
                _attach_report(identity, hints, bitrate_kbps=metrics.bitrate_kbps)
                log.info("identified %s confidence=low reason=mb-search", hints.filename)
                return identity
            except musicbrainzngs.WebServiceError:
                log.warning("mb recording lookup failed after search")

    identity = _identity_from_hints(hints, duration, bit_depth, sample_rate)
    _attach_report(identity, hints, bitrate_kbps=metrics.bitrate_kbps)
    log.info("identified %s confidence=low reason=existing-tags", hints.filename)
    return identity
