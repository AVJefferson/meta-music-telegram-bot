from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from app.errors import AppError
from app.hifi import is_nav_label, strip_hifi_keyboard
from app.membership import ignore_bot_update, message_from_bot, user_is_bot
from app.oauth import public_base_url
from app.paths import cache_path
from app.relocate import user_copy_of
from tests.support import make_ctx, temp_catalog


class CatalogIsolationTests(unittest.TestCase):
    def test_library_rows_scoped_by_user(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            a = catalog.insert_pending(
                kind="library",
                mb_recording_id="mb-a",
                acoustid=None,
                local_path="/tmp/a.flac",
                sidecar_path=None,
                relative_path="En/A/a.flac",
                bit_depth=16,
                sample_rate=44100,
                title="A",
                artist="Art",
                album="Al",
                status="uploaded",
                user_id=10,
            )
            catalog.insert_pending(
                kind="library",
                mb_recording_id="mb-b",
                acoustid=None,
                local_path="/tmp/b.flac",
                sidecar_path=None,
                relative_path="En/B/b.flac",
                bit_depth=16,
                sample_rate=44100,
                title="B",
                artist="Art",
                album="Al",
                status="uploaded",
                user_id=11,
            )
            self.assertEqual([t.id for t in catalog.list_library_tracks(10)], [a])
            self.assertEqual([t.title for t in catalog.list_library_tracks(11)], ["B"])
            self.assertIsNone(catalog.find_library_by_mbid("mb-a", 11))
            self.assertIsNotNone(catalog.find_library_by_mbid("mb-a", 10))

    def test_admin_cannot_block_self_is_enforced_in_catalog_blacklist(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.blacklist_user(99)
            self.assertTrue(catalog.is_user_blacklisted(99))
            catalog.unblacklist_user(99)
            self.assertFalse(catalog.is_user_blacklisted(99))

    def test_cache_path_rejects_traversal(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog, cache_root=Path(directory.name) / "cache")
            with self.assertRaises(AppError):
                cache_path(ctx, "../etc/passwd")
            with self.assertRaises(AppError):
                cache_path(ctx, "not-a-hash")

    def test_hifi_picks_are_user_scoped(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.put_hifi_pick("p1", 1, "s1", "RAW-HIFI", "Song", "2099-01-01T00:00:00+00:00")
            self.assertIsNone(catalog.get_hifi_pick("p1", 2))
            found = catalog.get_hifi_pick("p1", 1)
            self.assertIsNotNone(found)
            self.assertEqual(found[1], "RAW-HIFI")

    def test_pending_waiting_per_user(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            track = catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path="/tmp/x.flac",
                sidecar_path=None,
                relative_path="x.flac",
                bit_depth=None,
                sample_rate=None,
                title="t",
                artist="a",
                album="b",
                user_id=1,
            )
            catalog.insert_pending_review(
                phase="react_confirm",
                local_path="/tmp/x.flac",
                sidecar_path=None,
                relative_path=None,
                kind="library",
                original_json="{}",
                recommended_json="{}",
                working_json="{}",
                candidates_json='{"op":"library"}',
                identity_json="{}",
                source_report_json="{}",
                chat_id=-100,
                thread_id=None,
                status_message_id=1,
                topic_name="General",
                file_name="x.flac",
                track_id=track,
                expires_at="2099-01-01T00:00:00+00:00",
                user_id=1,
            )
            self.assertIsNone(catalog.get_waiting_for_track(track, 2))
            self.assertIsNotNone(catalog.get_waiting_for_track(track, 1))

    def test_user_copy_of_uses_sha_then_mbid(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            sha = "b" * 64
            theirs = catalog.insert_pending(
                kind="library",
                mb_recording_id="mb-x",
                acoustid=None,
                local_path="/tmp/t.flac",
                sidecar_path=None,
                relative_path="t.flac",
                bit_depth=None,
                sample_rate=None,
                title="t",
                artist="a",
                album="b",
                status="uploaded",
                user_id=1,
                audio_sha256=sha,
            )
            mine = catalog.insert_pending(
                kind="review",
                mb_recording_id="mb-x",
                acoustid=None,
                local_path="/tmp/m.flac",
                sidecar_path=None,
                relative_path="m.flac",
                bit_depth=None,
                sample_rate=None,
                title="t",
                artist="a",
                album="b",
                status="uploaded",
                user_id=2,
                audio_sha256=sha,
            )
            ctx = make_ctx(catalog)
            track = catalog.get_track(theirs)
            found = user_copy_of(ctx, track, 2)
            self.assertIsNotNone(found)
            self.assertEqual(found.id, mine)
            self.assertIsNone(user_copy_of(ctx, track, 3))


class PublicBaseUrlTests(unittest.TestCase):
    def test_http_rejected_except_localhost(self) -> None:
        from app.errors import AppError

        with self.assertRaises(AppError):
            public_base_url(SimpleNamespace(public_base_url="http://evil.example"))
        self.assertEqual(public_base_url(SimpleNamespace(public_base_url="http://localhost:8080")), "http://localhost:8080")
        self.assertEqual(public_base_url(SimpleNamespace(public_base_url="https://ok.example/")), "https://ok.example")


class WebAppKeyboardTests(unittest.TestCase):
    def test_http_localhost_falls_back_to_url_button(self) -> None:
        from app.user_cmd import _web_or_url_keyboard

        ctx = SimpleNamespace(settings=SimpleNamespace(public_base_url="http://localhost:8080"))
        ticket = "http://localhost:8080/oauth/start?token=abc"
        kb = _web_or_url_keyboard(ctx, "login", ticket, "Connect Google Drive")
        self.assertIsNotNone(kb)
        btn = kb.inline_keyboard[0][0]
        self.assertIsNone(btn.web_app)
        self.assertEqual(btn.url, ticket)

    def test_https_uses_webapp_button(self) -> None:
        from app.user_cmd import _web_or_url_keyboard

        ctx = SimpleNamespace(settings=SimpleNamespace(public_base_url="https://music.example"))
        kb = _web_or_url_keyboard(ctx, "login", "https://music.example/oauth/start?token=abc", "Connect")
        btn = kb.inline_keyboard[0][0]
        self.assertEqual(btn.web_app.url, "https://music.example/app/login")
        self.assertIsNone(btn.url)

    def test_http_without_fallback_has_no_keyboard(self) -> None:
        from app.user_cmd import _web_or_url_keyboard

        ctx = SimpleNamespace(settings=SimpleNamespace(public_base_url="http://127.0.0.1:8080"))
        self.assertIsNone(_web_or_url_keyboard(ctx, "settings", None, "Open settings"))


class HifiStripTests(unittest.TestCase):
    def test_strips_next_prev(self) -> None:
        rows = [
            [SimpleNamespace(text="Song A", data="cbA"), SimpleNamespace(text="Next", data="next")],
            [SimpleNamespace(text="Prev", data="prev")],
        ]
        picks = strip_hifi_keyboard(rows)
        self.assertEqual(picks, [("Song A", "cbA")])
        self.assertTrue(is_nav_label("➡️ Next"))


class BotOriginTests(unittest.TestCase):
    def test_other_bot_message_is_ignored(self) -> None:
        bot = SimpleNamespace(id=99)
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=55, is_bot=True),
            via_bot=None,
            sender_business_bot=None,
        )
        self.assertTrue(user_is_bot(msg.from_user, bot))
        self.assertTrue(message_from_bot(msg, bot))
        self.assertTrue(ignore_bot_update(SimpleNamespace(message=msg, my_chat_member=None), bot))

    def test_self_echo_ignored_even_if_is_bot_false(self) -> None:
        bot = SimpleNamespace(id=99)
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=99, is_bot=False),
            via_bot=None,
            sender_business_bot=None,
        )
        self.assertTrue(message_from_bot(msg, bot))

    def test_human_flac_not_ignored(self) -> None:
        bot = SimpleNamespace(id=99)
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=7, is_bot=False),
            via_bot=None,
            sender_business_bot=None,
        )
        self.assertFalse(message_from_bot(msg, bot))
        self.assertFalse(ignore_bot_update(SimpleNamespace(message=msg, my_chat_member=None), bot))

    def test_own_inline_via_bot_media_ignored(self) -> None:
        bot = SimpleNamespace(id=99)
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=7, is_bot=False),
            via_bot=SimpleNamespace(id=99, is_bot=True),
            sender_business_bot=None,
            document=SimpleNamespace(file_id="f"),
            audio=None,
            voice=None,
            video=None,
            video_note=None,
            sticker=None,
        )
        self.assertTrue(message_from_bot(msg, bot))

    def test_own_inline_via_bot_text_allowed(self) -> None:
        bot = SimpleNamespace(id=99)
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=7, is_bot=False),
            via_bot=SimpleNamespace(id=99, is_bot=True),
            sender_business_bot=None,
            document=None,
            audio=None,
            voice=None,
            video=None,
            video_note=None,
            sticker=None,
        )
        self.assertFalse(message_from_bot(msg, bot))

    def test_channel_post_from_bot_ignored(self) -> None:
        bot = SimpleNamespace(id=99)
        post = SimpleNamespace(
            from_user=SimpleNamespace(id=55, is_bot=True),
            via_bot=None,
            sender_business_bot=None,
        )
        update = SimpleNamespace(
            message=None,
            edited_message=None,
            channel_post=post,
            edited_channel_post=None,
            business_message=None,
            callback_query=None,
            message_reaction=None,
            my_chat_member=None,
        )
        self.assertTrue(ignore_bot_update(update, bot))

    def test_my_chat_member_from_bot_still_processed(self) -> None:
        update = SimpleNamespace(
            message=None,
            edited_message=None,
            channel_post=None,
            edited_channel_post=None,
            business_message=None,
            callback_query=None,
            message_reaction=None,
            my_chat_member=SimpleNamespace(from_user=SimpleNamespace(id=55, is_bot=True)),
        )
        self.assertFalse(ignore_bot_update(update, SimpleNamespace(id=99)))


