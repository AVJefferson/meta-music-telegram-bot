from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class Enrichment:
    cover: bytes | None = None
    cover_mime: str | None = None
    lyrics: str | None = None
    genre: str = ""
    instrumental: bool = False


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
