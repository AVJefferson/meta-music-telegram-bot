from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mutagen.id3 import ID3, TIT2, TPE1

from app.catalog import Catalog
from app.drive import DriveReviewItem
from app.genre import GenreMapper
from app.library_index import (
    dump_index_payload,
    ensure_library_index,
    entries_to_tracks,
    extract_item_tags,
    index_entry,
    load_index_entries,
    parse_index_payload,
    payload_sha,
    persist_index,
    rebuild_index,
    remove_entry,
    tags_from_path,
    tags_from_sidecar_payload,
    tags_from_songlog,
    upsert_entries,
    upsert_library_index,
)
from app.models import TagSet
from app.suggest import (
    Hit,
    SeedTrack,
    SimilarArtist,
    SimilarTrack,
    Suggestion,
    attach_library_meta,
    leftover_matches_seed,
    mix_library_pages,
    owned_key,
    owned_keys_from_tracks,
    parse_library_filename,
    parse_library_relative,
    parse_similar_artists,
    parse_similar_tracks,
    parse_top_tracks,
    rank_suggestions,
    seed_from_track,
    select_library_seeds,
    suggest_tracks,
    track_from_library_item,
    track_matches_language,
)
from app.suggest_cmd import (
    _fill_origin_messages,
    format_lyrics_text,
    format_suggest_card,
    load_drive_library,
    load_session_payload,
    pick_lyrics,
    suggest_label,
)

MAP = Path(__file__).resolve().parent.parent / "genre_map.yaml"


def mapper() -> GenreMapper:
    return GenreMapper(MAP)


def _track(catalog: Catalog, **overrides) -> int:
    payload = dict(
        kind="library",
        mb_recording_id=None,
        acoustid=None,
        local_path="",
        sidecar_path=None,
        relative_path="a.flac",
        bit_depth=16,
        sample_rate=44100,
        title="Karma Police",
        artist="Radiohead",
        album="OK Computer",
        status="uploaded",
        tags_json=json.dumps(
            {
                "title": "Karma Police",
                "artist": "Radiohead",
                "album": "OK Computer",
                "albumartist": "Radiohead",
                "composer": "",
                "genre": "alternative rock | English",
                "date": "1997",
                "tracknumber": "6",
                "discnumber": "1",
                "lyrics": "",
            }
        ),
        topic_name="English",
        file_name="karma.flac",
    )
    payload.update(overrides)
    return catalog.insert_pending(**payload)


class FakeLastfm:
    def __init__(self) -> None:
        self.similar_artist_map: dict[str, list[SimilarArtist]] = {}
        self.similar_track_map: dict[tuple[str, str], list[SimilarTrack]] = {}
        self.top_map: dict[str, list[SimilarTrack]] = {}
        self.tag_map: dict[str, list[str]] = {}
        self.artist_hits: dict[str, str] = {}
        self.track_hits: dict[str, tuple[str, str]] = {}

    async def similar_artists(self, artist: str) -> list[SimilarArtist]:
        return list(self.similar_artist_map.get(artist, []))

    async def similar_tracks(self, artist: str, title: str) -> list[SimilarTrack]:
        return list(self.similar_track_map.get((artist, title), []))

    async def top_tracks(self, artist: str, limit: int = 10) -> list[SimilarTrack]:
        return list(self.top_map.get(artist, []))[:limit]

    async def artist_tags(self, artist: str) -> list[str]:
        return list(self.tag_map.get(artist, []))

    async def search_artist(self, query: str) -> str | None:
        return self.artist_hits.get(query)

    async def search_track(self, query: str, artist: str = "") -> tuple[str, str] | None:
        return self.track_hits.get(query)


class OwnedKeyTests(unittest.TestCase):
    def test_punctuation_and_case(self) -> None:
        self.assertEqual(owned_key("A. R. Rahman", "Song!"), owned_key("a r rahman", "song"))


class QueryParseTests(unittest.TestCase):
    def test_genre_and_leftover_artist(self) -> None:
        tokens, leftover = mapper().extract_query_tokens("jazz melancholy A. R. Rahman")
        self.assertIn("jazz", [item.casefold() for item in tokens])
        self.assertIn("melancholy", [item.casefold() for item in tokens])
        self.assertIn("rahman", leftover.casefold())

    def test_topic_language(self) -> None:
        genre = mapper()
        self.assertEqual(genre.language_from_topic("Malayalam"), "Malayalam")
        self.assertEqual(genre.language_from_topic("malayalam"), "Malayalam")
        self.assertIsNone(genre.language_from_topic("General"))
        self.assertIsNone(genre.language_from_topic("Topic 12"))


