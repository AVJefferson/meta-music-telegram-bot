from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiogram import Dispatcher, Router
from aiogram.types import Message

from app.bot import polling_allowed_updates
from app.catalog import Catalog
from app.edit_ui import (
    apply_suggestion,
    exit_edit_keyboard,
    format_edit_card,
    parse_edit_callback,
    suggestions_for,
)
from app.genre import GenreMapper
from app.library import library_relative, review_relative
from app.models import PendingReview, TagSet
from app.reactions import (
    DELETE_EMOJIS,
    FOLDED,
    MONKEY,
    POO,
    THUMBS_DOWN,
    THUMBS_UP,
    WRITING,
    added_emojis,
    build_reactions_router,
    normalize_emoji,
    parse_react_callback,
    removed_emojis,
)
from app.relocate import ensure_local_flac
from app.review_cmd import review_label


class PollingAllowedUpdatesTests(unittest.TestCase):
    def test_always_requests_message_and_reaction(self) -> None:
        dp = Dispatcher()
        self.assertEqual(polling_allowed_updates(dp), ["message", "message_reaction"])

    def test_keeps_callback_query_from_handlers(self) -> None:
        dp = Dispatcher()
        dp.include_router(build_reactions_router())
        messages = Router()

        @messages.message()
        async def _unused(message: Message) -> None:
            del message

        dp.include_router(messages)
        allowed = polling_allowed_updates(dp)
        self.assertEqual(allowed, ["callback_query", "message", "message_reaction"])


class EmojiReactionTests(unittest.TestCase):
    def test_writing_hands_strips_variation_selector(self) -> None:
        self.assertEqual(normalize_emoji("✍️"), WRITING)
        self.assertEqual(normalize_emoji("✍"), WRITING)

    def test_add_only_vs_remove(self) -> None:
        old = [SimpleNamespace(emoji="👍")]
        new = [SimpleNamespace(emoji="👍"), SimpleNamespace(emoji="👎")]
        self.assertEqual(added_emojis(old, new), {THUMBS_DOWN})
        self.assertEqual(removed_emojis(new, old), {THUMBS_DOWN})

    def test_writing_remove_detected(self) -> None:
        old = [SimpleNamespace(emoji="✍️")]
        new: list = []
        self.assertEqual(removed_emojis(old, new), {WRITING})
        self.assertEqual(added_emojis(old, new), set())

    def test_delete_aliases(self) -> None:
        self.assertEqual(DELETE_EMOJIS, {POO, MONKEY})
        self.assertEqual(POO, "💩")
        self.assertEqual(MONKEY, "🙉")
        self.assertEqual(FOLDED, "🙏")
        self.assertEqual(THUMBS_UP, "👍")

    def test_exit_keyboard_has_three_choices(self) -> None:
        markup = exit_edit_keyboard(9)
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertEqual(labels, ["Commit to library", "Save draft", "Cancel"])
        self.assertEqual(data, ["r9:library", "r9:draft", "r9:cancel"])

    def test_parse_react_callback(self) -> None:
        self.assertEqual(parse_react_callback("r12:yes"), (12, "yes"))
        self.assertEqual(parse_react_callback("r12:draft"), (12, "draft"))
        self.assertEqual(parse_react_callback("r12:library"), (12, "library"))
        self.assertEqual(parse_react_callback("r12:cancel"), (12, "cancel"))
        self.assertIsNone(parse_react_callback("p12:ok"))


