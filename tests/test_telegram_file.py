from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.models import TagSet
from app.telegram_file import message_file_id, send_public_audio, update_public_audio


class MessageFileIdTests(unittest.TestCase):
    def test_prefers_audio_over_document(self) -> None:
        msg = SimpleNamespace(
            audio=SimpleNamespace(file_id="audio-1"),
            document=SimpleNamespace(file_id="doc-1"),
        )
        self.assertEqual(message_file_id(msg), "audio-1")

    def test_document_fallback(self) -> None:
        msg = SimpleNamespace(audio=None, document=SimpleNamespace(file_id="doc-1"))
        self.assertEqual(message_file_id(msg), "doc-1")


class SendPublicAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_audio_not_document(self) -> None:
        sent: list = []

        class Bot:
            async def send_audio(self, chat_id, audio, **kwargs):
                sent.append(("audio", chat_id, kwargs))
                return SimpleNamespace(
                    message_id=9,
                    audio=SimpleNamespace(file_id="tg-audio"),
                    document=None,
                )

            async def send_document(self, *args, **kwargs):
                sent.append(("document", args, kwargs))
                raise AssertionError("must not fall back")

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "song.flac"
            path.write_bytes(b"fLaC")
            ctx = SimpleNamespace(bot=Bot())
            result = await send_public_audio(
                ctx, 100, path, caption="hi", tags=TagSet(title="Song", artist="Art")
            )
        self.assertEqual(sent[0][0], "audio")
        self.assertEqual(sent[0][2]["title"], "Song")
        self.assertEqual(sent[0][2]["performer"], "Art")
        self.assertEqual(message_file_id(result), "tg-audio")

    async def test_falls_back_to_document(self) -> None:
        sent: list = []

        class Bot:
            async def send_audio(self, *args, **kwargs):
                raise RuntimeError("flac rejected")

            async def send_document(self, chat_id, document, **kwargs):
                sent.append(("document", chat_id, kwargs))
                return SimpleNamespace(
                    message_id=2,
                    audio=None,
                    document=SimpleNamespace(file_id="tg-doc"),
                )

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "song.flac"
            path.write_bytes(b"fLaC")
            ctx = SimpleNamespace(bot=Bot())
            result = await send_public_audio(ctx, 5, path, caption="cap")
        self.assertEqual(sent[0][0], "document")
        self.assertEqual(message_file_id(result), "tg-doc")

    async def test_edit_uses_input_media_audio(self) -> None:
        kinds: list[str] = []

        class Bot:
            async def edit_message_media(self, **kwargs):
                media = kwargs["media"]
                kinds.append(type(media).__name__)
                return SimpleNamespace(
                    audio=SimpleNamespace(file_id="new-audio"),
                    document=None,
                )

            async def edit_message_caption(self, **kwargs):
                raise AssertionError("caption-only must not run")

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "song.flac"
            path.write_bytes(b"fLaC")
            ctx = SimpleNamespace(bot=Bot())
            file_id = await update_public_audio(
                ctx,
                chat_id=1,
                message_id=2,
                path=path,
                caption="c",
                correct_media=True,
                tags=TagSet(title="T", artist="A"),
            )
        self.assertEqual(kinds, ["InputMediaAudio"])
        self.assertEqual(file_id, "new-audio")
