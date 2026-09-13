from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.cleanup import (
    LOCAL_TTL_DAYS,
    LOGGED_OUT_TTL_DAYS,
    _forget_inactive,
    _purge_cache,
    _purge_logged_out_locals,
    _purge_tmp,
    _purge_uploaded_local,
)
from app.paths import remember_cache
from tests.support import make_ctx, temp_catalog


class CleanupUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_logged_out_old_file_deleted(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            lib = Path(directory.name) / "library" / "1"
            lib.mkdir(parents=True)
            flac = lib / "old.flac"
            flac.write_bytes(b"flac")
            old = datetime.now(timezone.utc) - timedelta(days=LOGGED_OUT_TTL_DAYS + 1)
            os.utime(flac, (old.timestamp(), old.timestamp()))
            track_id = catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path=str(flac),
                sidecar_path=None,
                relative_path="old.flac",
                bit_depth=None,
                sample_rate=None,
                title="t",
                artist="a",
                album="b",
                status="uploaded",
                user_id=5,
            )
            catalog.ensure_user(5)
            ctx = make_ctx(catalog, library_root=Path(directory.name) / "library")
            _purge_logged_out_locals(ctx)
            self.assertFalse(flac.exists())
            self.assertIsNone(catalog.get_track(track_id).local_path)

    async def test_processing_not_deleted(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            lib = Path(directory.name) / "library" / "1"
            lib.mkdir(parents=True)
            flac = lib / "busy.flac"
            flac.write_bytes(b"flac")
            old = datetime.now(timezone.utc) - timedelta(days=LOGGED_OUT_TTL_DAYS + 1)
            os.utime(flac, (old.timestamp(), old.timestamp()))
            catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path=str(flac),
                sidecar_path=None,
                relative_path="busy.flac",
                bit_depth=None,
                sample_rate=None,
                title="t",
                artist="a",
                album="b",
                status="processing",
                user_id=5,
            )
            catalog.ensure_user(5)
            ctx = make_ctx(catalog, library_root=Path(directory.name) / "library")
            _purge_logged_out_locals(ctx)
            self.assertTrue(flac.exists())

    async def test_forget_clears_token_first(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.ensure_user(8)
            catalog.update_user(8, google_refresh_token="secret-refresh", google_email="a@x.com")
            catalog._conn.execute(
                "UPDATE users SET last_active_at=? WHERE telegram_user_id=8",
                ("2000-01-01T00:00:00+00:00",),
            )
            catalog._conn.commit()
            ctx = make_ctx(catalog, user_inactive_months=6, admin_telegram_user_id=1)
            await _forget_inactive(ctx)
            user = catalog.get_user(8)
            self.assertIsNone(user.google_refresh_token)
            self.assertFalse(user.logged_in)

    async def test_stale_cache_removed(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            cache_root = Path(directory.name) / "cache"
            ctx = make_ctx(catalog, cache_root=cache_root)
            sha = "a" * 64
            src = Path(directory.name) / "src.flac"
            src.write_bytes(b"xx")
            remember_cache(ctx, sha, src)
            catalog._conn.execute(
                "UPDATE cached_files SET last_used_at=? WHERE sha256=?",
                ("2000-01-01T00:00:00+00:00", sha),
            )
            catalog._conn.commit()
            _purge_cache(ctx)
            self.assertIsNone(catalog.get_cached_file(sha))


def _uploaded_track(catalog, flac: Path, *, uploaded_at: str, drive_file_id: str = "drv") -> int:
    track_id = catalog.insert_pending(
        kind="library",
        mb_recording_id=None,
        acoustid=None,
        local_path=str(flac),
        sidecar_path=None,
        relative_path=flac.name,
        bit_depth=None,
        sample_rate=None,
        title="t",
        artist="a",
        album="b",
        status="uploaded",
        drive_file_id=drive_file_id,
    )
    catalog._conn.execute("UPDATE tracks SET uploaded_at=? WHERE id=?", (uploaded_at, track_id))
    catalog._conn.commit()
    return track_id


class UploadedLocalRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_drive_local_kept(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            flac = Path(directory.name) / "fresh.flac"
            flac.write_bytes(b"flac")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            track_id = _uploaded_track(catalog, flac, uploaded_at=now)
            ctx = make_ctx(catalog)

            def boom(*_a, **_k):
                raise AssertionError("fresh local must not hit Drive")

            ctx.drive.file_exists = boom
            await _purge_uploaded_local(ctx, catalog.get_track(track_id))
            self.assertTrue(flac.exists())
            self.assertEqual(catalog.get_track(track_id).local_path, str(flac))

    async def test_old_drive_local_purged(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            flac = Path(directory.name) / "old.flac"
            flac.write_bytes(b"flac")
            old = (datetime.now(timezone.utc) - timedelta(days=LOCAL_TTL_DAYS + 1)).isoformat(
                timespec="seconds"
            )
            track_id = _uploaded_track(catalog, flac, uploaded_at=old)
            ctx = make_ctx(catalog)
            ctx.drive.file_exists = lambda *_a, **_k: True
            await _purge_uploaded_local(ctx, catalog.get_track(track_id))
            self.assertFalse(flac.exists())
            self.assertIsNone(catalog.get_track(track_id).local_path)


class TmpRetentionTests(unittest.TestCase):
    def test_purge_tmp_keeps_fresh_drops_old(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            fresh = directory / "fresh"
            aged = directory / "aged"
            fresh.mkdir()
            aged.mkdir()
            (fresh / "x").write_text("ok")
            (aged / "x").write_text("old")
            stamp = datetime.now(timezone.utc) - timedelta(days=LOCAL_TTL_DAYS + 1)
            os.utime(aged, (stamp.timestamp(), stamp.timestamp()))
            _purge_tmp(directory)
            self.assertTrue(fresh.exists())
            self.assertFalse(aged.exists())

