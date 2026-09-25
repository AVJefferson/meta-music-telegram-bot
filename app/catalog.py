from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import (
    ChatRecord,
    PendingReview,
    SuggestSession,
    TrackRecord,
    UserRecord,
    user_display_name,
)

GENERAL_TOPIC_THREAD_ID = 1
GENERAL_TOPIC_NAME = "General"


def is_general_topic(
    thread_id: int | None = None,
    topic_name: str | None = None,
    *,
    is_topic_message: bool | None = None,
) -> bool:
    """True for Telegram forum General (thread 1, non-topic messages, or that name)."""
    if is_topic_message is False:
        return True
    if thread_id == GENERAL_TOPIC_THREAD_ID:
        return True
    return (topic_name or "").strip().casefold() == GENERAL_TOPIC_NAME.casefold()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_UNSET = object()


def _profile_text(value: object, *, username: bool = False) -> str | None:
    if value is None or value is _UNSET:
        return None
    text = str(value).strip()
    if username:
        text = text.lstrip("@")
    return text or None


def chat_is_group(chat: ChatRecord) -> bool:
    return chat.type in {"group", "supergroup"} or (chat.chat_id < 0 and chat.type != "channel")


def chat_is_channel(chat: ChatRecord) -> bool:
    return chat.type == "channel"


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
        source_chat_id=_row_get(row, "source_chat_id"),
        source_message_id=_row_get(row, "source_message_id"),
        user_id=int(_row_get(row, "user_id") or 0),
        last_editor_user_id=_row_get(row, "last_editor_user_id"),
        audio_sha256=_row_get(row, "audio_sha256"),
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
        source_message_id=_row_get(row, "source_message_id"),
        user_id=int(_row_get(row, "user_id") or 0),
        public_message_id=_row_get(row, "public_message_id"),
    )


