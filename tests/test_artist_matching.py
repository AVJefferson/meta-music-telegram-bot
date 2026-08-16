from __future__ import annotations

import unittest

from app.models import TagSet
from app.review_ui import bulk_choice, format_summary, review_keyboard
from app.tags import fill_sparse_tags, normalize_tagset
from app.util import (
    diff_credit_html,
    format_artist_list,
    normalize_name_list,
    same_artist_names,
    split_artist_field,
)


def _toggle_fields(keyboard) -> set[str]:
    return {
        button.callback_data.split(":t:", 1)[1]
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and ":t:" in button.callback_data
    }


class SplitTests(unittest.TestCase):
    def test_separators(self) -> None:
        self.assertEqual(split_artist_field("A, B & C"), ["A", "B", "C"])
        self.assertEqual(split_artist_field("A; B"), ["A", "B"])
        self.assertEqual(split_artist_field("A / B"), ["A", "B"])
        self.assertEqual(split_artist_field("A feat. B"), ["A", "B"])
        self.assertEqual(split_artist_field("A ft B"), ["A", "B"])
        self.assertEqual(split_artist_field("A featuring B"), ["A", "B"])

    def test_unspaced_slash_stays_one_name(self) -> None:
        self.assertEqual(split_artist_field("AC/DC"), ["AC/DC"])
        self.assertEqual(split_artist_field("AC/DC & B"), ["AC/DC", "B"])


class SameArtistNamesTests(unittest.TestCase):
    def test_reordered_lists_match(self) -> None:
        self.assertTrue(same_artist_names("A, B & C", "C, B & A"))
        self.assertTrue(same_artist_names("A; B", "B & A"))
        self.assertTrue(same_artist_names("A feat. B", "A & B"))

    def test_case_and_punctuation_ignored(self) -> None:
        self.assertTrue(same_artist_names("a. r. rahman", "A R Rahman"))

    def test_extra_or_missing_name_differs(self) -> None:
        self.assertFalse(same_artist_names("A & B", "A, B & C"))
        self.assertFalse(same_artist_names("A & B", ""))


class ReviewUiTests(unittest.TestCase):
    def test_reordered_artist_emits_no_toggle(self) -> None:
        original = {"title": "Song", "artist": "B & A", "albumartist": "B & A"}
        recommended = {"title": "Song", "artist": "A & B", "albumartist": "A & B"}
        keyboard = review_keyboard(1, original, recommended, recommended)
        self.assertEqual(_toggle_fields(keyboard), set())

    def test_real_difference_still_emits_toggle(self) -> None:
        original = {"title": "Old title", "artist": "B & A"}
        recommended = {"title": "New title", "artist": "A & B"}
        keyboard = review_keyboard(1, original, recommended, recommended)
        self.assertEqual(_toggle_fields(keyboard), {"title"})

    def test_summary_marks_order_only_field_as_reordered(self) -> None:
        original = {"title": "Song", "artist": "B & A"}
        recommended = {"title": "Song", "artist": "A & B"}
        summary = format_summary(original, recommended, recommended)
        self.assertIn("<b>Artist</b>: A &amp; B", summary)
        self.assertNotIn("(reordered)", summary)
        self.assertNotIn("file: B &amp; A", summary)

    def test_use_file_keeps_recommended_artist_order(self) -> None:
        original = {"title": "Old title", "artist": "B & A", "composer": "D; C"}
        recommended = {"title": "New title", "artist": "A & B", "composer": "C & D"}
        chosen = bulk_choice(original, recommended, use_file=True)
        self.assertEqual(chosen["title"], "Old title")
        self.assertEqual(chosen["artist"], "A & B")
        self.assertEqual(chosen["composer"], "C & D")

    def test_use_file_keeps_genuinely_different_artist(self) -> None:
        original = {"artist": "A & B"}
        recommended = {"artist": "A, B & C"}
        self.assertEqual(bulk_choice(original, recommended, use_file=True)["artist"], "A & B")

    def test_use_recommended_takes_recommended(self) -> None:
        original = {"title": "Old title", "artist": "B & A"}
        recommended = {"title": "New title", "artist": "A & B"}
        chosen = bulk_choice(original, recommended, use_file=False)
        self.assertEqual(chosen, recommended)

    def test_sparse_empty_file_or_rec_emits_no_toggle(self) -> None:
        original = {"title": "Song", "composer": "Bach", "genre": "", "date": "1999"}
        recommended = {"title": "Song", "composer": "", "genre": "Rock", "date": ""}
        keyboard = review_keyboard(1, original, recommended, recommended)
        self.assertEqual(_toggle_fields(keyboard), set())

    def test_sparse_both_nonempty_still_emits_toggle(self) -> None:
        original = {"title": "Song", "genre": "Jazz", "date": "1998"}
        recommended = {"title": "Song", "genre": "Rock", "date": "1999"}
        keyboard = review_keyboard(1, original, recommended, recommended)
        self.assertEqual(_toggle_fields(keyboard), {"genre", "year"})

    def test_artist_empty_vs_rec_still_emits_toggle(self) -> None:
        original = {"title": "Song", "artist": ""}
        recommended = {"title": "Song", "artist": "A"}
        keyboard = review_keyboard(1, original, recommended, recommended)
        self.assertEqual(_toggle_fields(keyboard), {"artist"})

    def test_summary_sparse_one_empty_is_single_line(self) -> None:
        original = {"title": "Song", "genre": "", "composer": "Bach"}
        recommended = {"title": "Song", "genre": "Rock", "composer": ""}
        summary = format_summary(original, recommended, recommended)
        self.assertIn("<b>Genre</b>: Rock", summary)
        self.assertIn("<b>Composer</b>: Bach", summary)
        self.assertNotIn("file: Bach", summary)
        self.assertNotIn("rec: Rock", summary)

    def test_summary_composer_strikes_removed_names_only(self) -> None:
        original = {"title": "Song", "composer": "Bach, Mozart & Salieri"}
        recommended = {"title": "Song", "composer": "Bach & Mozart"}
        summary = format_summary(original, recommended, recommended)
        self.assertIn("<b>Composer</b>: Bach &amp; Mozart, <s>Salieri</s>", summary)
        self.assertNotIn("<s>Bach</s>", summary)
        self.assertNotIn("file: Bach", summary)

    def test_use_file_keeps_nonempty_sparse_fields(self) -> None:
        original = {"title": "Old", "genre": "", "date": "1999", "composer": ""}
        recommended = {"title": "New", "genre": "Rock", "date": "", "composer": "Bach"}
        chosen = bulk_choice(original, recommended, use_file=True)
        self.assertEqual(chosen["title"], "Old")
        self.assertEqual(chosen["genre"], "Rock")
        self.assertEqual(chosen["date"], "1999")
        self.assertEqual(chosen["composer"], "Bach")

    def test_use_recommended_keeps_file_sparse_when_rec_empty(self) -> None:
        original = {"title": "Old", "genre": "Jazz", "composer": "Bach"}
        recommended = {"title": "New", "genre": "", "composer": ""}
        chosen = bulk_choice(original, recommended, use_file=False)
        self.assertEqual(chosen["title"], "New")
        self.assertEqual(chosen["genre"], "Jazz")
        self.assertEqual(chosen["composer"], "Bach")