class DrivePathTests(unittest.TestCase):
    def test_filename_with_and_without_track_number(self) -> None:
        self.assertEqual(
            parse_library_filename("Radiohead - 06 - Karma Police", "Radiohead"),
            ("Radiohead", "Karma Police"),
        )
        self.assertEqual(
            parse_library_filename("Radiohead - Karma Police", "Radiohead"),
            ("Radiohead", "Karma Police"),
        )
        self.assertEqual(parse_library_filename("06 - Title", "Artist"), ("Artist", "Title"))

    def test_relative_path_becomes_seed_and_track(self) -> None:
        relative = "Malayalam/K. S. Harisankar/Lucifer/K. S. Harisankar - 01 - Payaliya.flac"
        seed = parse_library_relative(relative)
        assert seed is not None
        self.assertEqual(seed.topic_name, "Malayalam")
        self.assertEqual(seed.artist, "K. S. Harisankar")
        self.assertEqual(seed.album, "Lucifer")
        self.assertEqual(seed.title, "Payaliya")
        track = track_from_library_item(relative_path=relative, drive_file_id="drv")
        assert track is not None
        self.assertEqual(track.topic_name, "Malayalam")
        self.assertEqual(track.artist, "K. S. Harisankar")
        self.assertEqual(track.drive_file_id, "drv")
        self.assertTrue(track_matches_language(seed_from_track(track), "Malayalam", mapper()))


