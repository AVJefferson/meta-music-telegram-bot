from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal


Confidence = Literal["high", "low"]
Kind = Literal["library", "review"]


@dataclass
class TagHints:
    title: str = ""
    album: str = ""
    artist: str = ""
    albumartist: str = ""
    composer: str = ""
    genre: str = ""
    date: str = ""
    tracknumber: str = ""
    discnumber: str = ""
    filename: str = ""


@dataclass
class Candidate:
    mb_recording_id: str
    title: str = ""
    artist: str = ""
    score: float | None = None


@dataclass
class Identity:
    confidence: Confidence
    title: str = ""
    album: str = ""
    artists: list[str] = field(default_factory=list)
    album_artists: list[str] = field(default_factory=list)
    composers: list[str] = field(default_factory=list)
    year: str = ""
    tracknumber: str = ""
    discnumber: str = ""
    duration: float = 0.0
    bit_depth: int | None = None
    sample_rate: int | None = None
    raw_genre_tags: list[str] = field(default_factory=list)
    acoustid: str | None = None
    acoustid_score: float | None = None
    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    mb_release_group_id: str | None = None
    source: str = "unknown"
    candidates: list[Candidate] = field(default_factory=list)
    confidence_reason: str = ""
    source_report: dict[str, Any] = field(default_factory=dict)


@dataclass
class Enrichment:
    cover: bytes | None = None
    cover_mime: str | None = None
    lyrics: str | None = None
    genre: str = ""
    instrumental: bool = False
    cover_source: str = "none"
    caa_release: str | None = None
    itunes_report: dict[str, Any] = field(default_factory=dict)
    lastfm_tags: list[str] = field(default_factory=list)


@dataclass
class TagSet:
    title: str = ""
    album: str = ""
    artist: str = ""
    albumartist: str = ""
    composer: str = ""
    genre: str = ""
    date: str = ""
    tracknumber: str = ""
    discnumber: str = ""
    lyrics: str = ""


@dataclass
class Job:
    chat_id: int
    thread_id: int | None
    topic_name: str
    file_id: str
    file_name: str
    status_message_id: int
    local_path: str | None = None
    private: bool = False
    source_pending_id: int | None = None


@dataclass
class Ctx:
    settings: Any
    catalog: Any
    drive: Any
    http: Any
    genre: Any
    bot: Any
    mb: Any


@dataclass
class TrackRecord:
    id: int
    mb_recording_id: str | None
    acoustid: str | None
    kind: str
    local_path: str | None
    sidecar_path: str | None
    drive_file_id: str | None
    drive_url: str | None
    relative_path: str | None
    status: str
    bit_depth: int | None
    sample_rate: int | None
    title: str | None
    artist: str | None
    album: str | None
    error: str | None
    created_at: str
    uploaded_at: str | None

    @property
    def local(self) -> Path | None:
        return Path(self.local_path) if self.local_path else None


@dataclass
class PendingReview:
    id: int
    phase: str
    status: str
    local_path: str
    sidecar_path: str | None
    relative_path: str | None
    kind: str
    original_json: str
    recommended_json: str
    working_json: str
    candidates_json: str
    identity_json: str
    source_report_json: str
    drive_conflicts_json: str
    drive_root_id: str | None
    chat_id: int
    thread_id: int | None
    status_message_id: int
    topic_name: str
    file_name: str
    track_id: int | None
    replace_id: int | None
    old_drive_id: str | None
    source_drive_file_id: str | None
    source_drive_sidecar_id: str | None
    created_at: str
    expires_at: str


def identity_from_dict(data: dict[str, Any]) -> Identity:
    raw = dict(data)
    candidates = []
    for item in raw.get("candidates") or []:
        if isinstance(item, Candidate):
            candidates.append(item)
            continue
        allowed = {f.name for f in fields(Candidate)}
        candidates.append(Candidate(**{k: v for k, v in item.items() if k in allowed}))
    raw["candidates"] = candidates
    allowed = {f.name for f in fields(Identity)}
    return Identity(**{k: v for k, v in raw.items() if k in allowed})


def tagset_from_dict(data: dict[str, Any] | None) -> TagSet:
    raw = data or {}
    allowed = {f.name for f in fields(TagSet)}
    return TagSet(**{k: (v if isinstance(v, str) else str(v or "")) for k, v in raw.items() if k in allowed})
