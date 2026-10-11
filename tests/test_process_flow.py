from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.audio_in import download_telegram, progress_due
from app.captions import song_card
from app.models import TagSet
from app.prefs import ProcessPrefs, format_settings_text, process_prefs_from_settings
from app.rate_limit import reset as reset_rates
from app.rate_limit import retry_after
from app.review_ui import cover_keyboard
from app.tags import AudioMetrics
from app.user_cmd import format_start_text


class CoverConfirmTests(unittest.TestCase):
    def test_source_tap_is_selection_and_confirm_commits(self) -> None:
        keyboard = cover_keyboard(4, [{"label": "File", "width": 1200, "height": 1200}], selected=0)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("● File (1200x1200)", labels)
        self.assertIn("Confirm", labels)
        self.assertIn("p4:cv0", data)
        self.assertIn("p4:ok", data)
        self.assertIn("p4:cancel", data)


class RemoteDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_reports_each_quarter(self) -> None:
        chunks = [b"a" * 25, b"b" * 25, b"c" * 25, b"d" * 25]

        class _Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not chunks:
                    raise StopAsyncIteration
                return chunks.pop(0)

            async def aclose(self):
                return None

        class _Api:
            def file_url(self, token, path):
                return f"https://example.test/{token}/{path}"

        class _Session:
            api = _Api()

            def stream_content(self, **kwargs):
                del kwargs
                return _Stream()

        class _Bot:
            token = "t"
            session = _Session()

        seen: list[int] = []
        clock = {"t": 0.0}

        def _mono():
            clock["t"] += 6
            return clock["t"]

        async def _progress(pct: int) -> None:
            seen.append(pct)

        async def _stop() -> bool:
            return False

        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "song.flac"
            file = SimpleNamespace(file_path="files/song.flac", file_size=100)
            with patch("app.audio_in.time.monotonic", _mono):
                await download_telegram(_Bot(), file, dest, _progress, _stop)
            self.assertEqual(dest.read_bytes(), b"a" * 25 + b"b" * 25 + b"c" * 25 + b"d" * 25)
        self.assertEqual(seen, [25, 50, 75, 100])


class IdentifyPaceTests(unittest.TestCase):
    def test_full_window_returns_wait_and_does_not_consume(self) -> None:
        reset_rates()
        self.assertEqual(retry_after("identify", 0, 2), 0.0)
        self.assertEqual(retry_after("identify", 0, 2), 0.0)
        delay = retry_after("identify", 0, 2)
        self.assertGreater(delay, 0)
        again = retry_after("identify", 0, 2)
        self.assertGreater(again, 0)


class ProgressTests(unittest.TestCase):
    def test_progress_waits_for_both_gates(self) -> None:
        self.assertFalse(progress_due(4.9, 25))
        self.assertFalse(progress_due(5.0, 24))
        self.assertTrue(progress_due(5.0, 25))
        self.assertTrue(progress_due(20.0, 50))


class SongCardTests(unittest.TestCase):
    def test_song_card_matches_process_layout(self) -> None:
        text = song_card(
            TagSet(
                title="Teardrop",
                album="Mezzanine",
                artist="Massive Attack",
                albumartist="Massive Attack",
                composer="Massive Attack",
                date="1998-04-27",
                genre="Trip-hop | electronic",
            ),
            AudioMetrics(290, 16, 44100, 86, "FLAC", 2, 34_300_000),
        )
        self.assertIn("<i>Teardrop</i>", text)
        self.assertIn("Album: Mezzanine", text)
        self.assertIn("Album Artist: Massive Attack", text)
        self.assertIn("Composers: Massive Attack", text)
        self.assertIn("Year: 1998", text)
        self.assertIn("FLAC | 04:50 | 34.30MB | 86kbps | 44.1KHz | 16bit | 2CH", text)


class PrefTests(unittest.TestCase):
    def test_empty_settings_use_process_defaults(self) -> None:
        prefs = process_prefs_from_settings({})
        self.assertEqual(
            prefs,
            ProcessPrefs(
                save="ask",
                auto_forward=(),
                correct="yes",
                delete="no",
                notifications="all",
                channel="current",
                formats=("flac", "mp3", "m4a", "wav"),
            ),
        )
        text = format_settings_text(email=None, logged_in=False, prefs=prefs)
        self.assertIn("Default Save: Ask each time", text)
        self.assertIn("Correct telegram file: Yes", text)
        self.assertIn("<code>/login</code>", text)

    def test_start_lists_admin_commands_and_processed_count(self) -> None:
        user = SimpleNamespace(
            telegram_user_id=7,
            first_seen_at="2026-09-30T00:00:00+00:00",
            songs_edited=10,
            songs_processed=100,
            google_email="user@example.com",
        )
        text = format_start_text(
            user, username="username", prefs_logged_in=True, admin=True, days=90
        )
        self.assertIn("Hi @username", text)
        self.assertIn("User Since 2026 Sep 30", text)
        self.assertIn("Songs Processed: 100 Songs Edited: 10", text)
        self.assertIn("/block", text)
        self.assertIn("inactive for 90 days", text)


class LastfmCoverTests(unittest.TestCase):
    def test_picks_largest_album_image(self) -> None:
        from app.enrich import lastfm_image_url

        url = lastfm_image_url(
            {
                "album": {
                    "image": [
                        {"#text": "https://img.example/small.jpg", "size": "small"},
                        {"#text": "https://img.example/mega.jpg", "size": "mega"},
                        {"#text": "https://img.example/large.jpg", "size": "large"},
                    ]
                }
            }
        )
        self.assertEqual(url, "https://img.example/mega.jpg")
        self.assertEqual(lastfm_image_url({}), "")

    def test_discogs_prefers_matching_album_cover(self) -> None:
        from app.enrich import discogs_cover_url

        url = discogs_cover_url(
            {
                "results": [
                    {"title": "Other - Live", "cover_image": "https://img.example/other.jpg"},
                    {"title": "Massive Attack - Mezzanine", "cover_image": "https://img.example/mezz.jpg"},
                ]
            },
            "Mezzanine",
        )
        self.assertEqual(url, "https://img.example/mezz.jpg")
        self.assertEqual(discogs_cover_url({}, "Mezzanine"), "")


class SonglogShaTests(unittest.TestCase):
    def test_reads_header_hash(self) -> None:
        from app.library_index import songlog_sha256

        text = "file: a.flac\nsha256: AbC\n\n== file tags ==\nsha256: no\n"
        self.assertEqual(songlog_sha256(text), "abc")
        self.assertEqual(songlog_sha256("== file tags ==\nsha256: no\n"), "")
