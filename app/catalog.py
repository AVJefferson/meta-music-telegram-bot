from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import PendingReview, TrackRecord


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_get(row: sqlite3.Row, key: str, default=None):
    try:
        if key not in row.keys():  # noqa: SIM118  sqlite3.Row `in` checks values, not columns
            return default
    except Exception:
        return default
    value = row[key]
    return default if value is None and default is not None else value


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
        telegram_file_id=_row_get(row, "telegram_file_id"),
        source_report_json=_row_get(row, "source_report_json"),
        tags_json=_row_get(row, "tags_json"),
        identity_json=_row_get(row, "identity_json"),
        topic_name=_row_get(row, "topic_name"),
        file_name=_row_get(row, "file_name"),
        drive_sidecar_id=_row_get(row, "drive_sidecar_id"),
        drive_log_id=_row_get(row, "drive_log_id"),
        thread_id=_row_get(row, "thread_id"),
    )


def _row_to_pending(row: sqlite3.Row) -> PendingReview:
    return PendingReview(
        id=row["id"],
        phase=row["phase"],
        status=row["status"],
        local_path=row["local_path"],
        sidecar_path=row["sidecar_path"],
        relative_path=row["relative_path"],
        kind=row["kind"],
        original_json=row["original_json"],
        recommended_json=row["recommended_json"],
        working_json=row["working_json"],
        candidates_json=row["candidates_json"],
        identity_json=row["identity_json"],
        source_report_json=row["source_report_json"],
        drive_conflicts_json=row["drive_conflicts_json"],
        drive_root_id=row["drive_root_id"],
        chat_id=row["chat_id"],
        thread_id=row["thread_id"],
        status_message_id=row["status_message_id"],
        topic_name=row["topic_name"],
        file_name=row["file_name"],
        track_id=row["track_id"],
        replace_id=row["replace_id"],
        old_drive_id=row["old_drive_id"],
        source_drive_file_id=row["source_drive_file_id"],
        source_drive_sidecar_id=row["source_drive_sidecar_id"],
        telegram_file_id=row["telegram_file_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
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
                CREATE TABLE IF NOT EXISTS pending_reviews (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  phase TEXT NOT NULL,
                  status TEXT NOT NULL,
                  local_path TEXT NOT NULL,
                  sidecar_path TEXT,
                  relative_path TEXT,
                  kind TEXT NOT NULL,
                  original_json TEXT NOT NULL DEFAULT '{}',
                  recommended_json TEXT NOT NULL DEFAULT '{}',
                  working_json TEXT NOT NULL DEFAULT '{}',
                  candidates_json TEXT NOT NULL DEFAULT '[]',
                  identity_json TEXT NOT NULL DEFAULT '{}',
                  source_report_json TEXT NOT NULL DEFAULT '{}',
                  drive_conflicts_json TEXT NOT NULL DEFAULT '[]',
                  drive_root_id TEXT,
                  chat_id INTEGER NOT NULL,
                  thread_id INTEGER,
                  status_message_id INTEGER NOT NULL DEFAULT 0,
                  topic_name TEXT NOT NULL DEFAULT '',
                  file_name TEXT NOT NULL DEFAULT '',
                  track_id INTEGER,
                  replace_id INTEGER,
                  old_drive_id TEXT,
                  source_drive_file_id TEXT,
                  source_drive_sidecar_id TEXT,
                  telegram_file_id TEXT,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_reviews(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_pending_chat ON pending_reviews(chat_id, status);
                CREATE INDEX IF NOT EXISTS idx_pending_phase ON pending_reviews(status, phase);
                CREATE TABLE IF NOT EXISTS track_messages (
                  chat_id INTEGER NOT NULL,
                  message_id INTEGER NOT NULL,
                  track_id INTEGER NOT NULL,
                  PRIMARY KEY (chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_track_messages_track ON track_messages(track_id);
                """
            )
            pending_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(pending_reviews)").fetchall()
            }
            for name in ("source_drive_file_id", "source_drive_sidecar_id", "telegram_file_id"):
                if name not in pending_columns:
                    self._conn.execute(f"ALTER TABLE pending_reviews ADD COLUMN {name} TEXT")
            track_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(tracks)").fetchall()
            }
            for name in (
                "telegram_file_id",
                "source_report_json",
                "tags_json",
                "identity_json",
                "topic_name",
                "file_name",
                "drive_sidecar_id",
                "drive_log_id",
            ):
                if name not in track_columns:
                    self._conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} TEXT")
            if "thread_id" not in track_columns:
                self._conn.execute("ALTER TABLE tracks ADD COLUMN thread_id INTEGER")
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

    def list_topics(self) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT thread_id, name FROM topics ORDER BY name COLLATE NOCASE"
            ).fetchall()
        topics = [(int(row["thread_id"]), str(row["name"])) for row in rows]
        if not any(thread_id == 1 for thread_id, _name in topics):
            topics.insert(0, (1, "General"))
        return topics

    def find_library_by_mbid(self, mb_recording_id: str) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE mb_recording_id=? AND kind='library' "
                "AND status IN ('uploaded', 'pending', 'failed', 'awaiting_drive') "
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
        status: str = "pending",
        telegram_file_id: str | None = None,
        source_report_json: str | None = None,
        tags_json: str | None = None,
        identity_json: str | None = None,
        topic_name: str | None = None,
        file_name: str | None = None,
        drive_file_id: str | None = None,
        drive_url: str | None = None,
        drive_sidecar_id: str | None = None,
        drive_log_id: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tracks (
                    mb_recording_id, acoustid, kind, local_path, sidecar_path,
                    relative_path, status, bit_depth, sample_rate, title, artist, album,
                    created_at, telegram_file_id, source_report_json, tags_json,
                    identity_json, topic_name, file_name, drive_file_id, drive_url,
                    drive_sidecar_id, drive_log_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mb_recording_id,
                    acoustid,
                    kind,
                    local_path,
                    sidecar_path,
                    relative_path,
                    status,
                    bit_depth,
                    sample_rate,
                    title,
                    artist,
                    album,
                    _utc_now(),
                    telegram_file_id,
                    source_report_json,
                    tags_json,
                    identity_json,
                    topic_name,
                    file_name,
                    drive_file_id,
                    drive_url,
                    drive_sidecar_id,
                    drive_log_id,
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

    def mark_skipped(self, track_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tracks SET status='skipped', error=NULL WHERE id=?",
                (track_id,),
            )
            self._conn.commit()

    def find_uploaded_by_relative(self, relative_path: str) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE relative_path=? AND status='uploaded' "
                "ORDER BY id DESC LIMIT 1",
                (relative_path,),
            ).fetchone()
        return _row_to_track(row) if row else None

    def update_track_paths(
        self,
        track_id: int,
        *,
        local_path: str,
        sidecar_path: str | None,
        relative_path: str,
        title: str | None,
        artist: str | None,
        album: str | None,
        status: str | None = None,
    ) -> None:
        with self._lock:
            if status:
                self._conn.execute(
                    """
                    UPDATE tracks SET local_path=?, sidecar_path=?, relative_path=?,
                        title=?, artist=?, album=?, status=? WHERE id=?
                    """,
                    (local_path, sidecar_path, relative_path, title, artist, album, status, track_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE tracks SET local_path=?, sidecar_path=?, relative_path=?,
                        title=?, artist=?, album=? WHERE id=?
                    """,
                    (local_path, sidecar_path, relative_path, title, artist, album, track_id),
                )
            self._conn.commit()

    def insert_pending_review(
        self,
        *,
        phase: str,
        status: str = "waiting",
        local_path: str,
        sidecar_path: str | None = None,
        relative_path: str | None,
        kind: str,
        original_json: str,
        recommended_json: str,
        working_json: str,
        candidates_json: str,
        identity_json: str,
        source_report_json: str,
        drive_conflicts_json: str = "[]",
        drive_root_id: str | None = None,
        chat_id: int,
        thread_id: int | None,
        status_message_id: int,
        topic_name: str,
        file_name: str,
        track_id: int | None = None,
        replace_id: int | None = None,
        old_drive_id: str | None = None,
        source_drive_file_id: str | None = None,
        source_drive_sidecar_id: str | None = None,
        telegram_file_id: str | None = None,
        expires_at: str,
    ) -> int:
        with self._lock:
            if chat_id > 0 and track_id is None:
                active = self._conn.execute(
                    "SELECT id FROM pending_reviews WHERE chat_id=? AND status IN "
                    "('waiting', 'queued', 'processing', 'uploading', 'expiring') LIMIT 1",
                    (chat_id,),
                ).fetchone()
                if active:
                    raise RuntimeError("private chat already has a waiting session")
            cur = self._conn.execute(
                """
                INSERT INTO pending_reviews (
                    phase, status, local_path, sidecar_path, relative_path, kind,
                    original_json, recommended_json, working_json, candidates_json,
                    identity_json, source_report_json, drive_conflicts_json, drive_root_id,
                    chat_id, thread_id, status_message_id, topic_name, file_name,
                    track_id, replace_id, old_drive_id, source_drive_file_id,
                    source_drive_sidecar_id, telegram_file_id, created_at, expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    phase,
                    status,
                    local_path,
                    sidecar_path,
                    relative_path,
                    kind,
                    original_json,
                    recommended_json,
                    working_json,
                    candidates_json,
                    identity_json,
                    source_report_json,
                    drive_conflicts_json,
                    drive_root_id,
                    chat_id,
                    thread_id,
                    status_message_id,
                    topic_name,
                    file_name,
                    track_id,
                    replace_id,
                    old_drive_id,
                    source_drive_file_id,
                    source_drive_sidecar_id,
                    telegram_file_id,
                    _utc_now(),
                    expires_at,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_pending_review(self, pending_id: int) -> PendingReview | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_reviews WHERE id=?", (pending_id,)
            ).fetchone()
        return _row_to_pending(row) if row else None

    def update_pending_review(
        self,
        pending_id: int,
        **fields: object,
    ) -> None:
        if not fields:
            return
        allowed = {
            "phase",
            "status",
            "local_path",
            "sidecar_path",
            "relative_path",
            "kind",
            "original_json",
            "recommended_json",
            "working_json",
            "candidates_json",
            "identity_json",
            "source_report_json",
            "drive_conflicts_json",
            "drive_root_id",
            "status_message_id",
            "track_id",
            "replace_id",
            "old_drive_id",
            "source_drive_file_id",
            "source_drive_sidecar_id",
            "telegram_file_id",
            "topic_name",
            "thread_id",
            "file_name",
            "expires_at",
        }
        cols = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown pending field {key}")
            cols.append(f"{key}=?")
            values.append(value)
        values.append(pending_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE pending_reviews SET {', '.join(cols)} WHERE id=?",
                values,
            )
            self._conn.commit()

    def list_waiting_by_phase(self, phase: str) -> list[PendingReview]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending_reviews WHERE status='waiting' AND phase=? ORDER BY id",
                (phase,),
            ).fetchall()
        return [_row_to_pending(row) for row in rows]

    def list_pending_by_phase(self, phase: str, *statuses: str) -> list[PendingReview]:
        with self._lock:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = self._conn.execute(
                    f"SELECT * FROM pending_reviews WHERE phase=? AND status IN "
                    f"({placeholders}) ORDER BY id",
                    (phase, *statuses),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM pending_reviews WHERE phase=? ORDER BY id",
                    (phase,),
                ).fetchall()
        return [_row_to_pending(row) for row in rows]

    def get_waiting_for_chat(self, chat_id: int) -> PendingReview | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_reviews WHERE chat_id=? AND status='waiting' "
                "ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return _row_to_pending(row) if row else None

    def get_active_for_chat(self, chat_id: int) -> PendingReview | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_reviews WHERE chat_id=? AND status IN "
                "('waiting', 'queued', 'processing', 'uploading', 'expiring') "
                "ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return _row_to_pending(row) if row else None

    def claim_pending(self, pending_id: int, target_status: str) -> bool:
        return self.transition_pending(pending_id, "waiting", target_status)

    def transition_pending(
        self, pending_id: int, expected_status: str, target_status: str
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE pending_reviews SET status=? WHERE id=? AND status=?",
                (target_status, pending_id, expected_status),
            )
            self._conn.commit()
        return bool(cur.rowcount)

    def list_pending_by_status(self, *statuses: str) -> list[PendingReview]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM pending_reviews WHERE status IN ({placeholders}) ORDER BY id",
                statuses,
            ).fetchall()
        return [_row_to_pending(row) for row in rows]

    def claim_expired_pending(self) -> list[PendingReview]:
        now = _utc_now()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM pending_reviews
                WHERE status='waiting' AND expires_at<=?
                  AND phase NOT IN ('react_edit', 'react_confirm')
                ORDER BY id
                """,
                (now,),
            ).fetchall()
            claimed: list[PendingReview] = []
            for row in rows:
                cur = self._conn.execute(
                    "UPDATE pending_reviews SET status='expiring' WHERE id=? AND status='waiting'",
                    (row["id"],),
                )
                if cur.rowcount:
                    claimed.append(_row_to_pending(row))
            self._conn.commit()
        return claimed

    def prune_finished_pending(self, keep_days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(
            timespec="seconds"
        )
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM pending_reviews WHERE created_at < ? AND status IN "
                "('done', 'cancelled', 'expired', 'skipped')",
                (cutoff,),
            )
            self._conn.commit()
        return cur.rowcount or 0

    def get_track(self, track_id: int) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        return _row_to_track(row) if row else None

    def find_by_drive_file_id(self, drive_file_id: str) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE drive_file_id=? ORDER BY id DESC LIMIT 1",
                (drive_file_id,),
            ).fetchone()
        return _row_to_track(row) if row else None

    def list_review_tracks(self) -> list[TrackRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE kind='review' "
                "AND status IN ('uploaded', 'pending', 'failed') "
                "ORDER BY id DESC"
            ).fetchall()
        return [_row_to_track(row) for row in rows]

    def update_track(self, track_id: int, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "kind",
            "local_path",
            "sidecar_path",
            "drive_file_id",
            "drive_url",
            "relative_path",
            "status",
            "bit_depth",
            "sample_rate",
            "title",
            "artist",
            "album",
            "error",
            "telegram_file_id",
            "source_report_json",
            "tags_json",
            "identity_json",
            "topic_name",
            "file_name",
            "drive_sidecar_id",
            "drive_log_id",
            "thread_id",
            "mb_recording_id",
            "acoustid",
        }
        cols = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown track field {key}")
            cols.append(f"{key}=?")
            values.append(value)
        values.append(track_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tracks SET {', '.join(cols)} WHERE id=?",
                values,
            )
            self._conn.commit()

    def bind_track_message(self, track_id: int, chat_id: int, message_id: int) -> None:
        if not message_id:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO track_messages(chat_id, message_id, track_id) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id, message_id) DO UPDATE SET track_id=excluded.track_id",
                (chat_id, message_id, track_id),
            )
            self._conn.commit()

    def get_track_by_message(self, chat_id: int, message_id: int) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT t.* FROM tracks t "
                "JOIN track_messages m ON m.track_id = t.id "
                "WHERE m.chat_id=? AND m.message_id=?",
                (chat_id, message_id),
            ).fetchone()
        if row:
            return _row_to_track(row)
        pending = self.get_pending_by_message(chat_id, message_id)
        if pending and pending.track_id:
            return self.get_track(pending.track_id)
        return None

    def get_pending_by_message(self, chat_id: int, message_id: int) -> PendingReview | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_reviews WHERE chat_id=? AND status_message_id=? "
                "AND status IN ('waiting', 'processing', 'queued', 'uploading', 'expiring') "
                "ORDER BY id DESC LIMIT 1",
                (chat_id, message_id),
            ).fetchone()
        return _row_to_pending(row) if row else None

    def get_waiting_for_track(self, track_id: int) -> PendingReview | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_reviews WHERE track_id=? AND status='waiting' "
                "ORDER BY id DESC LIMIT 1",
                (track_id,),
            ).fetchone()
        return _row_to_pending(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

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
