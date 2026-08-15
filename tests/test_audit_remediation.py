from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import botapi
from app.catalog import Catalog
from app.drive import DriveChild, DriveClient
from app.membership import clear_cache, is_forum_member
from app.queue import recover_interrupted
from app.util import safe_link


def _pending_kwargs(**overrides) -> dict:
    base = dict(
        phase="tags",
        local_path="/tmp/source.flac",
        sidecar_path=None,
        relative_path=None,
        kind="library",
        original_json="{}",
        recommended_json="{}",
        working_json="{}",
        candidates_json="[]",
        identity_json="{}",
        source_report_json="{}",
        chat_id=-100123,
        thread_id=None,
        status_message_id=4,
        topic_name="General",
        file_name="source.flac",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    base.update(overrides)
    return base


class SafeLinkTests(unittest.TestCase):
    def test_quotes_are_escaped_so_the_attribute_cannot_break_out(self) -> None:
        hostile = 'https://example.com/a.jpg"><script>x</script>'
        escaped = safe_link(hostile)
        self.assertNotIn('"', escaped)
        self.assertIn("&quot;", escaped)

    def test_non_http_schemes_and_newlines_are_dropped(self) -> None:
        for value in ("javascript:alert(1)", "file:///etc/passwd", "", None, "https://a\nb"):
            self.assertEqual(safe_link(value), "")

    def test_plain_url_survives(self) -> None:
        self.assertEqual(safe_link("https://example.com/a.jpg"), "https://example.com/a.jpg")


class CoverGalleryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_cover_gallery_is_deleted_and_not_reposted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
                **_pending_kwargs(
                    phase="cover",
                    source_report_json=(
                        '{"cover_picker":{"role":"leader","album":"A","albumartist":"B",'
                        '"options":[{"label":"file","url":"https://example.com/a.jpg"}],'
                        '"media_message_ids":[101,102]}}'
                    ),
                )
            )
            deleted: list[int] = []
            sent_photos: list[object] = []

            async def delete_message(*, chat_id, message_id):
                deleted.append(int(message_id))

            async def edit_message_text(*args, **kwargs):
                return SimpleNamespace(message_id=4)

            async def send_photo(*args, **kwargs):
                sent_photos.append(kwargs)
                return SimpleNamespace(message_id=99)

            async def send_media_group(*args, **kwargs):
                sent_photos.append(kwargs)
                return [SimpleNamespace(message_id=99)]

            ctx = SimpleNamespace(
                catalog=catalog,
                bot=SimpleNamespace(
                    delete_message=delete_message,
                    edit_message_text=edit_message_text,
                    send_photo=send_photo,
                    send_media_group=send_media_group,
                    send_document=send_photo,
                    send_message=edit_message_text,
                ),
            )
            await recover_interrupted(ctx, asyncio.Queue())
            self.assertEqual(deleted, [101, 102])
            self.assertEqual(sent_photos, [])
            row = catalog.get_pending_review(pending_id)
            assert row is not None
            self.assertEqual(row.status, "waiting")
            self.assertNotIn("101", row.source_report_json)
            catalog.close()


class GroupJobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_group_job_is_requeued_with_its_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
                **_pending_kwargs(
                    phase="intake",
                    status="queued",
                    local_path="",
                    thread_id=7,
                    telegram_file_id="tg-file-1",
                )
            )
            catalog.update_pending_review(pending_id, status="processing")
            jobs: asyncio.Queue = asyncio.Queue()
            await recover_interrupted(SimpleNamespace(catalog=catalog), jobs)
            job = jobs.get_nowait()
            self.assertEqual(job.source_pending_id, pending_id)
            self.assertEqual(job.file_id, "tg-file-1")
            self.assertEqual(job.thread_id, 7)
            self.assertFalse(job.private)
            catalog.close()

    async def test_intake_row_without_file_id_fails_instead_of_looping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
                **_pending_kwargs(phase="intake", status="queued", local_path="")
            )
            jobs: asyncio.Queue = asyncio.Queue()
            await recover_interrupted(SimpleNamespace(catalog=catalog), jobs)
            self.assertTrue(jobs.empty())
            row = catalog.get_pending_review(pending_id)
            assert row is not None
            self.assertEqual(row.status, "failed")
            catalog.close()


class PendingPruneTests(unittest.TestCase):
    def test_only_old_finished_rows_are_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            done = catalog.insert_pending_review(**_pending_kwargs())
            waiting = catalog.insert_pending_review(**_pending_kwargs())
            catalog.update_pending_review(done, status="done")
            # Backdate both; only the finished one should go.
            with catalog._lock:
                catalog._conn.execute(
                    "UPDATE pending_reviews SET created_at='2000-01-01T00:00:00+00:00'"
                )
                catalog._conn.commit()
            self.assertEqual(catalog.prune_finished_pending(keep_days=30), 1)
            self.assertIsNone(catalog.get_pending_review(done))
            self.assertIsNotNone(catalog.get_pending_review(waiting))
            catalog.close()


class BotApiCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_root = botapi.BOT_API_ROOT
        self._tmp = tempfile.TemporaryDirectory()
        botapi.BOT_API_ROOT = Path(self._tmp.name)

    def tearDown(self) -> None:
        botapi.BOT_API_ROOT = self._real_root
        self._tmp.cleanup()

    def test_discard_removes_a_downloaded_file(self) -> None:
        music = botapi.BOT_API_ROOT / "token" / "music"
        music.mkdir(parents=True)
        target = music / "file_1.flac"
        target.write_bytes(b"x")
        botapi.discard_download(str(target))
        self.assertFalse(target.exists())

    def test_discard_refuses_paths_outside_the_volume(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "keep.flac"
            outside.write_bytes(b"x")
            botapi.discard_download(str(outside))
            self.assertTrue(outside.exists())

    def test_sweep_keeps_binlogs_and_recent_files(self) -> None:
        token = botapi.BOT_API_ROOT / "token"
        (token / "music").mkdir(parents=True)
        binlog = token / "td.binlog"
        binlog.write_bytes(b"db")
        recent = token / "music" / "file_new.flac"
        recent.write_bytes(b"x")
        stale = token / "music" / "file_old.flac"
        stale.write_bytes(b"x")
        old = time.time() - 48 * 3600
        import os

        os.utime(stale, (old, old))
        os.utime(binlog, (old, old))
        self.assertEqual(botapi.sweep_downloads(older_than_hours=24), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(binlog.exists())


class DriveLookupTests(unittest.TestCase):
    def test_find_by_name_filters_case_variants_returned_by_drive(self) -> None:
        drive = DriveClient.__new__(DriveClient)
        captured: dict[str, str] = {}

        class _Files:
            def list(self, **kwargs):
                captured.update(kwargs)
                return self

            def execute(self):
                return {
                    "files": [
                        {"id": "a", "name": "Song.flac", "mimeType": "audio/flac", "size": "1"},
                        {"id": "b", "name": "song.flac", "mimeType": "audio/flac", "size": "1"},
                    ]
                }

        drive._service = SimpleNamespace(files=lambda: _Files())  # type: ignore[attr-defined]
        hits = drive.find_by_name("parent", "Song.flac")
        self.assertEqual([h.id for h in hits], ["a"])
        # Scoped to the folder rather than listing all of it.
        self.assertIn("'parent' in parents", captured["q"])
        self.assertIn("name = 'Song.flac'", captured["q"])

    def test_name_with_apostrophe_is_escaped(self) -> None:
        drive = DriveClient.__new__(DriveClient)
        captured: dict[str, str] = {}

        class _Files:
            def list(self, **kwargs):
                captured.update(kwargs)
                return self

            def execute(self):
                return {"files": []}

        drive._service = SimpleNamespace(files=lambda: _Files())  # type: ignore[attr-defined]
        drive.find_by_name("parent", "Don't Stop.flac")
        self.assertIn("\\'", captured["q"])


class DrivePruneTests(unittest.TestCase):
    def test_empty_folders_are_removed_deepest_first(self) -> None:
        drive = DriveClient.__new__(DriveClient)
        drive._folder_cache = {}  # type: ignore[attr-defined]
        folder = "application/vnd.google-apps.folder"
        children = {
            "root": [
                DriveChild("empty-day", "2026-08-01", folder, None, None),
                DriveChild("full-day", "2026-08-02", folder, None, None),
            ],
            "empty-day": [DriveChild("nested", "nested", folder, None, None)],
            "nested": [],
            "full-day": [DriveChild("f", "song.flac", "audio/flac", 1, None)],
        }
        deleted: list[str] = []
        drive.list_children = lambda folder_id: children[folder_id]  # type: ignore[method-assign]
        drive.delete_file = lambda file_id: deleted.append(file_id)  # type: ignore[method-assign]
        removed = drive.prune_empty_folders("root")
        self.assertEqual(removed, 2)
        self.assertEqual(deleted, ["nested", "empty-day"])


class MembershipCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_cache()

    async def test_repeat_checks_hit_the_api_once(self) -> None:
        calls: list[int] = []

        async def get_chat_member(chat_id: int, user_id: int):
            calls.append(user_id)
            return SimpleNamespace(status="member")

        ctx = SimpleNamespace(
            settings=SimpleNamespace(allowed_chat_id=-100123),
            bot=SimpleNamespace(get_chat_member=get_chat_member),
        )
        self.assertTrue(await is_forum_member(ctx, 42))
        self.assertTrue(await is_forum_member(ctx, 42))
        self.assertEqual(len(calls), 1)

    async def test_non_member_is_rejected_and_missing_user_short_circuits(self) -> None:
        async def get_chat_member(chat_id: int, user_id: int):
            return SimpleNamespace(status="left")

        ctx = SimpleNamespace(
            settings=SimpleNamespace(allowed_chat_id=-100123),
            bot=SimpleNamespace(get_chat_member=get_chat_member),
        )
        self.assertFalse(await is_forum_member(ctx, 42))
        self.assertFalse(await is_forum_member(ctx, None))

    async def test_api_failure_without_a_cached_answer_denies(self) -> None:
        async def get_chat_member(chat_id: int, user_id: int):
            raise RuntimeError("telegram down")

        ctx = SimpleNamespace(
            settings=SimpleNamespace(allowed_chat_id=-100123),
            bot=SimpleNamespace(get_chat_member=get_chat_member),
        )
        self.assertFalse(await is_forum_member(ctx, 42))


if __name__ == "__main__":
    unittest.main()