class UserSinceFormatTests(unittest.TestCase):
    def test_iso_to_yy_mm_dd(self) -> None:
        from app.admin_cmd import format_since_date

        self.assertEqual(format_since_date("2026-09-13T18:57:07+00:00"), "26-09-13")
        self.assertEqual(format_since_date("2026-09-13T18:57:07"), "26-09-13")
        self.assertEqual(format_since_date(""), "?")


class MentionBotTests(unittest.TestCase):
    def test_other_user_mention_ignored(self) -> None:
        from app.user_cmd import mentions_bot

        me = SimpleNamespace(id=99, username="MetaMusicProBot")
        msg = SimpleNamespace(
            text="@alice hello",
            entities=[SimpleNamespace(type="mention", offset=0, length=6)],
        )
        self.assertFalse(mentions_bot(msg, me))

    def test_bot_username_in_text(self) -> None:
        from app.user_cmd import mentions_bot

        me = SimpleNamespace(id=99, username="MetaMusicProBot")
        msg = SimpleNamespace(text="@MetaMusicProBot song name", entities=[])
        self.assertTrue(mentions_bot(msg, me))


class InlineSearchResultTests(unittest.TestCase):
    def test_empty_query_no_results(self) -> None:
        from app.user_cmd import inline_search_results

        self.assertEqual(inline_search_results("  "), [])

    def test_sends_get_command(self) -> None:
        from app.user_cmd import inline_search_results

        rows = inline_search_results("  neon  lights ")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].input_message_content.message_text, "/get neon lights")
        self.assertTrue(rows[0].title.startswith("Search "))


class SearchDisabledTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_search_skips_hifi(self) -> None:
        from unittest.mock import AsyncMock, patch

        from app.user_cmd import SEARCH_DISABLED_TEXT, SEARCH_ENABLED, _start_search

        self.assertFalse(SEARCH_ENABLED)
        sent: list[str] = []

        async def fake_send(ctx, **kwargs):
            sent.append(kwargs["text"])

        ctx = SimpleNamespace(bot=SimpleNamespace(id=1), catalog=SimpleNamespace())
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=9, is_bot=False),
            chat=SimpleNamespace(id=9),
        )
        with patch("app.user_cmd.send_private", fake_send), patch(
            "app.hifi.search_songs", AsyncMock()
        ) as search:
            await _start_search(ctx, msg, "neon")
        search.assert_not_called()
        self.assertEqual(sent, [SEARCH_DISABLED_TEXT])