class LibraryIndexTests(unittest.TestCase):
    def test_sidecar_and_upsert_round_trip(self) -> None:
        sidecar = tags_from_sidecar_payload(
            {"proposed": {"title": "Go", "artist": "Radiohead", "album": "Pablo Honey", "genre": "rock | English"}}
        )
        self.assertEqual(sidecar.title, "Go")
        self.assertEqual(sidecar.genre, "rock | English")
        entry = index_entry(
            relative_path="English/Radiohead/Pablo Honey/Radiohead - 01 - Go.flac",
            drive_file_id="x",
            topic_name="English",
            tags=sidecar,
        )
        entries = upsert_entries([], entry)
        entries = upsert_entries(entries, {**entry, "title": "Go (remaster)"})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Go (remaster)")
        entries = remove_entry(entries, entry["relative_path"])
        self.assertEqual(entries, [])

    def test_entries_to_tracks_filters_topic_and_keeps_genre(self) -> None:
        payload = dump_index_payload(
            [
                index_entry(
                    relative_path="English/A/LP/A - 01 - One.flac",
                    drive_file_id="1",
                    topic_name="English",
                    tags=TagSet(title="One", artist="A", album="LP", genre="jazz | English"),
                ),
                index_entry(
                    relative_path="Malayalam/B/LP/B - 01 - Two.flac",
                    drive_file_id="2",
                    topic_name="Malayalam",
                    tags=TagSet(title="Two", artist="B", album="LP", genre="filmi | Malayalam"),
                ),
            ]
        )
        tracks = entries_to_tracks(parse_index_payload(payload), topic="Malayalam")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "Two")
        self.assertIn("filmi", tracks[0].tags_json or "")

    def test_path_tags_use_folder_albumartist(self) -> None:
        tags = tags_from_path(
            "English/Various Artists/Now/Radiohead - 01 - Karma Police.flac"
        )
        self.assertEqual(tags.title, "Karma Police")
        self.assertEqual(tags.artist, "Radiohead")
        self.assertEqual(tags.album, "Now")
        self.assertEqual(tags.albumartist, "Various Artists")
        self.assertEqual(tags.genre, "English")

    def test_songlog_prefers_chosen_block(self) -> None:
        tags = tags_from_songlog(
            "== file tags ==\n"
            "title: File Title\n"
            "artist: File Artist\n"
            "\n"
            "== CHOSEN ==\n"
            "title: Chosen Title\n"
            "artist: Chosen Artist\n"
            "genre: jazz | English\n"
        )
        self.assertEqual(tags.title, "Chosen Title")
        self.assertEqual(tags.artist, "Chosen Artist")
        self.assertEqual(tags.genre, "jazz | English")

    def test_extract_skips_flac_when_path_has_tags(self) -> None:
        item = DriveReviewItem(
            file_id="flac",
            sidecar_id=None,
            name="Radiohead - 06 - Karma Police.flac",
            relative_path="English/Radiohead/OK Computer/Radiohead - 06 - Karma Police.flac",
            size=1,
            modified=None,
        )

        def boom(*_args, **_kwargs):
            raise AssertionError("must not download FLAC when path tags are enough")

        ctx = SimpleNamespace(drive=SimpleNamespace(download_bytes=boom, download_to=boom))
        tags = extract_item_tags(ctx, item, Path("/tmp"))
        self.assertEqual(tags.title, "Karma Police")
        self.assertEqual(tags.artist, "Radiohead")

    def test_extract_overlays_sidecar_not_flac(self) -> None:
        item = DriveReviewItem(
            file_id="flac",
            sidecar_id="side",
            name="Radiohead - 06 - Karma Police.flac",
            relative_path="English/Radiohead/OK Computer/Radiohead - 06 - Karma Police.flac",
            size=1,
            modified=None,
        )

        def download_bytes(file_id: str) -> bytes:
            self.assertEqual(file_id, "side")
            return json.dumps(
                {"proposed": {"title": "Karma Police", "artist": "Radiohead", "genre": "alternative rock | English"}}
            ).encode()

        def boom(*_args, **_kwargs):
            raise AssertionError("must not download FLAC when sidecar exists")

        ctx = SimpleNamespace(drive=SimpleNamespace(download_bytes=download_bytes, download_to=boom))
        tags = extract_item_tags(ctx, item, Path("/tmp"))
        self.assertEqual(tags.genre, "alternative rock | English")

    def test_extract_reads_flac_in_tmp_then_deletes(self) -> None:
        item = DriveReviewItem(
            file_id="flac",
            sidecar_id=None,
            name="track.flac",
            relative_path="English/track.flac",
            size=1,
            modified=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            tmp_root = Path(directory)
            seen: list[Path] = []

            def download_to(_file_id: str, dest: Path) -> Path:
                seen.append(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"not-flac")
                tags = ID3()
                tags.add(TIT2(encoding=3, text="Stay With Me"))
                tags.add(TPE1(encoding=3, text="Sam Smith"))
                tags.save(dest)
                return dest

            ctx = SimpleNamespace(drive=SimpleNamespace(download_to=download_to))
            tags = extract_item_tags(ctx, item, tmp_root)
            self.assertEqual(tags.title, "Stay With Me")
            self.assertEqual(tags.artist, "Sam Smith")
            self.assertEqual(len(seen), 1)
            self.assertTrue(str(seen[0]).startswith(str(tmp_root / "index")))
            self.assertFalse(seen[0].exists())

    def test_rebuild_writes_drive_json_not_library_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            library_root = Path(directory) / "library"
            library_root.mkdir()
            uploaded: list[tuple[str, str]] = []
            item = DriveReviewItem(
                file_id="flac1",
                sidecar_id=None,
                name="Radiohead - 06 - Karma Police.flac",
                relative_path="English/Radiohead/OK Computer/Radiohead - 06 - Karma Police.flac",
                size=1,
                modified=None,
            )

            def boom(*_args, **_kwargs):
                raise AssertionError("rebuild with path tags must not download FLACs")

            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    gdrive_folder_id="root",
                    tmp_root=Path(directory) / "tmp",
                    library_root=library_root,
                ),
                catalog=catalog,
                drive=SimpleNamespace(
                    list_library_items=lambda _root: [item],
                    find_path=lambda *_args, **_kwargs: "libfolder",
                    find_by_name=lambda *_args, **_kwargs: [],
                    ensure_parent=lambda *_args, **_kwargs: "libfolder",
                    upload_bytes=lambda data, parent, name, _mime, replace_id=None: uploaded.append(
                        (parent, name, data)
                    )
                    or ("jsonid", None),
                    download_to=boom,
                    download_bytes=boom,
                ),
            )
            entries = rebuild_index(ctx)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["title"], "Karma Police")
            self.assertEqual(uploaded[0][0], "libfolder")
            self.assertEqual(uploaded[0][1], "tracks.json")
            self.assertFalse(any(library_root.rglob("*.flac")))
            cached = load_index_entries(ctx)
            self.assertIsNotNone(cached)
            self.assertEqual(len(cached or []), 1)

    def test_upsert_rebuilds_instead_of_clobbering_missing_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            existing = DriveReviewItem(
                file_id="old",
                sidecar_id=None,
                name="A - 01 - Old.flac",
                relative_path="English/A/LP/A - 01 - Old.flac",
                size=1,
                modified=None,
            )
            uploaded: list[bytes] = []
            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    gdrive_folder_id="root",
                    tmp_root=Path(directory) / "tmp",
                    library_root=Path(directory) / "library",
                ),
                catalog=catalog,
                drive=SimpleNamespace(
                    list_library_items=lambda _root: [existing],
                    find_path=lambda *_args, **_kwargs: None,
                    find_by_name=lambda *_args, **_kwargs: [],
                    ensure_parent=lambda *_args, **_kwargs: "libfolder",
                    upload_bytes=lambda data, *_args, **_kwargs: uploaded.append(data) or ("jsonid", None),
                    download_to=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError("path tags enough")
                    ),
                ),
            )
            upsert_library_index(
                ctx,
                relative_path="English/B/LP/B - 01 - New.flac",
                drive_file_id="new",
                topic_name="English",
                tags=TagSet(title="New", artist="B", album="LP", genre="English"),
            )
            tracks = json.loads(uploaded[-1].decode())["tracks"]
            titles = {item["title"] for item in tracks}
            self.assertEqual(titles, {"Old", "New"})

    def test_persist_updates_in_place_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            first = index_entry(
                relative_path="English/A/LP/A - 01 - One.flac",
                drive_file_id="1",
                topic_name="English",
                tags=TagSet(title="One", artist="A", album="LP"),
            )
            catalog.set_library_tag_index(
                dump_index_payload([first]),
                drive_file_id="jsonid",
                payload_sha="stale",
            )
            uploaded: list[str | None] = []

            def boom(*_args, **_kwargs):
                raise AssertionError("must not download tracks.json to add a row")

            ctx = SimpleNamespace(
                settings=SimpleNamespace(gdrive_folder_id="root"),
                catalog=catalog,
                drive=SimpleNamespace(
                    download_bytes=boom,
                    find_path=boom,
                    find_by_name=boom,
                    list_library_items=boom,
                    ensure_parent=boom,
                    upload_bytes=lambda data, parent, name, _mime, replace_id=None: uploaded.append(replace_id)
                    or (replace_id, None),
                ),
            )
            persist_index(
                ctx,
                [
                    first,
                    index_entry(
                        relative_path="English/B/LP/B - 01 - Two.flac",
                        drive_file_id="2",
                        topic_name="English",
                        tags=TagSet(title="Two", artist="B", album="LP"),
                    ),
                ],
            )
            self.assertEqual(uploaded, ["jsonid"])
            _payload, drive_id, sha = catalog.get_library_tag_index_meta()
            self.assertEqual(drive_id, "jsonid")
            self.assertTrue(sha)

    def test_persist_skips_identical_drive_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            entries = [
                index_entry(
                    relative_path="English/A/LP/A - 01 - One.flac",
                    drive_file_id="1",
                    topic_name="English",
                    tags=TagSet(title="One", artist="A", album="LP"),
                )
            ]
            payload = dump_index_payload(entries)
            catalog.set_library_tag_index(payload, drive_file_id="jsonid", payload_sha=payload_sha(payload))
            uploaded: list = []
            ctx = SimpleNamespace(
                catalog=catalog,
                drive=SimpleNamespace(
                    upload_bytes=lambda *_args, **_kwargs: uploaded.append(True) or ("jsonid", None)
                ),
            )
            persist_index(ctx, entries)
            self.assertEqual(uploaded, [])

    def test_ensure_uses_drive_json_after_empty_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            payload = dump_index_payload(
                [
                    index_entry(
                        relative_path="English/A/LP/A - 01 - One.flac",
                        drive_file_id="1",
                        topic_name="English",
                        tags=TagSet(title="One", artist="A", album="LP"),
                    )
                ]
            )

            def boom(*_args, **_kwargs):
                raise AssertionError("must not walk FLACs when tracks.json exists")

            ctx = SimpleNamespace(
                settings=SimpleNamespace(gdrive_folder_id="root"),
                catalog=catalog,
                drive=SimpleNamespace(
                    list_library_items=boom,
                    download_to=boom,
                    find_path=lambda _root, parts: "libfolder" if parts == ["library"] else None,
                    find_by_name=lambda _parent, name: [SimpleNamespace(id="jsonid")]
                    if name == "tracks.json"
                    else [],
                    download_bytes=lambda file_id: payload.encode() if file_id == "jsonid" else b"",
                ),
            )
            source = ensure_library_index(ctx)
            self.assertEqual(source, "cache")
            tracks = entries_to_tracks(load_index_entries(ctx) or [])
            self.assertEqual(tracks[0].title, "One")

    def test_ensure_walks_when_index_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            walked: list[bool] = []
            item = DriveReviewItem(
                file_id="flac1",
                sidecar_id=None,
                name="Radiohead - 06 - Karma Police.flac",
                relative_path="English/Radiohead/OK Computer/Radiohead - 06 - Karma Police.flac",
                size=1,
                modified=None,
            )
            ctx = SimpleNamespace(
                settings=SimpleNamespace(
                    gdrive_folder_id="root",
                    tmp_root=Path(directory) / "tmp",
                    library_root=Path(directory) / "library",
                ),
                catalog=catalog,
                drive=SimpleNamespace(
                    list_library_items=lambda _root: walked.append(True) or [item],
                    find_path=lambda *_args, **_kwargs: None,
                    find_by_name=lambda *_args, **_kwargs: [],
                    ensure_parent=lambda *_args, **_kwargs: "libfolder",
                    upload_bytes=lambda *_args, **_kwargs: ("jsonid", None),
                    download_to=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        AssertionError("path tags enough")
                    ),
                ),
            )
            source = ensure_library_index(ctx)
            self.assertEqual(source, "walk")
            self.assertEqual(walked, [True])