def _row_to_suggest(row: sqlite3.Row) -> SuggestSession:
    return SuggestSession(
        id=row["id"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        thread_id=row["thread_id"],
        query=row["query"],
        results_json=row["results_json"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


class Catalog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(path)
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
                CREATE TABLE IF NOT EXISTS suggest_sessions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  chat_id INTEGER NOT NULL,
                  thread_id INTEGER,
                  query TEXT NOT NULL DEFAULT '',
                  results_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_suggest_sessions_expires ON suggest_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS suggest_shown (
                  user_id INTEGER NOT NULL,
                  owned_key TEXT NOT NULL,
                  shown_at TEXT NOT NULL,
                  PRIMARY KEY (user_id, owned_key)
                );
                CREATE INDEX IF NOT EXISTS idx_suggest_shown_user ON suggest_shown(user_id, shown_at);
                CREATE TABLE IF NOT EXISTS lastfm_cache (
                  cache_key TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_tag_index (
                  id INTEGER PRIMARY KEY CHECK (id = 1),
                  payload_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  drive_file_id TEXT,
                  payload_sha TEXT
                );
                CREATE TABLE IF NOT EXISTS user_library_index (
                  user_id INTEGER PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  drive_file_id TEXT,
                  payload_sha TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                  telegram_user_id INTEGER PRIMARY KEY,
                  first_seen_at TEXT NOT NULL,
                  last_active_at TEXT NOT NULL,
                  songs_edited INTEGER NOT NULL DEFAULT 0,
                  google_refresh_token TEXT,
                  google_email TEXT,
                  gdrive_folder_id TEXT,
                  gdrive_review_folder_id TEXT,
                  settings_json TEXT NOT NULL DEFAULT '{}',
                  username TEXT,
                  first_name TEXT,
                  last_name TEXT
                );
                CREATE TABLE IF NOT EXISTS chats (
                  chat_id INTEGER PRIMARY KEY,
                  type TEXT NOT NULL DEFAULT '',
                  title TEXT NOT NULL DEFAULT '',
                  username TEXT,
                  active INTEGER NOT NULL DEFAULT 1,
                  added_at TEXT NOT NULL,
                  last_active_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blacklist_users (
                  telegram_user_id INTEGER PRIMARY KEY,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blacklist_chats (
                  chat_id INTEGER PRIMARY KEY,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_tickets (
                  token TEXT PRIMARY KEY,
                  telegram_user_id INTEGER NOT NULL,
                  expires_at TEXT NOT NULL,
                  used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_states (
                  state TEXT PRIMARY KEY,
                  telegram_user_id INTEGER NOT NULL,
                  code_verifier TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS cached_files (
                  sha256 TEXT PRIMARY KEY,
                  path TEXT NOT NULL,
                  size INTEGER,
                  created_at TEXT NOT NULL,
                  last_used_at TEXT NOT NULL,
                  refcount INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS hifi_picks (
                  pick_id TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  session_id TEXT NOT NULL,
                  hifi_callback TEXT NOT NULL,
                  label TEXT NOT NULL DEFAULT '',
                  expires_at TEXT NOT NULL
                );
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
            for name in ("source_chat_id", "source_message_id"):
                if name not in track_columns:
                    self._conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} INTEGER")
            if "source_message_id" not in pending_columns:
                self._conn.execute("ALTER TABLE pending_reviews ADD COLUMN source_message_id INTEGER")
            if "user_id" not in pending_columns:
                self._conn.execute(
                    "ALTER TABLE pending_reviews ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0"
                )
            if "public_message_id" not in pending_columns:
                self._conn.execute("ALTER TABLE pending_reviews ADD COLUMN public_message_id INTEGER")
            if "user_id" not in track_columns:
                self._conn.execute("ALTER TABLE tracks ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
            if "last_editor_user_id" not in track_columns:
                self._conn.execute("ALTER TABLE tracks ADD COLUMN last_editor_user_id INTEGER")
            if "audio_sha256" not in track_columns:
                self._conn.execute("ALTER TABLE tracks ADD COLUMN audio_sha256 TEXT")
            index_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(library_tag_index)").fetchall()
            }
            for name in ("drive_file_id", "payload_sha"):
                if name not in index_columns:
                    self._conn.execute(f"ALTER TABLE library_tag_index ADD COLUMN {name} TEXT")
            user_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(users)").fetchall()
            }
            for name in ("username", "first_name", "last_name"):
                if name not in user_columns:
                    self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT")
            chat_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(chats)").fetchall()
            }
            if "username" not in chat_columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN username TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracks_user ON tracks(user_id, kind, status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracks_user_mb ON tracks(user_id, mb_recording_id)"
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

    def list_topics(self) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT thread_id, name FROM topics ORDER BY name COLLATE NOCASE"
            ).fetchall()
        topics = [(int(row["thread_id"]), str(row["name"])) for row in rows]
        if not any(thread_id == GENERAL_TOPIC_THREAD_ID for thread_id, _name in topics):
            topics.insert(0, (GENERAL_TOPIC_THREAD_ID, GENERAL_TOPIC_NAME))
        return topics

    def list_library_topics(self) -> list[tuple[int, str]]:
        return [
            (thread_id, name)
            for thread_id, name in self.list_topics()
            if not is_general_topic(thread_id, name)
        ]

    def find_library_by_mbid(
        self, mb_recording_id: str, user_id: int | None = None
    ) -> TrackRecord | None:
        with self._lock:
            if user_id is None:
                row = self._conn.execute(
                    "SELECT * FROM tracks WHERE mb_recording_id=? AND kind='library' "
                    "AND status IN ('uploaded', 'pending', 'failed', 'awaiting_drive') "
                    "ORDER BY (status='uploaded') DESC, id DESC LIMIT 1",
                    (mb_recording_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM tracks WHERE mb_recording_id=? AND kind='library' AND user_id=? "
                    "AND status IN ('uploaded', 'pending', 'failed', 'awaiting_drive') "
                    "ORDER BY (status='uploaded') DESC, id DESC LIMIT 1",
                    (mb_recording_id, user_id),
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
        user_id: int = 0,
        last_editor_user_id: int | None = None,
        audio_sha256: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tracks (
                    mb_recording_id, acoustid, kind, local_path, sidecar_path,
                    relative_path, status, bit_depth, sample_rate, title, artist, album,
                    created_at, telegram_file_id, source_report_json, tags_json,
                    identity_json, topic_name, file_name, drive_file_id, drive_url,
                    drive_sidecar_id, drive_log_id, user_id, last_editor_user_id, audio_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    user_id,
                    last_editor_user_id,
                    audio_sha256,
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

    def find_uploaded_by_relative(
        self, relative_path: str, user_id: int | None = None
    ) -> TrackRecord | None:
        with self._lock:
            if user_id is None:
                row = self._conn.execute(
                    "SELECT * FROM tracks WHERE relative_path=? AND status='uploaded' "
                    "ORDER BY id DESC LIMIT 1",
                    (relative_path,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM tracks WHERE relative_path=? AND status='uploaded' AND user_id=? "
                    "ORDER BY id DESC LIMIT 1",
                    (relative_path, user_id),
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
        source_message_id: int | None = None,
        expires_at: str,
        user_id: int = 0,
        public_message_id: int | None = None,
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
                    source_drive_sidecar_id, telegram_file_id, source_message_id,
                    created_at, expires_at, user_id, public_message_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    source_message_id,
                    _utc_now(),
                    expires_at,
                    user_id,
                    public_message_id,
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
            "source_message_id",
            "topic_name",
            "thread_id",
            "file_name",
            "expires_at",
            "user_id",
            "public_message_id",
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
        if not drive_file_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE drive_file_id=? ORDER BY id DESC LIMIT 1",
                (drive_file_id,),
            ).fetchone()
        return _row_to_track(row) if row else None

    def find_by_telegram_file_id(self, telegram_file_id: str) -> TrackRecord | None:
        if not telegram_file_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE telegram_file_id=? ORDER BY id DESC LIMIT 1",
                (telegram_file_id,),
            ).fetchone()
        return _row_to_track(row) if row else None

    def list_review_tracks(self, user_id: int | None = None) -> list[TrackRecord]:
        with self._lock:
            if user_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM tracks WHERE kind='review' "
                    "AND status IN ('uploaded', 'pending', 'failed') "
                    "ORDER BY id DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tracks WHERE kind='review' AND user_id=? "
                    "AND status IN ('uploaded', 'pending', 'failed') "
                    "ORDER BY id DESC",
                    (user_id,),
                ).fetchall()
        return [_row_to_track(row) for row in rows]

    def count_review_tracks(self, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE kind='review' AND user_id=? "
                "AND status IN ('uploaded', 'pending', 'failed')",
                (user_id,),
            ).fetchone()
        return int(row[0] if row else 0)

    def count_library_tracks(self, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE kind='library' AND user_id=? "
                "AND status IN ('uploaded', 'pending', 'failed', 'awaiting_drive')",
                (user_id,),
            ).fetchone()
        return int(row[0] if row else 0)

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
            "source_chat_id",
            "source_message_id",
            "user_id",
            "last_editor_user_id",
            "audio_sha256",
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

    def list_track_messages(self, track_id: int) -> list[tuple[int, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id, message_id FROM track_messages WHERE track_id=? ORDER BY message_id",
                (track_id,),
            ).fetchall()
        return [(int(row["chat_id"]), int(row["message_id"])) for row in rows]

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

    def get_waiting_for_track(
        self, track_id: int, user_id: int | None = None
    ) -> PendingReview | None:
        with self._lock:
            if user_id is None:
                row = self._conn.execute(
                    "SELECT * FROM pending_reviews WHERE track_id=? AND status='waiting' "
                    "ORDER BY id DESC LIMIT 1",
                    (track_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM pending_reviews WHERE track_id=? AND user_id=? AND status='waiting' "
                    "ORDER BY id DESC LIMIT 1",
                    (track_id, user_id),
                ).fetchone()
        return _row_to_pending(row) if row else None

    def list_library_tracks(self, user_id: int | None = None) -> list[TrackRecord]:
        with self._lock:
            if user_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM tracks WHERE kind='library' "
                    "AND status IN ('uploaded', 'pending', 'failed', 'awaiting_drive') "
                    "ORDER BY id DESC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tracks WHERE kind='library' AND user_id=? "
                    "AND status IN ('uploaded', 'pending', 'failed', 'awaiting_drive') "
                    "ORDER BY id DESC",
                    (user_id,),
                ).fetchall()
        return [_row_to_track(row) for row in rows]

    def list_owned_tracks(self) -> list[TrackRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE kind IN ('library', 'review') "
                "AND status NOT IN ('deleted', 'skipped') "
                "ORDER BY id"
            ).fetchall()
        return [_row_to_track(row) for row in rows]

    def insert_suggest_session(
        self,
        *,
        user_id: int,
        chat_id: int,
        thread_id: int | None,
        query: str,
        results_json: str,
        expires_at: str,
    ) -> int:
        self.prune_suggest_state()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO suggest_sessions (
                    user_id, chat_id, thread_id, query, results_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, chat_id, thread_id, query, results_json, _utc_now(), expires_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_suggest_session(self, session_id: int) -> SuggestSession | None:
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM suggest_sessions WHERE id=? AND expires_at>?",
                (session_id, now),
            ).fetchone()
        return _row_to_suggest(row) if row else None

    def list_suggest_shown(self, user_id: int, limit: int = 50) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT owned_key FROM suggest_shown WHERE user_id=? "
                "ORDER BY shown_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return {str(row["owned_key"]) for row in rows}

    def mark_suggest_shown(self, user_id: int, keys: list[str], keep: int = 50) -> None:
        if not keys:
            return
        now = _utc_now()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO suggest_shown(user_id, owned_key, shown_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, owned_key) DO UPDATE SET shown_at=excluded.shown_at",
                [(user_id, key, now) for key in keys if key],
            )
            extra = self._conn.execute(
                "SELECT owned_key FROM suggest_shown WHERE user_id=? "
                "ORDER BY shown_at DESC",
                (user_id,),
            ).fetchall()
            overflow = [row["owned_key"] for row in extra[keep:]]
            if overflow:
                placeholders = ",".join("?" for _ in overflow)
                self._conn.execute(
                    f"DELETE FROM suggest_shown WHERE user_id=? AND owned_key IN ({placeholders})",
                    (user_id, *overflow),
                )
            self._conn.commit()

    def get_lastfm_cache(self, cache_key: str) -> str | None:
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM lastfm_cache WHERE cache_key=? AND expires_at>?",
                (cache_key, now),
            ).fetchone()
        return str(row["payload_json"]) if row else None

    def set_lastfm_cache(self, cache_key: str, payload_json: str, expires_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO lastfm_cache(cache_key, payload_json, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json, "
                "expires_at=excluded.expires_at",
                (cache_key, payload_json, expires_at),
            )
            self._conn.commit()

    def prune_suggest_state(self) -> int:
        now = _utc_now()
        with self._lock:
            sessions = self._conn.execute(
                "DELETE FROM suggest_sessions WHERE expires_at<=?", (now,)
            )
            cache = self._conn.execute(
                "DELETE FROM lastfm_cache WHERE expires_at<=?", (now,)
            )
            self._conn.commit()
        return (sessions.rowcount or 0) + (cache.rowcount or 0)

    def get_library_tag_index(self) -> str | None:
        payload, _drive_id, _sha = self.get_library_tag_index_meta()
        return payload

    def get_library_tag_index_meta(
        self, user_id: int = 0
    ) -> tuple[str | None, str | None, str | None]:
        with self._lock:
            if user_id:
                row = self._conn.execute(
                    "SELECT payload_json, drive_file_id, payload_sha FROM user_library_index "
                    "WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT payload_json, drive_file_id, payload_sha FROM library_tag_index WHERE id=1"
                ).fetchone()
        if not row:
            return None, None, None
        payload = str(row["payload_json"]) if row["payload_json"] else None
        drive_id = str(row["drive_file_id"]) if row["drive_file_id"] else None
        sha = str(row["payload_sha"]) if row["payload_sha"] else None
        return payload, drive_id, sha

    def set_library_tag_index(
        self,
        payload_json: str,
        *,
        drive_file_id: str | None = None,
        payload_sha: str | None = None,
        user_id: int = 0,
    ) -> None:
        with self._lock:
            if user_id:
                self._conn.execute(
                    "INSERT INTO user_library_index(user_id, payload_json, updated_at, drive_file_id, payload_sha) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET payload_json=excluded.payload_json, "
                    "updated_at=excluded.updated_at, "
                    "drive_file_id=COALESCE(excluded.drive_file_id, user_library_index.drive_file_id), "
                    "payload_sha=COALESCE(excluded.payload_sha, user_library_index.payload_sha)",
                    (user_id, payload_json, _utc_now(), drive_file_id, payload_sha),
                )
            else:
                self._conn.execute(
                    "INSERT INTO library_tag_index(id, payload_json, updated_at, drive_file_id, payload_sha) "
                    "VALUES (1, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json, "
                    "updated_at=excluded.updated_at, "
                    "drive_file_id=COALESCE(excluded.drive_file_id, library_tag_index.drive_file_id), "
                    "payload_sha=COALESCE(excluded.payload_sha, library_tag_index.payload_sha)",
                    (payload_json, _utc_now(), drive_file_id, payload_sha),
                )
            self._conn.commit()

    def _row_to_user(self, row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            telegram_user_id=int(row["telegram_user_id"]),
            first_seen_at=str(row["first_seen_at"]),
            last_active_at=str(row["last_active_at"]),
            songs_edited=int(row["songs_edited"] or 0),
            google_refresh_token=row["google_refresh_token"],
            google_email=row["google_email"],
            gdrive_folder_id=row["gdrive_folder_id"],
            gdrive_review_folder_id=row["gdrive_review_folder_id"],
            settings_json=str(row["settings_json"] or "{}"),
            username=_row_get(row, "username"),
            first_name=_row_get(row, "first_name"),
            last_name=_row_get(row, "last_name"),
        )

    def ensure_user(self, telegram_user_id: int) -> UserRecord:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users(telegram_user_id, first_seen_at, last_active_at) "
                "VALUES (?, ?, ?) ON CONFLICT(telegram_user_id) DO NOTHING",
                (telegram_user_id, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)
            ).fetchone()
        return self._row_to_user(row)

    def get_user(self, telegram_user_id: int) -> UserRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def touch_user(
        self,
        telegram_user_id: int,
        *,
        username: object = _UNSET,
        first_name: object = _UNSET,
        last_name: object = _UNSET,
    ) -> UserRecord:
        user = self.ensure_user(telegram_user_id)
        now = _utc_now()
        sets = ["last_active_at=?"]
        values: list[object] = [now]
        stored = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
        }
        for key, value in (
            ("username", username),
            ("first_name", first_name),
            ("last_name", last_name),
        ):
            if value is _UNSET:
                continue
            clean = _profile_text(value, username=(key == "username"))
            sets.append(f"{key}=?")
            values.append(clean)
            stored[key] = clean
        values.append(telegram_user_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE telegram_user_id=?",
                values,
            )
            self._conn.commit()
        return UserRecord(
            telegram_user_id=user.telegram_user_id,
            first_seen_at=user.first_seen_at,
            last_active_at=now,
            songs_edited=user.songs_edited,
            google_refresh_token=user.google_refresh_token,
            google_email=user.google_email,
            gdrive_folder_id=user.gdrive_folder_id,
            gdrive_review_folder_id=user.gdrive_review_folder_id,
            settings_json=user.settings_json,
            username=stored["username"],
            first_name=stored["first_name"],
            last_name=stored["last_name"],
        )

    def increment_songs_edited(self, telegram_user_id: int) -> None:
        self.ensure_user(telegram_user_id)
        with self._lock:
            self._conn.execute(
                "UPDATE users SET songs_edited=songs_edited+1, last_active_at=? "
                "WHERE telegram_user_id=?",
                (_utc_now(), telegram_user_id),
            )
            self._conn.commit()

    def update_user(self, telegram_user_id: int, **fields: object) -> None:
        allowed = {
            "google_refresh_token",
            "google_email",
            "gdrive_folder_id",
            "gdrive_review_folder_id",
            "settings_json",
            "songs_edited",
            "last_active_at",
            "username",
            "first_name",
            "last_name",
        }
        cols = []
        values = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown user field {key}")
            if key in {"username", "first_name", "last_name"}:
                value = _profile_text(value, username=(key == "username"))
            cols.append(f"{key}=?")
            values.append(value)
        if not cols:
            return
        values.append(telegram_user_id)
        self.ensure_user(telegram_user_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE users SET {', '.join(cols)} WHERE telegram_user_id=?",
                values,
            )
            self._conn.commit()

    def clear_user_google(self, telegram_user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET google_refresh_token=NULL, google_email=NULL, "
                "gdrive_folder_id=NULL, gdrive_review_folder_id=NULL WHERE telegram_user_id=?",
                (telegram_user_id,),
            )
            self._conn.commit()

    def list_active_users(self, since_iso: str) -> list[UserRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE last_active_at>=? ORDER BY last_active_at DESC",
                (since_iso,),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def list_users(self) -> list[UserRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY last_active_at DESC"
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def list_inactive_users(self, before_iso: str) -> list[UserRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE last_active_at<? ORDER BY last_active_at",
                (before_iso,),
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def forget_user(self, telegram_user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET google_refresh_token=NULL, google_email=NULL, "
                "gdrive_folder_id=NULL, gdrive_review_folder_id=NULL, settings_json='{}' "
                "WHERE telegram_user_id=?",
                (telegram_user_id,),
            )
            self._conn.execute(
                "DELETE FROM user_library_index WHERE user_id=?", (telegram_user_id,)
            )
            self._conn.execute(
                "DELETE FROM oauth_tickets WHERE telegram_user_id=?", (telegram_user_id,)
            )
            self._conn.execute(
                "DELETE FROM oauth_states WHERE telegram_user_id=?", (telegram_user_id,)
            )
            self._conn.execute("DELETE FROM hifi_picks WHERE user_id=?", (telegram_user_id,))
            self._conn.commit()

    def delete_user_tracks(self, telegram_user_id: int) -> list[TrackRecord]:
        rows = self.list_user_tracks(telegram_user_id)
        with self._lock:
            self._conn.execute("DELETE FROM tracks WHERE user_id=?", (telegram_user_id,))
            self._conn.commit()
        return rows

    def list_user_tracks(self, telegram_user_id: int) -> list[TrackRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tracks WHERE user_id=? ORDER BY id",
                (telegram_user_id,),
            ).fetchall()
        return [_row_to_track(row) for row in rows]

    def find_user_track_by_mbid(
        self, user_id: int, mb_recording_id: str, kind: str | None = None
    ) -> TrackRecord | None:
        sql = (
            "SELECT * FROM tracks WHERE user_id=? AND mb_recording_id=? "
            "AND status NOT IN ('deleted', 'skipped') "
        )
        args: list[object] = [user_id, mb_recording_id]
        if kind:
            sql += "AND kind=? "
            args.append(kind)
        sql += "ORDER BY (kind='library') DESC, id DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return _row_to_track(row) if row else None

    def find_user_track_by_sha(self, user_id: int, sha256: str) -> TrackRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE user_id=? AND audio_sha256=? "
                "AND status NOT IN ('deleted', 'skipped') ORDER BY id DESC LIMIT 1",
                (user_id, sha256),
            ).fetchone()
        return _row_to_track(row) if row else None

    def upsert_chat(
        self,
        chat_id: int,
        *,
        type: str = "",
        title: str = "",
        username: str = "",
        active: bool = True,
    ) -> None:
        now = _utc_now()
        handle = _profile_text(username, username=True) or ""
        with self._lock:
            self._conn.execute(
                "INSERT INTO chats(chat_id, type, title, username, active, added_at, last_active_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "type=CASE WHEN excluded.type='' THEN chats.type ELSE excluded.type END, "
                "title=CASE WHEN excluded.title='' THEN chats.title ELSE excluded.title END, "
                "username=CASE WHEN excluded.username='' THEN chats.username ELSE excluded.username END, "
                "active=excluded.active, last_active_at=excluded.last_active_at",
                (chat_id, type, title, handle, 1 if active else 0, now, now),
            )
            self._conn.commit()

    def _row_to_chat(self, row: sqlite3.Row) -> ChatRecord:
        return ChatRecord(
            chat_id=int(row["chat_id"]),
            type=str(row["type"] or ""),
            title=str(row["title"] or ""),
            active=bool(row["active"]),
            added_at=str(row["added_at"]),
            last_active_at=str(row["last_active_at"]),
            username=_row_get(row, "username"),
        )

    def get_chat(self, chat_id: int) -> ChatRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_chat(row)

    def list_chats(self, types: tuple[str, ...] | None = None) -> list[ChatRecord]:
        with self._lock:
            if types:
                placeholders = ",".join("?" for _ in types)
                rows = self._conn.execute(
                    f"SELECT * FROM chats WHERE type IN ({placeholders}) ORDER BY title",
                    types,
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM chats ORDER BY title").fetchall()
        return [self._row_to_chat(row) for row in rows]

    def admin_overview(self) -> dict[str, object]:
        users = self.list_users()
        chats = self.list_chats()
        now = datetime.now(timezone.utc)
        since_24h = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        since_7d = (now - timedelta(days=7)).isoformat(timespec="seconds")
        start_day = (now.date() - timedelta(days=13)).isoformat()
        with self._lock:
            black_users = self._conn.execute("SELECT COUNT(*) AS n FROM blacklist_users").fetchone()["n"]
            black_chats = self._conn.execute("SELECT COUNT(*) AS n FROM blacklist_chats").fetchone()["n"]
            blocked_chat_ids = {
                int(row["chat_id"])
                for row in self._conn.execute("SELECT chat_id FROM blacklist_chats").fetchall()
            }
            hour_rows = self._conn.execute(
                "SELECT substr(created_at, 12, 2) AS hour, COUNT(*) AS n FROM tracks "
                "WHERE created_at IS NOT NULL AND length(created_at) >= 13 GROUP BY hour"
            ).fetchall()
            day_rows = self._conn.execute(
                "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n FROM tracks "
                "WHERE substr(created_at, 1, 10) >= ? GROUP BY day",
                (start_day,),
            ).fetchall()
        hours = [0] * 24
        for row in hour_rows:
            try:
                hour = int(row["hour"])
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23:
                hours[hour] = int(row["n"])
        by_day = {str(row["day"]): int(row["n"]) for row in day_rows}
        days = []
        for offset in range(13, -1, -1):
            day = (now.date() - timedelta(days=offset)).isoformat()
            days.append({"date": day, "count": by_day.get(day, 0)})
        groups = [chat for chat in chats if chat_is_group(chat)]
        channels = [chat for chat in chats if chat_is_channel(chat)]
        groups.sort(key=lambda chat: chat.last_active_at, reverse=True)
        channels.sort(key=lambda chat: chat.last_active_at, reverse=True)

        def chat_row(chat: ChatRecord) -> dict[str, object]:
            return {
                "id": chat.chat_id,
                "title": chat.title or "",
                "username": chat.username,
                "type": chat.type or "",
                "last_active": chat.last_active_at,
                "blocked": chat.chat_id in blocked_chat_ids,
            }

        return {
            "counts": {
                "users": len(users),
                "groups": len(groups),
                "channels": len(channels),
                "blacklisted": int(black_users or 0) + int(black_chats or 0),
            },
            "users": [
                {
                    "id": user.telegram_user_id,
                    "username": user.username,
                    "name": user_display_name(user.first_name, user.last_name),
                    "last_active": user.last_active_at,
                    "songs_edited": int(user.songs_edited or 0),
                }
                for user in users
            ],
            "groups": [chat_row(chat) for chat in groups],
            "channels": [chat_row(chat) for chat in channels],
            "activity": {
                "hours": hours,
                "active_24h": sum(1 for user in users if user.last_active_at >= since_24h),
                "active_7d": sum(1 for user in users if user.last_active_at >= since_7d),
            },
            "usage": {"days": days},
        }

    def list_known_chat_ids(self) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id FROM chats WHERE active=1"
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def is_user_blacklisted(self, telegram_user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM blacklist_users WHERE telegram_user_id=?",
                (telegram_user_id,),
            ).fetchone()
        return row is not None

    def is_chat_blacklisted(self, chat_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM blacklist_chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return row is not None

    def blacklist_user(self, telegram_user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO blacklist_users(telegram_user_id, created_at) VALUES (?, ?) "
                "ON CONFLICT(telegram_user_id) DO NOTHING",
                (telegram_user_id, _utc_now()),
            )
            self._conn.commit()

    def unblacklist_user(self, telegram_user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM blacklist_users WHERE telegram_user_id=?",
                (telegram_user_id,),
            )
            self._conn.commit()

    def blacklist_chat(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO blacklist_chats(chat_id, created_at) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO NOTHING",
                (chat_id, _utc_now()),
            )
            self._conn.commit()

    def unblacklist_chat(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM blacklist_chats WHERE chat_id=?", (chat_id,)
            )
            self._conn.commit()

    def create_oauth_ticket(self, token: str, telegram_user_id: int, expires_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_tickets(token, telegram_user_id, expires_at, used) "
                "VALUES (?, ?, ?, 0)",
                (token, telegram_user_id, expires_at),
            )
            self._conn.commit()

    def consume_oauth_ticket(self, token: str) -> int | None:
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT telegram_user_id FROM oauth_tickets "
                "WHERE token=? AND used=0 AND expires_at>?",
                (token, now),
            ).fetchone()
            if not row:
                return None
            cur = self._conn.execute(
                "UPDATE oauth_tickets SET used=1 WHERE token=? AND used=0",
                (token,),
            )
            self._conn.commit()
            if not cur.rowcount:
                return None
            return int(row["telegram_user_id"])

    def create_oauth_state(
        self, state: str, telegram_user_id: int, code_verifier: str, expires_at: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_states(state, telegram_user_id, code_verifier, expires_at, used) "
                "VALUES (?, ?, ?, ?, 0)",
                (state, telegram_user_id, code_verifier, expires_at),
            )
            self._conn.commit()

    def consume_oauth_state(self, state: str) -> tuple[int, str] | None:
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT telegram_user_id, code_verifier FROM oauth_states "
                "WHERE state=? AND used=0 AND expires_at>?",
                (state, now),
            ).fetchone()
            if not row:
                return None
            cur = self._conn.execute(
                "UPDATE oauth_states SET used=1 WHERE state=? AND used=0",
                (state,),
            )
            self._conn.commit()
            if not cur.rowcount:
                return None
            return int(row["telegram_user_id"]), str(row["code_verifier"])

    def put_cached_file(self, sha256: str, path: str, size: int | None) -> None:
        now = _utc_now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO cached_files(sha256, path, size, created_at, last_used_at, refcount) "
                "VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(sha256) DO UPDATE SET last_used_at=excluded.last_used_at, "
                "refcount=cached_files.refcount+1, path=excluded.path",
                (sha256, path, size, now, now),
            )
            self._conn.commit()

    def get_cached_file(self, sha256: str) -> str | None:
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT path FROM cached_files WHERE sha256=?", (sha256,)
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE cached_files SET last_used_at=? WHERE sha256=?",
                (now, sha256),
            )
            self._conn.commit()
            return str(row["path"])

    def list_stale_cache(self, before_iso: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sha256, path FROM cached_files WHERE last_used_at<? AND refcount<=0",
                (before_iso,),
            ).fetchall()
        return [(str(row["sha256"]), str(row["path"])) for row in rows]

    def list_cache_older_than(self, before_iso: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT sha256, path FROM cached_files WHERE last_used_at<?",
                (before_iso,),
            ).fetchall()
        return [(str(row["sha256"]), str(row["path"])) for row in rows]

    def delete_cached_file(self, sha256: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM cached_files WHERE sha256=?", (sha256,))
            self._conn.commit()

    def put_hifi_pick(
        self,
        pick_id: str,
        user_id: int,
        session_id: str,
        hifi_callback: str,
        label: str,
        expires_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO hifi_picks(pick_id, user_id, session_id, hifi_callback, label, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pick_id, user_id, session_id, hifi_callback, label, expires_at),
            )
            self._conn.commit()

    def get_hifi_pick(self, pick_id: str, user_id: int) -> tuple[str, str, str] | None:
        now = _utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, hifi_callback, label FROM hifi_picks "
                "WHERE pick_id=? AND user_id=? AND expires_at>?",
                (pick_id, user_id, now),
            ).fetchone()
        if not row:
            return None
        return str(row["session_id"]), str(row["hifi_callback"]), str(row["label"])

    def list_hifi_picks(self, session_id: str, user_id: int) -> list[tuple[str, str]]:
        now = _utc_now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT pick_id, label FROM hifi_picks "
                "WHERE session_id=? AND user_id=? AND expires_at>?",
                (session_id, user_id, now),
            ).fetchall()
        return [(str(row["pick_id"]), str(row["label"])) for row in rows]

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