class FormatRoundTripTests(unittest.TestCase):
    def test_split_then_format_normalizes_separators(self) -> None:
        self.assertEqual(format_artist_list(split_artist_field("A; B / C feat. D")), "A, B, C & D")

    def test_sorts_alphabetically_and_uses_ampersand(self) -> None:
        self.assertEqual(format_artist_list(["C", "A", "B"]), "A, B & C")
        self.assertEqual(format_artist_list(["A", "a", "B"]), "A & B")

    def test_album_artist_leads_when_present(self) -> None:
        names = ["Jimmy Napes", "Disclosure", "Sam Smith"]
        self.assertEqual(
            format_artist_list(names, lead="Sam Smith"),
            "Sam Smith, Disclosure & Jimmy Napes",
        )
        self.assertEqual(
            format_artist_list(names, lead="Sam Smith & Disclosure"),
            "Sam Smith, Disclosure & Jimmy Napes",
        )

    def test_normalize_name_list_dedupes(self) -> None:
        self.assertEqual(normalize_name_list("B, A, b & C"), "A, B & C")

    def test_diff_credit_html_strikes_removed_only(self) -> None:
        html = diff_credit_html("Bach, Mozart & Salieri", "Bach & Mozart")
        self.assertEqual(html, "Bach &amp; Mozart, <s>Salieri</s>")
        self.assertNotIn("<s>Bach</s>", html)
        self.assertNotIn("<s>Mozart</s>", html)


class NormalizeTagsetTests(unittest.TestCase):
    def test_album_artist_leads_artist_and_composer(self) -> None:
        tags = normalize_tagset(
            TagSet(
                albumartist="Sam Smith & Disclosure",
                artist="Jimmy Napes, Disclosure, Sam Smith, sam smith",
                composer="Jimmy Napes, Sam Smith",
            )
        )
        self.assertEqual(tags.albumartist, "Disclosure & Sam Smith")
        self.assertEqual(tags.artist, "Disclosure, Sam Smith & Jimmy Napes")
        self.assertEqual(tags.composer, "Sam Smith & Jimmy Napes")


class FillSparseTagsTests(unittest.TestCase):
    def test_fills_empty_rec_from_file(self) -> None:
        file_tags = TagSet(composer="Bach", genre="Jazz", date="1999", artist="A")
        rec = TagSet(composer="", genre="", date="", artist="B")
        filled = fill_sparse_tags(file_tags, rec)
        self.assertEqual(filled.composer, "Bach")
        self.assertEqual(filled.genre, "Jazz")
        self.assertEqual(filled.date, "1999")
        self.assertEqual(filled.artist, "B")

    def test_keeps_nonempty_rec(self) -> None:
        file_tags = TagSet(composer="File", genre="Jazz", date="1998")
        rec = TagSet(composer="Rec", genre="Rock", date="1999")
        filled = fill_sparse_tags(file_tags, rec)
        self.assertEqual(filled.composer, "Rec")
        self.assertEqual(filled.genre, "Rock")
        self.assertEqual(filled.date, "1999")


if __name__ == "__main__":
    unittest.main()
