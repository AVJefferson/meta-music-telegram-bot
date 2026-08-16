from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mutagen.id3 import ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TRCK

from app.catalog import Catalog
from app.models import TagSet
from app.relocate import hydrate_track_tags, read_tags_for_card
from app.review_cmd import format_song_card
from app.tags import overlay_tagset, read_tagset


def write_id3(path: Path, **fields: str) -> None:
    tags = ID3()
    mapping = {
        "title": TIT2,
        "artist": TPE1,
        "album": TALB,
        "albumartist": TPE2,
        "composer": TCOM,
        "genre": TCON,
        "date": TDRC,
        "tracknumber": TRCK,
    }
    for key, frame_cls in mapping.items():
        value = fields.get(key)
        if value:
            tags.add(frame_cls(encoding=3, text=value))
    tags.save(path)


class TagReadTests(unittest.TestCase):
    def test_id3_fallback_when_not_vorbis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "03 - Sam Smith - Stay With Me.flac"
            write_id3(
                path,
                title="Stay With Me",
                artist="Sam Smith",
                album="In the Lonely Hour",
                albumartist="Sam Smith",
                composer="Sam Smith",
                genre="Pop",
                date="2014",
                tracknumber="3/10",
            )
            tags = read_tagset(path)
            self.assertEqual(tags.title, "Stay With Me")
            self.assertEqual(tags.artist, "Sam Smith")
            self.assertEqual(tags.album, "In the Lonely Hour")
            self.assertEqual(tags.albumartist, "Sam Smith")
            self.assertEqual(tags.composer, "Sam Smith")
            self.assertEqual(tags.genre, "Pop")
            self.assertEqual(tags.date, "2014")
            self.assertEqual(tags.tracknumber, "3")

    def test_overlay_prefers_file_over_filename_title(self) -> None:
        base = TagSet(title="03 - Sam Smith - Stay With Me", artist="", album="")
        extra = TagSet(title="Stay With Me", artist="Sam Smith", album="In the Lonely Hour")
        merged = overlay_tagset(base, extra)
        self.assertEqual(merged.title, "Stay With Me")
        self.assertEqual(merged.artist, "Sam Smith")
        self.assertEqual(merged.album, "In the Lonely Hour")

    def test_review_card_reads_id3_even_if_catalog_title_is_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "03 - Sam Smith - Stay With Me.flac"
            write_id3(
                path,
                title="Stay With Me",
                artist="Sam Smith",
                album="In the Lonely Hour",
            )
            track = SimpleNamespace(
                kind="review",
                drive_url=None,
                relative_path="2026-08-16/03 - Sam Smith - Stay With Me.flac",
                local_path=str(path),
                tags_json=None,
                source_report_json=None,
                title="03 - Sam Smith - Stay With Me",
                artist="",
                album="",
            )
            tags = read_tags_for_card(track)
            self.assertEqual(tags.title, "Stay With Me")
            self.assertEqual(tags.artist, "Sam Smith")
            text = format_song_card(track)
            self.assertIn("Stay With Me", text)
            self.assertIn("Sam Smith", text)
            self.assertIn("In the Lonely Hour", text)
            self.assertNotIn("<b>03 - Sam Smith - Stay With Me</b>", text)


class HydrateTrackTagsTests(unittest.IsolatedAsyncioTestCase):
    async def test_hydrate_when_catalog_title_is_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review = root / "review"
            relative = "2026-08-16/03 - Sam Smith - Stay With Me.flac"
            dest = review / relative
            dest.parent.mkdir(parents=True)
            write_id3(
                dest,
                title="Stay With Me",
                artist="Sam Smith",
                album="In the Lonely Hour",
            )
            catalog = Catalog(root / "state.sqlite")
            track_id = catalog.insert_pending(
                kind="review",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path=relative,
                bit_depth=None,
                sample_rate=None,
                title="03 - Sam Smith - Stay With Me",
                artist="",
                album="",
                status="uploaded",
                file_name="03 - Sam Smith - Stay With Me.flac",
                drive_file_id="drive-1",
            )
            track = catalog.get_track(track_id)
            assert track is not None
            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    library_root=root / "library",
                    review_root=review,
                    pending_root=root / "pending",
                    gdrive_folder_id="lib",
                    gdrive_review_folder_id="rev",
                ),
                catalog=catalog,
                drive=SimpleNamespace(),
            )
            updated = await hydrate_track_tags(ctx, track)
            self.assertEqual(updated.title, "Stay With Me")
            self.assertEqual(updated.artist, "Sam Smith")
            self.assertEqual(updated.album, "In the Lonely Hour")
            self.assertTrue(updated.tags_json)
            catalog.close()
