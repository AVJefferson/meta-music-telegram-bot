from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import Chat, User

from app.audio_in import CHECKS_TEXT
from app.bot import intake_audio_message
from app.intake import ingest_local_audio
from app.models import Identity, Job, TagSet
from app.queue import _commit_upload_locked
from tests.support import make_ctx, temp_catalog


class IntakeStatusFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingest_local_audio_with_status_message_id_uses_text_status(self) -> None:
        directory, catalog = temp_catalog()
        try:
            root = Path(directory.name)
            local = root / "song.flac"
            local.write_bytes(b"fLaC")

            edited_messages: list[dict] = []

            async def mock_edit_message_text(text, **kwargs):
                edited_messages.append({"text": text, "kwargs": kwargs})

            bot = SimpleNamespace(
                edit_message_text=mock_edit_message_text,
            )
            jobs_queue: asyncio.Queue[Job] = asyncio.Queue()
            ctx = make_ctx(catalog, cache_root=root / "cache")
            ctx.bot = bot
            ctx.jobs = jobs_queue

            chat = Chat(id=100, type="group")

            with patch("app.intake.send_public_audio") as mock_send_audio:
                pending_id = await ingest_local_audio(
                    ctx,
                    chat=chat,
                    user_id=1,
                    path=local,
                    file_name="song.flac",
                    status_message_id=55,
                )
                mock_send_audio.assert_not_called()

            self.assertEqual(len(edited_messages), 1)
            self.assertEqual(edited_messages[0]["text"], CHECKS_TEXT)
            self.assertEqual(edited_messages[0]["kwargs"]["message_id"], 55)

            row = catalog.get_pending_review(pending_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status_message_id, 55)
            self.assertEqual(row.public_message_id, 55)
            report = json.loads(row.source_report_json)
            self.assertTrue(report.get("text_status"))

            self.assertEqual(jobs_queue.qsize(), 1)
            job = await jobs_queue.get()
            self.assertEqual(job.status_message_id, 55)
            self.assertEqual(job.public_message_id, 55)
        finally:
            directory.cleanup()

    async def test_ingest_local_audio_without_status_message_id_sends_audio(self) -> None:
        directory, catalog = temp_catalog()
        try:
            root = Path(directory.name)
            local = root / "song.flac"
            local.write_bytes(b"fLaC")

            jobs_queue: asyncio.Queue[Job] = asyncio.Queue()
            ctx = make_ctx(catalog, cache_root=root / "cache")
            ctx.jobs = jobs_queue

            chat = Chat(id=100, type="group")

            fake_sent = SimpleNamespace(message_id=88, audio=SimpleNamespace(file_id="audio-fid"))
            with patch("app.intake.send_public_audio", new_callable=AsyncMock) as mock_send_audio:
                mock_send_audio.return_value = fake_sent
                pending_id = await ingest_local_audio(
                    ctx,
                    chat=chat,
                    user_id=1,
                    path=local,
                    file_name="song.flac",
                    status_message_id=None,
                )
                mock_send_audio.assert_called_once()

            row = catalog.get_pending_review(pending_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.status_message_id, 88)
            report = json.loads(row.source_report_json)
            self.assertFalse(report.get("text_status", False))
        finally:
            directory.cleanup()

    async def test_intake_audio_message_passes_status_message_id_and_does_not_delete(self) -> None:
        directory, catalog = temp_catalog()
        try:
            root = Path(directory.name)
            ctx = make_ctx(catalog, cache_root=root / "cache", tmp_root=root / "tmp")

            status_msg = SimpleNamespace(
                message_id=77,
                edit_text=AsyncMock(),
                delete=AsyncMock(),
            )

            user = User(id=1, is_bot=False, first_name="Tester")
            chat = Chat(id=-100123, type="supergroup")
            message = SimpleNamespace(
                message_id=10,
                from_user=user,
                chat=chat,
                audio=SimpleNamespace(file_id="tg-audio-id", file_name="track.flac", mime_type="audio/flac"),
                document=None,
                reply=AsyncMock(return_value=status_msg),
                is_topic_message=False,
                message_thread_id=None,
            )

            ctx.catalog.upsert_chat(chat.id, type="supergroup", title="Test Group", username="test", active=True)
            ctx.catalog.ensure_user(user.id)

            ctx.settings.pending_root = root / "pending"

            with (
                patch("app.audio_in.download_telegram", new_callable=AsyncMock),
                patch("app.botapi.discard_download"),
                patch("app.audio_in.source_deleted", new_callable=AsyncMock, return_value=False),
                patch("app.tags.read_audio_metrics"),
                patch("app.membership.user_is_blocked", new_callable=AsyncMock, return_value=False),
                patch("app.bot.allow_user", new_callable=AsyncMock, return_value=True),
                patch("app.intake.ingest_local_audio", new_callable=AsyncMock) as mock_ingest,
            ):
                bot_mock = SimpleNamespace(
                    get_file=AsyncMock(return_value=SimpleNamespace(file_path="music/track.flac", file_size=100)),
                )
                ctx.bot = bot_mock
                await intake_audio_message(message, ctx)

                mock_ingest.assert_called_once()
                call_kwargs = mock_ingest.call_args.kwargs
                self.assertEqual(call_kwargs["status_message_id"], 77)
                self.assertEqual(call_kwargs["source_message_id"], 10)

                # Ensure status.delete() was NOT called
                status_msg.delete.assert_not_called()
        finally:
            directory.cleanup()

    async def test_commit_upload_with_text_status_sends_audio_and_deletes_placeholder(self) -> None:
        directory, catalog = temp_catalog()
        try:
            root = Path(directory.name)
            dest = root / "library" / "Track.flac"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fLaC")

            deleted_messages: list[dict] = []

            async def mock_delete_message(chat_id, message_id):
                deleted_messages.append({"chat_id": chat_id, "message_id": message_id})

            bot = SimpleNamespace(
                delete_message=mock_delete_message,
                edit_message_text=AsyncMock(),
                edit_message_caption=AsyncMock(),
            )
            ctx = make_ctx(catalog, library_root=root / "library", tmp_root=root / "tmp")
            ctx.bot = bot
            ctx.drive = SimpleNamespace(
                create_file=lambda *args, **kwargs: ("drive-fid", "https://drive.google.com/test"),
            )

            job = Job(
                chat_id=-100,
                thread_id=None,
                topic_name="",
                file_id="",
                file_name="Track.flac",
                status_message_id=33,
                public_message_id=33,
                source_message_id=5,
                user_id=1,
            )

            tags = TagSet(title="Track Title", artist="Artist Name")
            identity = Identity(confidence="high", title="Track Title", artists=["Artist Name"])
            source_report = {"text_status": True}

            fake_sent_audio = SimpleNamespace(
                message_id=99,
                audio=SimpleNamespace(file_id="new-audio-fid"),
                document=None,
            )

            with (
                patch("app.telegram_file.send_public_audio", new_callable=AsyncMock) as mock_send_audio,
                patch("app.queue._autoforward_card", new_callable=AsyncMock),
                patch("app.queue.edit_status", new_callable=AsyncMock) as mock_edit_status,
            ):
                mock_send_audio.return_value = fake_sent_audio
                result = await _commit_upload_locked(
                    ctx,
                    job=job,
                    local=dest,
                    tags=tags,
                    identity=identity,
                    kind="library",
                    source_report=source_report,
                )

                self.assertIsNotNone(result)
                mock_send_audio.assert_called_once()
                send_kwargs = mock_send_audio.call_args.kwargs
                self.assertEqual(send_kwargs["caption"], mock_send_audio.call_args[1].get("caption", send_kwargs.get("caption")))

                # Verify placeholder message 33 was deleted
                self.assertEqual(len(deleted_messages), 1)
                self.assertEqual(deleted_messages[0]["message_id"], 33)

                # Verify job identifiers were updated to the new audio card
                self.assertEqual(job.status_message_id, 99)
                self.assertEqual(job.public_message_id, 99)

                # Verify edit_status was called during upload progress on the text status message, but NOT after send_public_audio
                self.assertEqual(mock_edit_status.call_count, 1)
                self.assertIn("Uploading to Drive…", mock_edit_status.call_args[0][2])
        finally:
            directory.cleanup()

    async def test_commit_upload_with_delete_original_deletes_both_source_and_placeholder(self) -> None:
        directory, catalog = temp_catalog()
        try:
            root = Path(directory.name)
            dest = root / "library" / "Track.flac"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fLaC")

            deleted_messages: list[dict] = []

            async def mock_delete_message(chat_id, message_id):
                deleted_messages.append({"chat_id": chat_id, "message_id": message_id})

            bot = SimpleNamespace(
                delete_message=mock_delete_message,
                edit_message_text=AsyncMock(),
                edit_message_caption=AsyncMock(),
            )
            ctx = make_ctx(catalog, library_root=root / "library", tmp_root=root / "tmp")
            ctx.bot = bot
            ctx.drive = SimpleNamespace(
                create_file=lambda *args, **kwargs: ("drive-fid", "https://drive.google.com/test"),
            )

            job = Job(
                chat_id=-100,
                thread_id=None,
                topic_name="",
                file_id="",
                file_name="Track.flac",
                status_message_id=33,
                public_message_id=33,
                source_message_id=5,
                user_id=1,
                delete_original=True,
            )

            tags = TagSet(title="Track Title", artist="Artist Name")
            identity = Identity(confidence="high", title="Track Title", artists=["Artist Name"])
            source_report = {"text_status": True, "delete_original": True}

            fake_sent_audio = SimpleNamespace(
                message_id=99,
                audio=SimpleNamespace(file_id="new-audio-fid"),
                document=None,
            )

            with (
                patch("app.telegram_file.send_public_audio", new_callable=AsyncMock) as mock_send_audio,
                patch("app.queue._autoforward_card", new_callable=AsyncMock),
                patch("app.queue.edit_status", new_callable=AsyncMock),
            ):
                mock_send_audio.return_value = fake_sent_audio
                result = await _commit_upload_locked(
                    ctx,
                    job=job,
                    local=dest,
                    tags=tags,
                    identity=identity,
                    kind="library",
                    source_report=source_report,
                )

                self.assertIsNotNone(result)
                # Verify both the placeholder (33) and the original source message (5) were deleted
                deleted_ids = [d["message_id"] for d in deleted_messages]
                self.assertIn(33, deleted_ids)
                self.assertIn(5, deleted_ids)
                self.assertEqual(len(deleted_ids), 2)
        finally:
            directory.cleanup()