class SeedFilterTests(unittest.TestCase):
    def test_language_topic_and_leftover_boost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            _track(catalog)
            _track(
                catalog,
                title="Payaliya",
                artist="K. S. Harisankar",
                album="Lucifer",
                relative_path="b.flac",
                file_name="payaliya.flac",
                topic_name="Malayalam",
                tags_json=json.dumps(
                    {
                        "title": "Payaliya",
                        "artist": "K. S. Harisankar",
                        "album": "Lucifer",
                        "albumartist": "Deepak Dev",
                        "composer": "",
                        "genre": "filmi | Malayalam",
                        "date": "2019",
                        "tracknumber": "1",
                        "discnumber": "1",
                        "lyrics": "",
                    }
                ),
            )
            tracks = catalog.list_library_tracks()
            genre = mapper()
            mal = select_library_seeds(tracks, genre, language="Malayalam")
            self.assertEqual(len(mal), 1)
            self.assertEqual(mal[0].title, "Payaliya")
            boosted = select_library_seeds(tracks, genre, leftover="radiohead")
            self.assertTrue(any(seed.boosted and seed.title == "Karma Police" for seed in boosted))
            self.assertTrue(
                leftover_matches_seed(boosted[0], "radiohead")
                or leftover_matches_seed(boosted[-1], "radiohead")
            )

    def test_track_language_from_genre_not_topic(self) -> None:
        seed = SeedTrack(artist="A", title="B", genre="jazz | Hindi", topic_name="General")
        self.assertTrue(track_matches_language(seed, "Hindi", mapper()))
        self.assertFalse(track_matches_language(seed, "Tamil", mapper()))


