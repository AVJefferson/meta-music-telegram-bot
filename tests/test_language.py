from __future__ import annotations

import unittest
from pathlib import Path

from app.genre import GenreMapper, language_name_from_iso
from app.identify import mb_language_names
from app.library import UNKNOWN_LANGUAGE_FOLDER, library_relative
from app.models import TagSet

MAP = Path(__file__).resolve().parent.parent / "genre_map.yaml"


def mapper() -> GenreMapper:
    return GenreMapper(MAP)


class LanguageDetectTests(unittest.TestCase):
    def test_languages_from_tags_first_seen_order(self) -> None:
        genre = mapper()
        langs = genre.languages_from_tags(
            ["malayalam songs", "tamil", "Malayalam", "songs in hindi", "french house"]
        )
        self.assertEqual(langs, ["Malayalam", "Tamil", "Hindi"])

    def test_phrase_and_not_substring(self) -> None:
        genre = mapper()
        self.assertEqual(genre.language_from_raw("Tamil song"), "Tamil")
        self.assertEqual(genre.language_from_raw("english music"), "English")
        self.assertIsNone(genre.language_from_raw("french house"))
        self.assertIsNone(genre.language_from_raw("spanish guitar"))
        self.assertIsNone(genre.language_from_raw("General"))

    def test_classify_keeps_every_language(self) -> None:
        text = mapper().classify(["pop", "malayalam songs", "tamil", "french house"])
        folded = text.casefold()
        self.assertIn("malayalam", folded)
        self.assertIn("tamil", folded)
        self.assertIn("pop", folded)
        self.assertNotIn("french", folded)

    def test_extra_language_ignores_forum_general(self) -> None:
        text = mapper().classify(["pop"], extra_language="General")
        self.assertNotIn("General", text)
        self.assertIn("pop", text.casefold())

    def test_iso_map(self) -> None:
        self.assertEqual(language_name_from_iso("mal"), "malayalam")
        self.assertEqual(language_name_from_iso("ENG"), "english")
        self.assertEqual(language_name_from_iso("fra"), "french")
        self.assertEqual(language_name_from_iso("fre"), "french")
        self.assertIsNone(language_name_from_iso("zxx"))
        self.assertIsNone(language_name_from_iso("und"))

    def test_mb_language_names(self) -> None:
        recording = {
            "work-relation-list": [{"work": {"id": "w", "language": "tam"}}],
        }
        release = {"text-representation": {"language": "mal"}}
        names = mb_language_names(recording, release, ["hin", "mal"])
        self.assertEqual(names, ["hindi", "malayalam", "tamil"])

    def test_library_folder_unknown_when_empty(self) -> None:
        tags = TagSet(title="Go", artist="A", albumartist="A", album="LP", tracknumber="1")
        path = library_relative("", tags)
        self.assertEqual(path.parts[0], UNKNOWN_LANGUAGE_FOLDER)
