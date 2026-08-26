from __future__ import annotations

import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from aiogram.enums import ChatMemberStatus
from PIL import Image

from app.bot import resolve_topic
from app.catalog import Catalog, is_general_topic
from app.drive import DriveChild, DriveClient
from app.membership import member_status
from app.private_ui import (
    EDITOR_FIELDS,
    _next_editor_phase,
    _normalize_image,
    _previous_editor_phase,
    _public_addresses,
    _review_keyboard,
    _topic_keyboard,
)
from app.queue import _delete_promoted_review_source, recover_interrupted
from app.review_ui import conflict_keyboard, cover_keyboard, parse_callback, review_keyboard


class CatalogSessionTests(unittest.TestCase):
    def test_pending_session_round_trip_and_topics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            catalog.upsert_topic(9, "Malayalam")
            self.assertEqual(catalog.list_topics(), [(1, "General"), (9, "Malayalam")])
            self.assertEqual(catalog.list_library_topics(), [(9, "Malayalam")])
            pending_id = catalog.insert_pending_review(
                phase="edit:0",
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
                chat_id=123,
                thread_id=None,
                status_message_id=4,
                topic_name="Malayalam",
                file_name="source.flac",
                source_drive_file_id="flac-id",
                source_drive_sidecar_id="json-id",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            row = catalog.get_waiting_for_chat(123)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.id, pending_id)
            self.assertEqual(row.source_drive_file_id, "flac-id")
            self.assertEqual(row.source_drive_sidecar_id, "json-id")
            self.assertTrue(catalog.claim_pending(pending_id, "processing"))
            self.assertFalse(catalog.claim_pending(pending_id, "processing"))
            with self.assertRaises(RuntimeError):
                catalog.insert_pending_review(
                    phase="review_list",
                    local_path="",
                    sidecar_path=None,
                    relative_path=None,
                    kind="review",
                    original_json="{}",
                    recommended_json="{}",
                    working_json="{}",
                    candidates_json="[]",
                    identity_json="{}",
                    source_report_json="{}",
                    chat_id=123,
                    thread_id=None,
                    status_message_id=5,
                    topic_name="",
                    file_name="reviews",
                    expires_at="2099-01-01T00:00:00+00:00",
                )
            track_id = catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path="/tmp/library.flac",
                sidecar_path=None,
                relative_path="General/library.flac",
                bit_depth=16,
                sample_rate=44100,
                title="Song",
                artist="Artist",
                album="Album",
            )
            self.assertGreater(track_id, 0)


class DriveReviewTests(unittest.TestCase):
    def test_recursive_review_listing_pairs_sidecar(self) -> None:
        drive = DriveClient.__new__(DriveClient)
        children = {
            "root": [
                DriveChild("day", "2026-08-15", "application/vnd.google-apps.folder", None, None)
            ],
            "day": [
                DriveChild("flac", "song.flac", "audio/flac", 100, "2026-08-15T10:00:00Z"),
                DriveChild("json", "song.json", "application/json", 20, "2026-08-15T10:00:01Z"),
                DriveChild("txt", "notes.txt", "text/plain", 5, None),
            ],
        }
        drive.list_children = lambda folder_id: children[folder_id]  # type: ignore[method-assign]
        items = drive.list_review_items("root")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].relative_path, "2026-08-15/song.flac")
        self.assertEqual(items[0].sidecar_id, "json")

    def test_library_listing_can_scope_to_language_folder(self) -> None:
        drive = DriveClient.__new__(DriveClient)
        folder = "application/vnd.google-apps.folder"
        children = {
            "root": [
                DriveChild("en", "English", folder, None, None),
                DriveChild("ml", "Malayalam", folder, None, None),
            ],
            "en": [
                DriveChild("aa", "Radiohead", folder, None, None),
            ],
            "aa": [
                DriveChild("al", "OK Computer", folder, None, None),
            ],
            "al": [
                DriveChild("flac", "Radiohead - 06 - Karma Police.flac", "audio/flac", 1, None),
                DriveChild("jpg", "cover.jpg", "image/jpeg", 1, None),
            ],
            "ml": [
                DriveChild("ha", "Harisankar", folder, None, None),
            ],
            "ha": [
                DriveChild("lu", "Lucifer", folder, None, None),
            ],
            "lu": [
                DriveChild("mlf", "Harisankar - 01 - Payaliya.flac", "audio/flac", 1, None),
            ],
            "idx": [
                DriveChild("json", "tracks.json", "application/json", 10, None),
            ],
        }
        children["root"].append(DriveChild("idx", "library", folder, None, None))
        drive.list_children = lambda folder_id: children[folder_id]  # type: ignore[method-assign]
        mal = drive.list_library_items("root", topic="Malayalam")
        self.assertEqual(len(mal), 1)
        self.assertEqual(mal[0].relative_path, "Malayalam/Harisankar/Lucifer/Harisankar - 01 - Payaliya.flac")
        all_items = drive.list_library_items("root")
        self.assertEqual(len(all_items), 2)
        missing = drive.list_library_items("root", topic="Tamil")
        self.assertEqual(missing, [])
        self.assertTrue(all("library" not in item.relative_path.casefold() for item in all_items))


class PromotionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_promoted_review_deletes_flac_then_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
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
                chat_id=123,
                thread_id=None,
                status_message_id=4,
                topic_name="General",
                file_name="source.flac",
                source_drive_file_id="flac-id",
                source_drive_sidecar_id="json-id",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            row = catalog.get_pending_review(pending_id)
            assert row is not None
            deleted: list[str] = []
            ctx = SimpleNamespace(
                drive=SimpleNamespace(delete_file=lambda file_id: deleted.append(file_id))
            )
            self.assertTrue(await _delete_promoted_review_source(ctx, row))
            self.assertEqual(deleted, ["flac-id", "json-id"])

    async def test_queued_dm_job_recovers_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
                phase="dm_topic",
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
                chat_id=123,
                thread_id=None,
                status_message_id=4,
                topic_name="General",
                file_name="source.flac",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            catalog.update_pending_review(pending_id, status="processing")
            jobs: asyncio.Queue = asyncio.Queue()
            await recover_interrupted(SimpleNamespace(catalog=catalog), jobs)
            job = jobs.get_nowait()
            self.assertEqual(job.source_pending_id, pending_id)
            recovered = catalog.get_pending_review(pending_id)
            assert recovered is not None
            self.assertEqual(recovered.status, "queued")

    async def test_queued_intake_job_recovers_from_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
                phase="intake",
                local_path="/tmp/drive-copy.flac",
                sidecar_path=None,
                relative_path=None,
                kind="library",
                original_json="{}",
                recommended_json="{}",
                working_json="{}",
                candidates_json="[]",
                identity_json="{}",
                source_report_json="{}",
                chat_id=-100,
                thread_id=1,
                status_message_id=9,
                topic_name="English",
                file_name="song.flac",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            catalog.update_pending_review(pending_id, status="processing", telegram_file_id=None)
            jobs: asyncio.Queue = asyncio.Queue()
            await recover_interrupted(SimpleNamespace(catalog=catalog), jobs)
            job = jobs.get_nowait()
            self.assertEqual(job.local_path, "/tmp/drive-copy.flac")
            self.assertEqual(job.file_id, "")
            self.assertEqual(job.source_pending_id, pending_id)
            recovered = catalog.get_pending_review(pending_id)
            assert recovered is not None
            self.assertEqual(recovered.status, "queued")