class EditUiTests(unittest.TestCase):
    def test_parse_edit_callback(self) -> None:
        action = parse_edit_callback("e3:f:title")
        assert action is not None
        self.assertEqual(action.op, "field")
        self.assertEqual(action.field, "title")
        suggest = parse_edit_callback("e3:s:artist:1")
        assert suggest is not None
        self.assertEqual(suggest.op, "suggest")
        self.assertEqual(suggest.index, 1)
        cover = parse_edit_callback("e3:cv:2")
        assert cover is not None
        self.assertEqual(cover.op, "cover_pick")
        self.assertEqual(cover.index, 2)

    def test_strikethrough_changed_fields(self) -> None:
        old = TagSet(title="Old", artist="A", album="LP")
        new = TagSet(title="New", artist="A", album="LP")
        text = format_edit_card(old, new, header="<b>review</b>")
        self.assertIn("<s>Old</s> → <b>New</b>", text)
        self.assertIn("Artist: A", text)
        self.assertNotIn("<s>A</s>", text)
        cleared = format_edit_card(old, TagSet(title="Old", artist="", album="LP"), header="x")
        self.assertIn("<s>A</s> → —", cleared)

    def test_suggestions_from_report(self) -> None:
        report = {
            "file_tags": {"title": "File Title", "artist": "File Artist", "genre": "edm"},
            "filename": {"stem": "stem-title"},
            "musicbrainz": {"title": "MB Title", "artist": "MB Artist", "genre_tags": ["ncs"]},
            "itunes": {"title": "iTunes Title", "genre": "Electronic"},
            "lastfm_tags": ["ncs", "happy"],
        }
        titles = suggestions_for(report, "title")
        self.assertIn("File Title", titles)
        self.assertIn("MB Title", titles)
        self.assertIn("stem-title", titles)
        mapper = GenreMapper(Path("genre_map.yaml"))
        genres = suggestions_for(report, "genre", mapper)
        self.assertTrue(any("NCS" in item for item in genres))
        row = PendingReview(
            id=1,
            phase="react_edit",
            status="waiting",
            local_path="",
            sidecar_path=None,
            relative_path=None,
            kind="review",
            original_json="{}",
            recommended_json="{}",
            working_json='{"title":"old","album":"","artist":"","albumartist":"","composer":"","genre":"","date":"","tracknumber":"","discnumber":"","lyrics":""}',
            candidates_json="{}",
            identity_json="{}",
            source_report_json='{"edit_suggestions":["File Title","MB Title"]}',
            drive_conflicts_json="[]",
            drive_root_id=None,
            chat_id=1,
            thread_id=None,
            status_message_id=1,
            topic_name="English",
            file_name="a.flac",
            track_id=1,
            replace_id=None,
            old_drive_id=None,
            source_drive_file_id=None,
            source_drive_sidecar_id=None,
            telegram_file_id=None,
            created_at="",
            expires_at="",
        )
        updated = apply_suggestion(row, "title", 1)
        assert updated is not None
        self.assertIn("MB Title", updated)


class GenreNcsTests(unittest.TestCase):
    def test_ncs_aliases(self) -> None:
        mapper = GenreMapper(Path("genre_map.yaml"))
        self.assertIn("NCS", mapper.classify(["ncs"]))
        self.assertIn("NCS", mapper.classify(["No Copyright Sounds"]))
        self.assertIn("NCS", mapper.classify(["nocopyrightsounds"]))