class RankTests(unittest.TestCase):
    def test_library_tracks_are_kept_and_tagged(self) -> None:
        hits = [
            Hit(artist="Radiohead", title="Karma Police", match=0.99, vias=("similar to Muse",)),
            Hit(artist="Muse", title="Starlight", match=0.4, vias=("similar to Radiohead",)),
        ]
        ranked = rank_suggestions(
            hits,
            owned={owned_key("Radiohead", "Karma Police")},
            shown=set(),
            mapper=mapper(),
        )
        by_title = {item.title: item for item in ranked}
        self.assertIn("Karma Police", by_title)
        self.assertIn("Starlight", by_title)
        self.assertTrue(by_title["Karma Police"].in_library)
        self.assertFalse(by_title["Starlight"].in_library)

    def test_mix_pages_half_library(self) -> None:
        in_lib = [
            Suggestion(artist="A", title=f"In{index}", score=1, why="", url="", in_library=True)
            for index in range(4)
        ]
        out_lib = [
            Suggestion(artist="B", title=f"Out{index}", score=1, why="", url="", in_library=False)
            for index in range(4)
        ]
        mixed = mix_library_pages(in_lib + out_lib, page_size=8)
        page = mixed[:8]
        self.assertEqual(sum(1 for item in page if item.in_library), 4)
        self.assertEqual(sum(1 for item in page if not item.in_library), 4)

    def test_mix_pages_fills_with_out_when_few_in_library(self) -> None:
        in_lib = [Suggestion(artist="A", title="Only", score=1, why="", url="", in_library=True)]
        out_lib = [
            Suggestion(artist="B", title=f"Out{index}", score=1, why="", url="", in_library=False)
            for index in range(7)
        ]
        mixed = mix_library_pages(in_lib + out_lib, page_size=8)
        self.assertEqual(sum(1 for item in mixed if item.in_library), 1)
        self.assertEqual(len(mixed), 8)

    def test_attach_library_meta_copies_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            track_id = _track(catalog)
            catalog.update_track(
                track_id,
                source_chat_id=-100,
                source_message_id=55,
                thread_id=9,
                telegram_file_id="AgFile",
            )
            track = catalog.get_track(track_id)
            assert track is not None
            items = attach_library_meta(
                [Suggestion(artist="Radiohead", title="Karma Police", score=1, why="", url="")],
                [track],
            )
            self.assertTrue(items[0].in_library)
            self.assertEqual(items[0].track_id, track_id)
            self.assertEqual(items[0].chat_id, -100)
            self.assertEqual(items[0].message_id, 55)
            self.assertEqual(items[0].thread_id, 9)
            self.assertEqual(items[0].telegram_file_id, "AgFile")

    def test_fill_origin_from_bound_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            track_id = _track(catalog)
            catalog.bind_track_message(track_id, -100, 77)
            item = Suggestion(
                artist="Radiohead",
                title="Karma Police",
                score=1,
                why="",
                url="",
                in_library=True,
                track_id=track_id,
            )
            _fill_origin_messages(SimpleNamespace(catalog=catalog), [item])
            self.assertEqual(item.chat_id, -100)
            self.assertEqual(item.message_id, 77)

    def test_multi_seed_beats_single_weak_match(self) -> None:
        weak = Hit(artist="One Hit", title="Weak", match=0.35, vias=("similar to A",))
        strong = Hit(
            artist="Two Hit",
            title="Strong",
            match=0.30,
            vias=("similar to A", "similar to B"),
        )
        ranked = rank_suggestions(
            [weak, strong],
            owned=set(),
            shown=set(),
            mapper=mapper(),
        )
        self.assertEqual(ranked[0].title, "Strong")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_diversity_caps_per_artist(self) -> None:
        hits = [
            Hit(artist="Muse", title=f"Song {index}", match=0.9 - index * 0.01, vias=("similar to Radiohead",))
            for index in range(6)
        ]
        ranked = rank_suggestions(
            hits,
            owned=set(),
            shown=set(),
            mapper=mapper(),
            per_artist=2,
            limit=10,
        )
        self.assertEqual(len(ranked), 2)

    def test_other_language_dropped_when_known(self) -> None:
        keep = Hit(
            artist="Local",
            title="Keep",
            match=0.4,
            vias=("similar to A",),
            artist_tags=("malayalam", "filmi"),
        )
        drop = Hit(
            artist="Other",
            title="Drop",
            match=0.9,
            vias=("similar to A",),
            artist_tags=("hindi", "filmi"),
        )
        unknown = Hit(artist="Maybe", title="Unknown", match=0.5, vias=("similar to A",))
        ranked = rank_suggestions(
            [drop, keep, unknown],
            owned=set(),
            shown=set(),
            mapper=mapper(),
            language="Malayalam",
        )
        titles = {item.title for item in ranked}
        self.assertIn("Keep", titles)
        self.assertIn("Unknown", titles)
        self.assertNotIn("Drop", titles)

    def test_shown_sorts_after_fresh(self) -> None:
        fresh = Hit(artist="A", title="Fresh", match=0.2, vias=("similar to X",))
        old = Hit(artist="B", title="Old", match=0.9, vias=("similar to X",))
        ranked = rank_suggestions(
            [old, fresh],
            owned=set(),
            shown={owned_key("B", "Old")},
            mapper=mapper(),
        )
        self.assertEqual([item.title for item in ranked], ["Fresh", "Old"])