class UiTests(unittest.TestCase):
    def test_cancel_callbacks_exist_in_group_keyboards(self) -> None:
        tags = {"title": "Song"}
        keyboards = [
            review_keyboard(2, tags, tags, tags),
            cover_keyboard(2, [{"label": "file"}]),
            conflict_keyboard(2),
        ]
        for keyboard in keyboards:
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertIn("p2:cancel", callbacks)
        action = parse_callback("p2:cancel")
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.op, "cancel")

    def test_cover_keyboard_back_only_from_review(self) -> None:
        plain = cover_keyboard(2, [{"label": "file"}])
        review = cover_keyboard(2, [{"label": "file"}], from_review=True)
        waiting = cover_keyboard(2, [{"label": "file"}], from_review=True, waiting=True)
        def _ops(keyboard):
            return {
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data
            }
        self.assertNotIn("p2:back", _ops(plain))
        self.assertIn("p2:back", _ops(review))
        self.assertIn("p2:cancel", _ops(review))
        self.assertIn("p2:back", _ops(waiting))
        self.assertNotIn("p2:cv0", _ops(waiting))
        action = parse_callback("p2:back")
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.op, "back")

    def test_private_keyboards_always_offer_cancel(self) -> None:
        for keyboard in (
            _topic_keyboard(3, [(1, "General")], 0),
            _review_keyboard(3, 1, 0),
        ):
            callbacks = [
                button.callback_data
                for row in keyboard.inline_keyboard
                for button in row
                if button.callback_data
            ]
            self.assertIn("d3:cancel", callbacks)

    def test_cover_normalization_outputs_jpeg(self) -> None:
        source = BytesIO()
        Image.new("RGBA", (20, 20), (255, 0, 0, 100)).save(source, format="PNG")
        normalized = _normalize_image(source.getvalue())
        with Image.open(BytesIO(normalized)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.mode, "RGB")

    def test_editor_phase_navigation(self) -> None:
        self.assertEqual(_previous_editor_phase("edit:0"), "edit:0")
        self.assertEqual(_previous_editor_phase("edit:cover"), f"edit:{len(EDITOR_FIELDS) - 1}")
        self.assertEqual(_previous_editor_phase("edit:confirm"), "edit:cover")
        self.assertEqual(_next_editor_phase(0), "edit:1")
        self.assertEqual(_next_editor_phase(len(EDITOR_FIELDS) - 1), "edit:cover")

    def test_membership_enum_normalizes_to_api_value(self) -> None:
        member = SimpleNamespace(status=ChatMemberStatus.MEMBER)
        self.assertEqual(member_status(member), "member")


class GeneralTopicTests(unittest.TestCase):
    def test_is_general_topic(self) -> None:
        self.assertTrue(is_general_topic(1, "English"))
        self.assertTrue(is_general_topic(9, "General"))
        self.assertTrue(is_general_topic(9, "general"))
        self.assertTrue(is_general_topic(12, "Malayalam", is_topic_message=False))
        self.assertFalse(is_general_topic(9, "Malayalam"))
        self.assertFalse(is_general_topic(9, "Malayalam", is_topic_message=True))
        self.assertFalse(is_general_topic(None, "", is_topic_message=True))

    def test_forum_general_message_is_ignored(self) -> None:
        ctx = SimpleNamespace(
            catalog=SimpleNamespace(get_topic=lambda _tid: None, upsert_topic=lambda *_a: None)
        )
        general = SimpleNamespace(message_thread_id=1, is_topic_message=False, reply_to_message=None)
        thread_id, name = resolve_topic(general, ctx)
        self.assertTrue(is_general_topic(thread_id, name, is_topic_message=general.is_topic_message))
        ctx = SimpleNamespace(
            catalog=SimpleNamespace(
                get_topic=lambda tid: "Malayalam" if tid == 9 else None,
                upsert_topic=lambda *_a: None,
            )
        )
        language = SimpleNamespace(message_thread_id=9, is_topic_message=True, reply_to_message=None)
        thread_id, name = resolve_topic(language, ctx)
        self.assertEqual(name, "Malayalam")
        self.assertFalse(is_general_topic(thread_id, name, is_topic_message=language.is_topic_message))

    def test_library_topics_drop_general_even_if_stored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            catalog.upsert_topic(1, "General")
            catalog.upsert_topic(9, "Malayalam")
            catalog.upsert_topic(12, "English")
            self.assertEqual(catalog.list_library_topics(), [(12, "English"), (9, "Malayalam")])


class UrlSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_cover_url_is_rejected(self) -> None:
        self.assertEqual(await _public_addresses("localhost"), [])


if __name__ == "__main__":
    unittest.main()