class CatalogMessageBindTests(unittest.TestCase):
    def test_bind_lookup_and_expire_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="review",
                mb_recording_id=None,
                acoustid=None,
                local_path="/tmp/a.flac",
                sidecar_path=None,
                relative_path="2026-08-16/a.flac",
                bit_depth=16,
                sample_rate=44100,
                title="Song",
                artist="Artist",
                album="Album",
                status="uploaded",
                telegram_file_id="tg-1",
                topic_name="English",
                file_name="a.flac",
            )
            catalog.bind_track_message(track_id, -100, 55)
            catalog.bind_track_message(track_id, -100, 99)
            found = catalog.get_track_by_message(-100, 55)
            again = catalog.get_track_by_message(-100, 99)
            self.assertIsNotNone(found)
            self.assertIsNotNone(again)
            assert found is not None
            assert again is not None
            self.assertEqual(found.id, track_id)
            self.assertEqual(again.id, track_id)
            self.assertEqual(found.telegram_file_id, "tg-1")
            pending_id = catalog.insert_pending_review(
                phase="react_edit",
                local_path="/tmp/stage.flac",
                sidecar_path=None,
                relative_path="2026-08-16/a.flac",
                kind="review",
                original_json="{}",
                recommended_json="{}",
                working_json="{}",
                candidates_json="{}",
                identity_json="{}",
                source_report_json="{}",
                chat_id=-100,
                thread_id=1,
                status_message_id=56,
                topic_name="English",
                file_name="a.flac",
                track_id=track_id,
                expires_at="2000-01-01T00:00:00+00:00",
            )
            expired = catalog.claim_expired_pending()
            self.assertEqual(expired, [])
            still = catalog.get_pending_review(pending_id)
            assert still is not None
            self.assertEqual(still.status, "waiting")
            confirm_id = catalog.insert_pending_review(
                phase="react_confirm",
                local_path="/tmp/a.flac",
                sidecar_path=None,
                relative_path="2026-08-16/a.flac",
                kind="review",
                original_json="{}",
                recommended_json="{}",
                working_json="{}",
                candidates_json='{"op":"library"}',
                identity_json="{}",
                source_report_json="{}",
                chat_id=-100,
                thread_id=1,
                status_message_id=57,
                topic_name="English",
                file_name="a.flac",
                track_id=track_id,
                expires_at="2000-01-01T00:00:00+00:00",
            )
            catalog.update_pending_review(pending_id, status="cancelled")
            expired = catalog.claim_expired_pending()
            self.assertEqual(expired, [])
            confirm = catalog.get_pending_review(confirm_id)
            assert confirm is not None
            self.assertEqual(confirm.status, "waiting")
            reviews = catalog.list_review_tracks()
            self.assertEqual(len(reviews), 1)
            catalog.close()


class PathHelperTests(unittest.TestCase):
    def test_library_and_review_relative(self) -> None:
        tags = TagSet(title="Go", artist="A", albumartist="A", album="LP", tracknumber="1")
        lib = library_relative("English", tags)
        self.assertEqual(lib.parts[0], "English")
        self.assertTrue(str(lib).endswith(".flac"))
        rev = review_relative("song.flac")
        self.assertEqual(rev.name, "song.flac")
        self.assertEqual(len(rev.parts), 2)


class ReviewLabelTests(unittest.TestCase):
    def test_artist_album_title(self) -> None:
        track = SimpleNamespace(
            title="Go", artist="Artist", album="LP", file_name="x.flac", relative_path=None
        )
        self.assertEqual(review_label(track), "Artist — LP — Go")

    def test_empty_fields_use_filename_stem(self) -> None:
        track = SimpleNamespace(
            title="", artist="", album="", file_name="song-name.flac", relative_path=None
        )
        self.assertEqual(review_label(track), "song-name — song-name — song-name")