class LastfmParseTests(unittest.TestCase):
    def test_single_item_not_list(self) -> None:
        artists = parse_similar_artists(
            {"similarartists": {"artist": {"name": "Muse", "match": "0.8", "url": "https://www.last.fm/music/Muse"}}}
        )
        self.assertEqual(artists[0].name, "Muse")
        self.assertAlmostEqual(artists[0].match, 0.8)
        tracks = parse_similar_tracks(
            {
                "similartracks": {
                    "track": {
                        "name": "Starlight",
                        "artist": {"name": "Muse"},
                        "match": "0.4",
                        "url": "https://www.last.fm/music/Muse/_/Starlight",
                    }
                }
            }
        )
        self.assertEqual(tracks[0].title, "Starlight")
        tops = parse_top_tracks({"toptracks": {"track": {"name": "Uprising", "artist": {"name": "Muse"}}}})
        self.assertAlmostEqual(tops[0].match, 0.8)


class CatalogSuggestTests(unittest.TestCase):
    def test_library_owned_and_session_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            lib_id = _track(catalog)
            catalog.insert_pending(
                kind="review",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="r.flac",
                bit_depth=None,
                sample_rate=None,
                title="Review Song",
                artist="Review Artist",
                album="",
                status="uploaded",
                file_name="r.flac",
            )
            library = catalog.list_library_tracks()
            self.assertEqual(len(library), 1)
            self.assertEqual(library[0].id, lib_id)
            owned = owned_keys_from_tracks(catalog.list_owned_tracks())
            self.assertIn(owned_key("Radiohead", "Karma Police"), owned)
            self.assertIn(owned_key("Review Artist", "Review Song"), owned)
            session_id = catalog.insert_suggest_session(
                user_id=7,
                chat_id=1,
                thread_id=None,
                query="jazz",
                results_json='{"language":"","items":[]}',
                expires_at="2099-01-01T00:00:00+00:00",
            )
            session = catalog.get_suggest_session(session_id)
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.query, "jazz")
            catalog.mark_suggest_shown(7, ["a|one", "b|two"])
            self.assertEqual(catalog.list_suggest_shown(7), {"a|one", "b|two"})
            catalog.set_lastfm_cache("k", "{}", "2099-01-01T00:00:00+00:00")
            self.assertEqual(catalog.get_lastfm_cache("k"), "{}")

    def test_shown_keep_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            catalog.mark_suggest_shown(1, [f"k|{index}" for index in range(5)], keep=3)
            shown = catalog.list_suggest_shown(1)
            self.assertEqual(len(shown), 3)

    def test_seed_from_track_reads_tags_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            track_id = _track(catalog)
            track = catalog.get_track(track_id)
            assert track is not None
            seed = seed_from_track(track)
            self.assertEqual(seed.artist, "Radiohead")
            self.assertIn("English", seed.genre)


