from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.formats import (
    SavePrefs,
    default_filename,
    detect_format,
    is_allowed_audio_message,
    is_known_audio_message,
    mime_for_path,
    normalize_allowed,
    normalize_save_dest,
    notify_group_save_enabled,
    save_prefs_from_settings,
    stored_download_name,
    suffix_for,
    user_allowed_formats,
    user_save_prefs,
)
from app.library import review_relative
from app.models import TagSet
from app.suggest import parse_library_relative
from app.tags import build_filename


class FormatRegistryTests(unittest.TestCase):
    def test_detect_name_and_mime(self) -> None:
        self.assertEqual(detect_format("a.mp3", "audio/mpeg"), "mp3")
        self.assertEqual(detect_format("", "audio/x-flac"), "flac")
        self.assertEqual(detect_format("x.WAV", ""), "wav")
        self.assertEqual(detect_format("clip.m4a", "audio/mp4"), "m4a")
        self.assertEqual(detect_format("x.opus", ""), "opus")
        self.assertEqual(detect_format("x.ogg", ""), "ogg")
        self.assertIsNone(detect_format("notes.pdf", "application/pdf"))

    def test_normalize_allowed(self) -> None:
        self.assertEqual(normalize_allowed(None), ["flac", "mp3"])
        self.assertEqual(normalize_allowed([]), ["flac", "mp3"])
        self.assertEqual(normalize_allowed(["wav", "FLAC", "bogus", "wav"]), ["flac", "wav"])
        self.assertEqual(normalize_allowed("mpeg"), ["mp3"])

    def test_user_allowed_defaults(self) -> None:
        ctx = SimpleNamespace(catalog=SimpleNamespace(get_user=lambda _uid: None))
        self.assertEqual(user_allowed_formats(ctx, 1), ["flac", "mp3"])
        user = SimpleNamespace(settings_json=json.dumps({"allowed_formats": ["ogg"]}))
        ctx = SimpleNamespace(catalog=SimpleNamespace(get_user=lambda _uid: user))
        self.assertEqual(user_allowed_formats(ctx, 9), ["ogg"])

    def test_save_prefs(self) -> None:
        self.assertEqual(normalize_save_dest("LIBRARY"), "library")
        self.assertEqual(normalize_save_dest("nope"), "none")
        prefs = save_prefs_from_settings(
            {
                "default_dest": "review",
                "correct_telegram": "on",
                "skip_save_prompt": True,
                "delete_original": "yes",
            }
        )
        self.assertEqual(
            prefs,
            SavePrefs(dest="review", correct_telegram=True, skip_save_prompt=True, delete_original=True),
        )
        self.assertEqual(user_save_prefs(None), SavePrefs())
        user = SimpleNamespace(settings_json="{not json")
        self.assertEqual(user_save_prefs(user), SavePrefs())
        self.assertTrue(notify_group_save_enabled({}))
        self.assertTrue(notify_group_save_enabled({"notify_group_save": True}))
        self.assertFalse(notify_group_save_enabled({"notify_group_save": False}))
        self.assertFalse(notify_group_save_enabled({"notify_group_save": "off"}))

    def test_message_allowlist(self) -> None:
        mp3 = SimpleNamespace(
            document=SimpleNamespace(file_name="a.mp3", mime_type="audio/mpeg"),
            audio=None,
        )
        wav = SimpleNamespace(
            document=None,
            audio=SimpleNamespace(file_name="x.wav", mime_type="audio/wav"),
        )
        self.assertTrue(is_known_audio_message(mp3))
        self.assertTrue(is_allowed_audio_message(mp3, ["flac", "mp3"]))
        self.assertFalse(is_allowed_audio_message(mp3, ["flac"]))
        self.assertFalse(is_allowed_audio_message(wav, ["flac", "mp3"]))
        self.assertTrue(is_allowed_audio_message(wav, ["wav"]))

    def test_paths_and_mime(self) -> None:
        self.assertEqual(default_filename("audio/mpeg"), "track.mp3")
        self.assertEqual(stored_download_name("My Song.MP3"), "My Song.mp3")
        self.assertEqual(suffix_for("a.wav", "ignored.flac"), ".wav")
        self.assertEqual(mime_for_path("a.mp3"), "audio/mpeg")
        self.assertEqual(mime_for_path("a.flac"), "audio/flac")
        self.assertTrue(str(review_relative("cut.mp3")).endswith(".mp3"))
        name = build_filename(TagSet(title="Go", albumartist="A", tracknumber="1"), ext=".mp3")
        self.assertTrue(name.endswith(".mp3"))
        seed = parse_library_relative("English/A/LP/A - 01 - Go.mp3")
        self.assertIsNotNone(seed)
        self.assertEqual(seed.title, "Go")
        self.assertIsNone(parse_library_relative("English/A/LP/notes.txt"))