class EnsureLocalFlacTests(unittest.IsolatedAsyncioTestCase):
    async def test_insert_react_pending_without_explicit_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            pending_id = catalog.insert_pending_review(
                phase="react_edit",
                local_path="/tmp/stage.flac",
                relative_path="English/A/LP/a.flac",
                kind="library",
                original_json="{}",
                recommended_json="{}",
                working_json="{}",
                candidates_json="{}",
                identity_json="{}",
                source_report_json="{}",
                chat_id=-100,
                thread_id=1,
                status_message_id=10,
                topic_name="English",
                file_name="a.flac",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            row = catalog.get_pending_review(pending_id)
            assert row is not None
            self.assertIsNone(row.sidecar_path)
            catalog.close()

    async def test_uses_library_copy_when_catalog_path_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "library"
            dest = library / "English" / "A" / "LP" / "a.flac"
            dest.parent.mkdir(parents=True)
            dest.write_bytes(b"flac")
            catalog = Catalog(root / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path=str(root / "gone.flac"),
                sidecar_path=None,
                relative_path="English/A/LP/a.flac",
                bit_depth=16,
                sample_rate=44100,
                title="Go",
                artist="A",
                album="LP",
                status="uploaded",
                drive_file_id="drive-1",
            )
            track = catalog.get_track(track_id)
            assert track is not None
            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    library_root=library,
                    review_root=root / "review",
                    pending_root=root / "pending",
                    gdrive_folder_id="lib",
                    gdrive_review_folder_id="rev",
                ),
                catalog=catalog,
                drive=SimpleNamespace(),
            )
            path = await ensure_local_flac(ctx, track)
            self.assertEqual(path, dest)
            catalog.close()

    async def test_downloads_from_drive_when_local_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = Catalog(root / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path=str(root / "gone.flac"),
                sidecar_path=None,
                relative_path="English/A/LP/a.flac",
                bit_depth=16,
                sample_rate=44100,
                title="Go",
                artist="A",
                album="LP",
                status="uploaded",
                drive_file_id="drive-1",
            )
            track = catalog.get_track(track_id)
            assert track is not None
            downloaded: list[str] = []

            class Drive:
                def download_to(self, file_id: str, dest: Path) -> Path:
                    downloaded.append(file_id)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(b"flac")
                    return dest

            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    library_root=root / "library",
                    review_root=root / "review",
                    pending_root=root / "pending",
                    gdrive_folder_id="lib",
                    gdrive_review_folder_id="rev",
                ),
                catalog=catalog,
                drive=Drive(),
            )
            path = await ensure_local_flac(ctx, track)
            self.assertEqual(downloaded, ["drive-1"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), b"flac")
            refreshed = catalog.get_track(track_id)
            assert refreshed is not None
            self.assertEqual(refreshed.local_path, str(path))
            catalog.close()

    async def test_resolves_drive_id_from_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = Catalog(root / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="review",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="2026-08-16/a.flac",
                bit_depth=None,
                sample_rate=None,
                title="Go",
                artist="A",
                album="LP",
                status="uploaded",
                drive_file_id=None,
            )
            track = catalog.get_track(track_id)
            assert track is not None

            class Drive:
                def __init__(self) -> None:
                    self.parts: list[str] | None = None

                def find_path(self, root_id: str, folder_parts: list[str]) -> str:
                    self.parts = folder_parts
                    return "parent"

                def find_name_conflicts(self, parent_id: str, filename: str):
                    return [SimpleNamespace(id="found-id")]

                def download_to(self, file_id: str, dest: Path) -> Path:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(b"flac")
                    return dest

            drive = Drive()
            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    library_root=root / "library",
                    review_root=root / "review",
                    pending_root=root / "pending",
                    gdrive_folder_id="lib",
                    gdrive_review_folder_id="rev",
                ),
                catalog=catalog,
                drive=drive,
            )
            path = await ensure_local_flac(ctx, track)
            self.assertEqual(drive.parts, ["2026-08-16"])
            self.assertTrue(path.is_file())
            refreshed = catalog.get_track(track_id)
            assert refreshed is not None
            self.assertEqual(refreshed.drive_file_id, "found-id")
            catalog.close()

    async def test_concurrent_downloads_share_one_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = Catalog(root / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="review",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="2026-08-16/a.flac",
                bit_depth=None,
                sample_rate=None,
                title="Go",
                artist="A",
                album="LP",
                status="uploaded",
                drive_file_id="drive-1",
            )
            track = catalog.get_track(track_id)
            assert track is not None
            calls: list[str] = []

            class Drive:
                def download_to(self, file_id: str, dest: Path) -> Path:
                    calls.append(file_id)
                    time.sleep(0.05)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(b"flac")
                    return dest

            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    library_root=root / "library",
                    review_root=root / "review",
                    pending_root=root / "pending",
                    gdrive_folder_id="lib",
                    gdrive_review_folder_id="rev",
                ),
                catalog=catalog,
                drive=Drive(),
            )
            first, second = await asyncio.gather(ensure_local_flac(ctx, track), ensure_local_flac(ctx, track))
            self.assertEqual(calls, ["drive-1"])
            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            catalog.close()