class SuggestCmdFormatTests(unittest.TestCase):
    def test_card_escapes_and_links(self) -> None:
        from app.suggest import Suggestion

        card = format_suggest_card(
            Suggestion(
                artist="A & B",
                title="<X>",
                score=1.0,
                why="similar to Radiohead",
                url="https://www.last.fm/music/A/_/X",
                mbid="abc",
            )
        )
        self.assertIn("&amp;", card)
        self.assertIn("&lt;X&gt;", card)
        self.assertIn("Last.fm", card)
        self.assertIn("MusicBrainz", card)
        self.assertNotIn("In library", card)
        owned = format_suggest_card(
            Suggestion(artist="A", title="B", score=1.0, why="", url="", in_library=True)
        )
        self.assertIn("In library", owned)
        self.assertEqual(suggest_label(Suggestion("Artist", "Title", 1, "", "")), "Artist — Title")
        self.assertEqual(
            suggest_label(Suggestion("Artist", "Title", 1, "", "", in_library=True)),
            "✓ Artist — Title",
        )
        lyrics = format_lyrics_text(
            Suggestion(artist="A", title="B", score=1, why="", url="", in_library=True),
            "[00:01.00] Hello",
        )
        self.assertIn("Synced lyrics", lyrics)
        self.assertIn("In library", lyrics)

    def test_pick_lyrics_prefers_synced(self) -> None:
        self.assertEqual(pick_lyrics("[00:01.00] A", "[00:02.00] B"), "[00:01.00] A")
        self.assertEqual(pick_lyrics("plain", "[00:01.00] synced"), "[00:01.00] synced")
        self.assertEqual(pick_lyrics("plain", ""), "plain")
        self.assertEqual(pick_lyrics("", "plain fetched"), "plain fetched")

    def test_session_payload_wrapper(self) -> None:
        language, items = load_session_payload(
            json.dumps(
                {
                    "language": "Malayalam",
                    "items": [{"artist": "A", "title": "B", "score": 1, "why": "", "url": "https://x"}],
                }
            )
        )
        self.assertEqual(language, "Malayalam")
        self.assertEqual(items[0].title, "B")
        _language, legacy = load_session_payload(
            json.dumps([{"artist": "A", "title": "C", "score": 1, "why": "", "url": ""}])
        )
        self.assertEqual(legacy[0].title, "C")


class SuggestPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_lastfm_keeps_library_except_seed(self) -> None:
        client = FakeLastfm()
        client.top_map["Radiohead"] = [
            SimilarTrack("Radiohead", "Karma Police", 0.8),
            SimilarTrack("Radiohead", "Paranoid Android", 0.8),
        ]
        client.similar_artist_map["Radiohead"] = [SimilarArtist("Muse", 0.7)]
        client.top_map["Muse"] = [SimilarTrack("Muse", "Starlight", 0.8)]
        client.similar_track_map[("Radiohead", "Karma Police")] = [
            SimilarTrack("Muse", "Knights of Cydonia", 0.5),
            SimilarTrack("Radiohead", "Karma Police", 0.9),
        ]
        seeds = [SeedTrack(artist="Radiohead", title="Karma Police", genre="alternative rock | English")]
        items = await suggest_tracks(
            client,
            seeds,
            owned={
                owned_key("Radiohead", "Karma Police"),
                owned_key("Radiohead", "Paranoid Android"),
            },
            shown=set(),
            mapper=mapper(),
        )
        titles = {item.title for item in items}
        self.assertNotIn("Karma Police", titles)
        self.assertIn("Paranoid Android", titles)
        self.assertTrue({"Starlight", "Knights of Cydonia"} & titles)
        owned_row = next(item for item in items if item.title == "Paranoid Android")
        self.assertTrue(owned_row.in_library)

    async def test_load_drive_library_uses_cached_index_not_flac_walk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            catalog.set_library_tag_index(
                dump_index_payload(
                    [
                        index_entry(
                            relative_path="English/Radiohead/OK Computer/Radiohead - 06 - Karma Police.flac",
                            drive_file_id="id1",
                            topic_name="English",
                            tags=TagSet(
                                title="Karma Police",
                                artist="Radiohead",
                                album="OK Computer",
                                genre="alternative rock | English",
                            ),
                        )
                    ]
                )
            )

            def boom(*_args, **_kwargs):
                raise AssertionError("must not walk Drive FLACs when index exists")

            ctx = SimpleNamespace(
                settings=SimpleNamespace(gdrive_folder_id="lib"),
                catalog=catalog,
                drive=SimpleNamespace(list_library_items=boom, find_path=boom),
            )
            tracks = await load_drive_library(ctx, topic="English")
            self.assertEqual(len(tracks), 1)
            self.assertEqual(tracks[0].title, "Karma Police")
            self.assertEqual(tracks[0].artist, "Radiohead")
            self.assertEqual(tracks[0].topic_name, "English")
            self.assertEqual(tracks[0].album, "OK Computer")

    async def test_load_drive_library_uses_drive_json_not_flac_walk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory) / "state.sqlite")
            payload = dump_index_payload(
                [
                    index_entry(
                        relative_path="Malayalam/Harisankar/Lucifer/Harisankar - 01 - Payaliya.flac",
                        drive_file_id="id2",
                        topic_name="Malayalam",
                        tags=TagSet(title="Payaliya", artist="K. S. Harisankar", album="Lucifer"),
                    )
                ]
            )

            def boom(*_args, **_kwargs):
                raise AssertionError("must not walk Drive FLACs when tracks.json exists")

            ctx = SimpleNamespace(
                settings=SimpleNamespace(gdrive_folder_id="lib"),
                catalog=catalog,
                drive=SimpleNamespace(
                    list_library_items=boom,
                    download_to=boom,
                    find_path=lambda _root, parts: "libfolder" if parts == ["library"] else None,
                    find_by_name=lambda _parent, name: [SimpleNamespace(id="jsonid")]
                    if name == "tracks.json"
                    else [],
                    download_bytes=lambda file_id: payload.encode() if file_id == "jsonid" else b"",
                ),
            )
            tracks = await load_drive_library(ctx, topic="Malayalam")
            self.assertEqual(len(tracks), 1)
            self.assertEqual(tracks[0].title, "Payaliya")
            self.assertIsNotNone(catalog.get_library_tag_index())
