from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, EphemeralMessageParameters, Message, Update, User

from app.authenticity import AuthenticityResult
from app.bot import CtxMiddleware
from app.captions import music_caption
from app.edit_ui import _tech_for_row
from app.models import Identity, Job, TagSet
from app.queue import _commit_upload, _preview, _tech_block, tag_preview
from app.review_cmd import format_song_card
from app.suggest import DEFAULT_SUGGEST_COUNT, PAGE_SIZE, SUGGEST_COUNTS, Suggestion
from app.suggest_cmd import _list_text, format_suggest_card, suggest_list_keyboard
from app.user_cmd import build_user_command_router
from tests.support import make_ctx, temp_catalog

DRIVE_URL = "https://drive.google.com/file/d/abc123/view"
RELATIVE = "English/Sam/Hour/Sam - 01 - Stay.flac"
EMAIL = "ada@example.com"


def _track(**overrides):
    data = dict(
        kind="library",
        drive_url=DRIVE_URL,
        relative_path=RELATIVE,
        local_path=None,
        tags_json=json.dumps({"title": "Stay", "artist": "Sam", "album": "Hour"}),
        source_report_json=json.dumps(
            {"authenticity": {"verdict": "FAKE_CERTAIN", "score": 92, "cutoff_hz": 16000}}
        ),
        identity_json=None,
        title="Stay",
        artist="Sam",
        album="Hour",
        bit_depth=16,
        sample_rate=44100,
        source_chat_id=-100,
        last_editor_user_id=9,
        file_name="stay.flac",
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def _card_hides_personal(text: str) -> None:
    lowered = text.casefold()
    if "authenticity" in lowered:
        raise AssertionError(text)
    if "saved (" in lowered:
        raise AssertionError(text)
    if RELATIVE in text or "editor:" in text:
        raise AssertionError(text)
    if text.startswith("library") or text.startswith("review"):
        raise AssertionError(text)


class SongCardPrivacyTests(unittest.TestCase):
    def test_cards_omit_authenticity_and_location(self) -> None:
        track = _track()
        group = format_song_card(track, chat_id=-100)
        channel = format_song_card(track, chat_id=-1001234567890)
        private = format_song_card(track, chat_id=42)
        fallback = format_song_card(track)
        review = format_song_card(_track(kind="review"), chat_id=-100)
        for text in (group, channel, private, fallback, review):
            _card_hides_personal(text)
            self.assertIn("Stay", text)
            self.assertIn("Sam", text)
        for text in (group, channel, fallback, review):
            self.assertNotIn("drive.google.com", text)
        self.assertIn(DRIVE_URL, private)
        self.assertNotIn("review", review.casefold())

    def test_status_preview_omits_authenticity(self) -> None:
        result = AuthenticityResult(verdict="FAKE_CERTAIN", score=92, cutoff_hz=16000)
        preview = tag_preview(TagSet(title="Stay", artist="Sam"), authenticity=result)
        self.assertNotIn("Authenticity", preview)
        identity = Identity(confidence="high", title="Stay")
        report = {"authenticity": result.to_dict()}
        queued = _preview(TagSet(title="Stay"), identity, report=report)
        self.assertNotIn("Authenticity", queued)
        self.assertNotIn("Authenticity", _tech_block(identity, report=report))
        row = SimpleNamespace(
            local_path=None,
            identity_json=json.dumps({"source_report": {"authenticity": result.to_dict()}, "duration": 1}),
            source_report_json=json.dumps({"authenticity": result.to_dict(), "duration": 1}),
        )
        self.assertNotIn("Authenticity", _tech_for_row(row))

    def test_caption_omits_path_editor_and_saved(self) -> None:
        caption = music_caption(
            tags=TagSet(title="Stay", artist="Sam", album="Hour", genre="Pop"),
            relative_path=RELATIVE,
            extra="identifying…",
            last_editor_user_id=9,
        )
        self.assertIn("Stay", caption)
        self.assertIn("identifying…", caption)
        self.assertNotIn(RELATIVE, caption)
        self.assertNotIn("editor:", caption)
        self.assertNotIn("Saved", caption)
        self.assertNotIn("drive.google.com", caption)


class _Drive:
    def ensure_parent(self, root_id, relative):
        return "parent-1"

    def find_name_conflicts(self, parent_id, filename):
        return []

    def create_file(self, dest, parent_id, filename, mime):
        return "file-1", DRIVE_URL

    def delete_file(self, file_id):
        return None


class SaveConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def _save(self, chat_id: int, *, notify_group_save: bool | None = None):
        directory, catalog = temp_catalog()
        edits: list[str] = []
        sends: list[dict] = []
        captions: list[str] = []
        try:
            catalog.ensure_user(7)
            fields: dict = {"gdrive_folder_id": "root-folder"}
            if notify_group_save is not None:
                fields["settings_json"] = json.dumps({"notify_group_save": notify_group_save})
            catalog.update_user(7, **fields)
            root = Path(directory.name)
            local = root / "stay.flac"
            local.write_bytes(b"fLaC")
            ctx = make_ctx(catalog)
            ctx.settings.library_root = root / "library"
            ctx.settings.review_root = root / "review"
            ctx.settings.pending_root = root / "pending"
            ctx.settings.enable_log_per_music_file = False
            ctx.drive = _Drive()

            async def edit_message_text(text, **kwargs):
                edits.append(text)

            async def send_message(*args, **kwargs):
                text = kwargs.get("text")
                if text is None and len(args) > 1:
                    text = args[1]
                sends.append({"text": text, "kwargs": kwargs})
                return SimpleNamespace(message_id=3, chat=SimpleNamespace(id=chat_id))

            ctx.bot = SimpleNamespace(edit_message_text=edit_message_text, send_message=send_message)
            job = Job(
                chat_id,
                None,
                "English",
                "",
                "stay.flac",
                10,
                user_id=7,
            )
            tags = TagSet(title="Stay", artist="Sam", album="Hour")
            identity = Identity(confidence="high", title="Stay", artists=["Sam"])
            report = {
                "drive_dest": "library",
                "authenticity": {"verdict": "FAKE_CERTAIN", "score": 92, "cutoff_hz": 16000},
            }

            async def fake_audio(*args, **kwargs):
                captions.append(kwargs.get("caption") or "")
                return None

            with (
                patch("app.queue.remember_library_tags", lambda *a, **k: None),
                patch("app.telegram_file.update_public_audio", fake_audio),
            ):
                await _commit_upload(
                    ctx,
                    job=job,
                    local=local,
                    tags=tags,
                    identity=identity,
                    kind="library",
                    source_report=report,
                )
            return edits, sends, captions
        finally:
            catalog.close()
            directory.cleanup()

    async def test_group_save_location_is_ephemeral_without_drive_url(self) -> None:
        edits, sends, captions = await self._save(-100)
        public = "\n".join(edits)
        _card_hides_personal(public)
        self.assertNotIn("drive.google.com", public)
        self.assertNotIn("<code>", public)
        self.assertEqual(len(sends), 1)
        note = sends[0]["text"]
        self.assertIn("Saved (library)", note)
        self.assertIn("<code>", note)
        self.assertIn("Sam", note)
        self.assertNotIn("drive.google.com", note)
        self.assertNotIn("Authenticity", note)
        self.assertNotIn("Artist:", note)
        params = sends[0]["kwargs"].get("ephemeral_message_parameters")
        self.assertIsInstance(params, EphemeralMessageParameters)
        self.assertEqual(params.receiver_user_id, 7)
        self.assertTrue(captions)
        _card_hides_personal(captions[0])
        self.assertNotIn("drive.google.com", captions[0])
        self.assertNotIn("<code>", captions[0])

    async def test_group_save_note_follows_setting(self) -> None:
        edits_off, sends_off, _captions_off = await self._save(-100, notify_group_save=False)
        public_off = "\n".join(edits_off)
        _card_hides_personal(public_off)
        self.assertNotIn("drive.google.com", public_off)
        self.assertNotIn("<code>", public_off)
        self.assertEqual(sends_off, [])

        edits_on, sends_on, _captions_on = await self._save(-100, notify_group_save=True)
        public_on = "\n".join(edits_on)
        _card_hides_personal(public_on)
        self.assertNotIn("drive.google.com", public_on)
        self.assertEqual(len(sends_on), 1)
        note = sends_on[0]["text"]
        self.assertIn("Saved (library)", note)
        self.assertIn("<code>", note)
        self.assertNotIn("drive.google.com", note)
        self.assertNotIn("Authenticity", note)
        params = sends_on[0]["kwargs"].get("ephemeral_message_parameters")
        self.assertIsInstance(params, EphemeralMessageParameters)
        self.assertEqual(params.receiver_user_id, 7)

    async def test_private_save_card_has_drive_link_only(self) -> None:
        edits, sends, captions = await self._save(42)
        public = "\n".join(edits)
        _card_hides_personal(public)
        self.assertIn(DRIVE_URL, edits[-1])
        self.assertNotIn("<code>", public)
        self.assertEqual(sends, [])
        self.assertTrue(captions)
        self.assertNotIn("drive.google.com", captions[0])
        self.assertNotIn("Saved", captions[0])


class SuggestListPrivacyTests(unittest.TestCase):
    def test_group_list_has_no_library_mark(self) -> None:
        item = Suggestion(artist="A", title="B", score=1, why="because", url="", in_library=True)
        for chat_id in (-100, -1001234567890):
            card = format_suggest_card(item, chat_id=chat_id)
            listing = _list_text(0, 2, "English", "jazz", chat_id=chat_id)
            label = suggest_list_keyboard(1, [item], 0, chat_id=chat_id).inline_keyboard[0][0].text
            self.assertNotIn("In library", card)
            self.assertNotIn("in library", listing.casefold())
            self.assertNotIn("✓", listing)
            self.assertNotIn("✓", label)
        private = format_suggest_card(item, chat_id=5)
        self.assertIn("In library", private)
        self.assertIn("✓ = in library", _list_text(0, 1, None, "", chat_id=5))
        self.assertIn("In library", format_suggest_card(item))


class _Session(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        chat_id = int(method.chat_id)
        kind = "supergroup" if chat_id < 0 else "private"
        return Message(
            message_id=len(self.calls) + 10,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type=kind),
            text=getattr(method, "text", None) or "",
        )

    async def stream_content(
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ):
        if False:
            yield b""
        raise AssertionError("no stream")


class UserCardPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def _command(self, text: str):
        directory, catalog = temp_catalog()
        try:
            catalog.ensure_user(9)
            catalog.update_user(9, google_email=EMAIL)
            session = _Session()
            bot = Bot(token="4242:AAEtesttoken", session=session)
            ctx = make_ctx(catalog)
            ctx.bot = bot
            dp = Dispatcher()
            dp.update.middleware(CtxMiddleware(ctx))
            dp.include_router(build_user_command_router())
            msg = Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=-100, type="supergroup", title="Room"),
                from_user=User(id=9, is_bot=False, first_name="Ada"),
                text=text,
            )
            await dp.feed_update(bot, Update(update_id=1, message=msg))
            await bot.session.close()
            return session.calls
        finally:
            catalog.close()
            directory.cleanup()

    def _assert_email_is_ephemeral(self, calls) -> None:
        email_calls = [call for call in calls if EMAIL in (getattr(call, "text", None) or "")]
        self.assertTrue(email_calls)
        for call in calls:
            text = getattr(call, "text", None) or ""
            params = getattr(call, "ephemeral_message_parameters", None)
            if EMAIL in text:
                self.assertIsInstance(params, EphemeralMessageParameters)
                self.assertEqual(params.receiver_user_id, 9)
            else:
                self.assertNotIn(EMAIL, text)

    async def test_group_start_email_is_ephemeral(self) -> None:
        self._assert_email_is_ephemeral(await self._command("/start"))

    async def test_group_settings_email_is_ephemeral(self) -> None:
        self._assert_email_is_ephemeral(await self._command("/settings"))


class SuggestDefaultTests(unittest.TestCase):
    def test_mini_app_default_count_is_ten(self) -> None:
        self.assertEqual(DEFAULT_SUGGEST_COUNT, 10)
        self.assertEqual(SUGGEST_COUNTS, (10, 20, 50, 100))
        self.assertEqual(PAGE_SIZE, 8)
        js = Path(__file__).resolve().parents[1].joinpath("app/webapp/static/app.js").read_text()
        self.assertIn("let suggestCount = 10;", js)
        self.assertIn("const SUGGEST_COUNTS = [10, 20, 50, 100];", js)
