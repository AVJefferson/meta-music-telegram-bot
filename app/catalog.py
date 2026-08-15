from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.models import TrackRecord


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_track(row: sqlite3.Row) -> TrackRecord:
    return TrackRecord(
        id=row["id"],
        mb_recording_id=row["mb_recording_id"],
        acoustid=row["acoustid"],
        kind=row["kind"],
        local_path=row["local_path"],
        sidecar_path=row["sidecar_path"],
        drive_file_id=row["drive_file_id"],
        drive_url=row["drive_url"],
        relative_path=row["relative_path"],
        status=row["status"],
        bit_depth=row["bit_depth"],
        sample_rate=row["sample_rate"],
        title=row["title"],
        artist=row["artist"],
        album=row["album"],
        error=row["error"],
        created_at=row["created_at"],
        uploaded_at=row["uploaded_at"],
    )


class Catalog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  mb_recording_id TEXT,
                  acoustid TEXT,
                  kind TEXT NOT NULL,
                  local_path TEXT,
                  sidecar_path TEXT,
                  drive_file_id TEXT,
                  drive_url TEXT,
                  relative_path TEXT,
                  status TEXT NOT NULL,
                  bit_depth INTEGER,
                  sample_rate INTEGER,
                  title TEXT,
                  artist TEXT,
                  album TEXT,
                  error TEXT,
                  created_at TEXT NOT NULL,
                  uploaded_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tracks_mb ON tracks(mb_recording_id);
                CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
                CREATE TABLE IF NOT EXISTS topics (
                  thread_id INTEGER PRIMARY KEY,
                  name TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def upsert_topic(self, thread_id: int, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO topics(thread_id, name) VALUES (?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET name=excluded.name",
                (thread_id, name),
            )
            self._conn.commit()

    def get_topic(self, thread_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM topics WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return row["name"] if row else None

    def find_library_by_mbid(self, mb_recording_id: str) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE mb_recording_id=? AND kind='library' "
                "AND status IN ('uploaded', 'pending', 'failed') "
                "ORDER BY (status='uploaded') DESC, id DESC LIMIT 1",
                (mb_recording_id,),
            ).fetchone()
        return _row_to_track(row) if row else None

    def insert_pending(
        self,
        *,
        kind: str,
        mb_recording_id: str | None,
        acoustid: str | None,
        local_path: str,
        sidecar_path: str | None,
        relative_path: str,
        bit_depth: int | None,
        sample_rate: int | None,
        title: str | None,
        artist: str | None,
        album: str | None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tracks (
                    mb_recording_id, acoustid, kind, local_path, sidecar_path,
                    relative_path, status, bit_depth, sample_rate, title, artist, album,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    mb_recording_id,
                    acoustid,
                    kind,
                    local_path,
                    sidecar_path,
                    relative_path,
                    bit_depth,
                    sample_rate,
                    title,
                    artist,
                    album,
                    _utc_now(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def mark_uploaded(self, track_id: int, drive_file_id: str, drive_url: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tracks SET status='uploaded', drive_file_id=?, drive_url=?, "
                "uploaded_at=?, error=NULL WHERE id=?",
                (drive_file_id, drive_url, _utc_now(), track_id),
            )
            self._conn.commit()

    def mark_failed(self, track_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tracks SET status='failed', error=? WHERE id=?",
                (error[:2000], track_id),
            )
            self._conn.commit()

    def list_failed(self) -> list[TrackRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE status='failed' ORDER BY id"
            ).fetchall()
        return [_row_to_track(r) for r in rows]

    def list_uploaded_with_local(self) -> list[TrackRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE status='uploaded' AND local_path IS NOT NULL "
                "ORDER BY id"
            ).fetchall()
        return [_row_to_track(r) for r in rows]

    def clear_local_paths(self, track_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tracks SET local_path=NULL, sidecar_path=NULL WHERE id=?",
                (track_id,),
            )
            self._conn.commit()

    def update_quality_and_local(
        self,
        track_id: int,
        *,
        local_path: str,
        sidecar_path: str | None,
        relative_path: str,
        bit_depth: int | None,
        sample_rate: int | None,
        title: str | None,
        artist: str | None,
        album: str | None,
        acoustid: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE tracks SET local_path=?, sidecar_path=?, relative_path=?,
                    bit_depth=?, sample_rate=?, title=?, artist=?, album=?, acoustid=?,
                    status='pending', error=NULL
                WHERE id=?
                """,
                (
                    local_path,
                    sidecar_path,
                    relative_path,
                    bit_depth,
                    sample_rate,
                    title,
                    artist,
                    album,
                    acoustid,
                    track_id,
                ),
            )
            self._conn.commit()
