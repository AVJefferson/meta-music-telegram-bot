from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from app.http_app import create_http_app
from tests.support import make_ctx, make_init_data, temp_catalog

ORIGIN = "https://music.example"


def _headers(user_id: int = 11, **extra) -> dict[str, str]:
    headers = {
        "X-Telegram-Init-Data": make_init_data("bot-token", user_id),
        "Origin": ORIGIN,
    }
    headers.update(extra)
    return headers


def _review_track(catalog, user_id: int, *, title: str = "Mine", **extra) -> int:
    fields = dict(
        kind="review",
        mb_recording_id=None,
        acoustid=None,
        local_path="",
        sidecar_path=None,
        relative_path="pile/song.flac",
        bit_depth=None,
        sample_rate=None,
        title=title,
        artist="A",
        album="B",
        status="uploaded",
        file_name="song.flac",
        drive_url="https://drive.google.com/file/d/abc/view",
        user_id=user_id,
    )
    fields.update(extra)
    return catalog.insert_pending(**fields)


class WebappApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_me_includes_counts_not_tokens(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.ensure_user(11)
            _review_track(catalog, 11)
            catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="lib/a.flac",
                bit_depth=None,
                sample_rate=None,
                title="Lib",
                artist="A",
                album="A",
                status="uploaded",
                user_id=11,
            )
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/me", headers=_headers())
                data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["review_count"], 1)
            self.assertEqual(data["library_count"], 1)
            blob = json.dumps(data)
            self.assertNotIn("refresh_token", blob)
            self.assertNotIn("access_token", blob)

    async def test_login_returns_https_ticket(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/login", headers=_headers())
                data = await resp.json()
            self.assertEqual(resp.status, 200)
            self.assertTrue(data["ok"])
            self.assertTrue(data["url"].startswith("https://music.example/oauth/start?token="))

    async def test_review_payload_fields(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            _review_track(catalog, 11)
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/review", headers=_headers())
                data = await resp.json()
            self.assertTrue(data["ok"])
            track = data["tracks"][0]
            self.assertEqual(track["file_name"], "song.flac")
            self.assertEqual(track["relative_path"], "pile/song.flac")
            self.assertEqual(track["drive_url"], "https://drive.google.com/file/d/abc/view")
            self.assertEqual(track["status"], "uploaded")
            self.assertIn("genre", track)
            self.assertIn("albumartist", track)

    async def test_cancel_deletes_own_track_not_other(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            mine = _review_track(catalog, 11, title="Mine")
            other = _review_track(catalog, 22, title="Secret", relative_path="x.flac")
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                steal = await client.post(
                    f"/api/review/{other}/action",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"action": "cancel"}),
                )
                gone = await client.post(
                    f"/api/review/{mine}/action",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"action": "cancel"}),
                )
                steal_status = steal.status
                gone_status = gone.status
                gone_data = await gone.json()
            self.assertEqual(steal_status, 404)
            self.assertEqual(gone_status, 200)
            self.assertEqual(gone_data["action"], "cancel")
            self.assertEqual(catalog.get_track(mine).status, "deleted")
            self.assertEqual(catalog.get_track(other).status, "uploaded")

    async def test_settings_dest_and_unlink(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.ensure_user(11)
            catalog.update_user(11, google_refresh_token="rt", google_email="a@x.com")
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                dest = await client.post(
                    "/api/settings",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"default_dest": "library"}),
                )
                unlink = await client.post(
                    "/api/settings",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"unlink": True}),
                )
                dest_data = await dest.json()
                unlink_data = await unlink.json()
            self.assertEqual(dest_data["default_dest"], "library")
            self.assertFalse(unlink_data["logged_in"])
            self.assertFalse(catalog.get_user(11).logged_in)

    async def test_settings_allowed_formats(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.ensure_user(11)
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                me = await client.get("/api/me", headers=_headers())
                me_data = await me.json()
                saved = await client.post(
                    "/api/settings",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"allowed_formats": ["wav", "bogus", "flac"]}),
                )
                saved_data = await saved.json()
                dest = await client.post(
                    "/api/settings",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"default_dest": "review"}),
                )
                dest_data = await dest.json()
                empty = await client.post(
                    "/api/settings",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"allowed_formats": []}),
                )
                empty_data = await empty.json()
            self.assertEqual(me_data["allowed_formats"], ["flac", "mp3"])
            self.assertEqual(saved_data["allowed_formats"], ["flac", "wav"])
            self.assertEqual(dest_data["allowed_formats"], ["flac", "wav"])
            self.assertEqual(dest_data["default_dest"], "review")
            self.assertEqual(empty_data["allowed_formats"], ["flac", "mp3"])
            stored = json.loads(catalog.get_user(11).settings_json)
            self.assertEqual(stored["default_dest"], "review")
            self.assertEqual(stored["allowed_formats"], ["flac", "mp3"])

    async def test_suggest_lastfm_flag_and_bool_in_library(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            rows = [
                {
                    "artist": "A",
                    "title": "T",
                    "in_library": True,
                    "why": "near",
                    "url": "https://www.last.fm/music/A/_/T",
                    "mbid": "rec-1",
                }
            ]
            with patch("app.suggest.suggest_for_user", AsyncMock(return_value=rows)):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/suggest", headers=_headers())
                    data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertFalse(data["lastfm"])
            self.assertIs(data["results"][0]["in_library"], True)
            self.assertEqual(data["results"][0]["mbid"], "rec-1")
            links = data["results"][0]["links"]
            self.assertTrue(links["youtube"].startswith("https://music.youtube.com/search"))
            self.assertTrue(links["lastfm"].startswith("https://www.last.fm/"))
            self.assertTrue(links["google"].startswith("https://www.google.com/search"))
            self.assertIn("HiFiAudioBot", links["hifi"])
            self.assertEqual(links["apple"], "")
            self.assertEqual(links["musicbrainz"], "https://musicbrainz.org/recording/rec-1")

    async def test_app_shell_and_static(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                page = await client.get("/app")
                css = await client.get("/app/static/app.css")
                js = await client.get("/app/static/app.js")
                html = await page.text()
                page_status = page.status
                css_status = css.status
                js_status = js.status
                css_type = css.headers.get("Content-Type", "")
            self.assertEqual(page_status, 200)
            self.assertIn("/app/static/app.css", html)
            self.assertIn("/app/static/app.js", html)
            self.assertEqual(css_status, 200)
            self.assertEqual(js_status, 200)
            self.assertIn("text/css", css_type)

    async def test_library_count_prefers_index(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.ensure_user(11)
            catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="lib/a.flac",
                bit_depth=None,
                sample_rate=None,
                title="Lib",
                artist="A",
                album="A",
                status="uploaded",
                user_id=11,
            )
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            entries = [{"title": "a"}, {"title": "b"}, {"title": "c"}]
            with patch("app.library_index.load_index_entries", return_value=entries):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/me", headers=_headers())
                    data = await resp.json()
            self.assertEqual(data["library_count"], 3)
            self.assertEqual(data["suggest_similarity"], 0.5)
            self.assertFalse(data["suggest_allow_dissimilar"])
            self.assertFalse(data["correct_telegram"])
            self.assertFalse(data["skip_save_prompt"])
            self.assertFalse(data["delete_original"])

    async def test_draft_rejected_and_tags_action(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            mine = _review_track(catalog, 11)
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            with (
                patch("app.relocate.hydrate_track_tags", AsyncMock(side_effect=lambda _ctx, track: track)),
                patch("app.relocate.relocate_track", AsyncMock()) as reloc,
            ):
                async with TestClient(TestServer(app)) as client:
                    draft = await client.post(
                        f"/api/review/{mine}/action",
                        headers=_headers(**{"Content-Type": "application/json"}),
                        data=json.dumps({"action": "draft"}),
                    )
                    tags = await client.post(
                        f"/api/review/{mine}/action",
                        headers=_headers(**{"Content-Type": "application/json"}),
                        data=json.dumps({"action": "tags", "title": "New", "artist": "B"}),
                    )
                    draft_status = draft.status
                    tags_status = tags.status
                    tags_data = await tags.json()
            self.assertEqual(draft_status, 400)
            self.assertEqual(tags_status, 200)
            self.assertEqual(tags_data["action"], "tags")
            self.assertEqual(reloc.await_args.kwargs["kind"], "review")
            self.assertEqual(reloc.await_args.kwargs["tags"].title, "New")
            self.assertEqual(reloc.await_args.kwargs["tags"].artist, "B")

    async def test_settings_similarity_round_trip(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.ensure_user(11)
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                saved = await client.post(
                    "/api/settings",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps({"suggest_similarity": 0.8, "suggest_allow_dissimilar": True}),
                )
                me = await client.get("/api/me", headers=_headers())
                saved_data = await saved.json()
                me_data = await me.json()
            self.assertAlmostEqual(saved_data["suggest_similarity"], 0.8)
            self.assertTrue(saved_data["suggest_allow_dissimilar"])
            self.assertAlmostEqual(me_data["suggest_similarity"], 0.8)
            self.assertTrue(me_data["suggest_allow_dissimilar"])
            stored = json.loads(catalog.get_user(11).settings_json)
            self.assertEqual(stored["suggest_similarity"], 0.8)
            self.assertTrue(stored["suggest_allow_dissimilar"])

    async def test_settings_save_prompt_prefs(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.ensure_user(11)
            catalog.update_user(11, settings_json=json.dumps({"default_dest": "library"}))
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                saved = await client.post(
                    "/api/settings",
                    headers=_headers(**{"Content-Type": "application/json"}),
                    data=json.dumps(
                        {"correct_telegram": True, "skip_save_prompt": True, "delete_original": True}
                    ),
                )
                me = await client.get("/api/me", headers=_headers())
                saved_data = await saved.json()
                me_data = await me.json()
            self.assertTrue(saved_data["correct_telegram"])
            self.assertTrue(saved_data["skip_save_prompt"])
            self.assertTrue(saved_data["delete_original"])
            self.assertEqual(saved_data["default_dest"], "library")
            self.assertTrue(me_data["correct_telegram"])
            self.assertTrue(me_data["skip_save_prompt"])
            self.assertTrue(me_data["delete_original"])
            self.assertEqual(me_data["default_dest"], "library")
            stored = json.loads(catalog.get_user(11).settings_json)
            self.assertEqual(stored["default_dest"], "library")
            self.assertTrue(stored["correct_telegram"])
            self.assertTrue(stored["skip_save_prompt"])
            self.assertTrue(stored["delete_original"])

    async def test_suggest_art_returns_urls(self) -> None:
        from dataclasses import replace

        directory, catalog = temp_catalog()
        with directory:
            ctx = replace(make_ctx(catalog), http=object())
            app = create_http_app(ctx)
            art = {"covers": ["https://example/a.jpg"], "apple": "https://music.apple.com/x"}
            with patch("app.enrich.list_cover_urls", AsyncMock(return_value=art)):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get(
                        "/api/suggest/art?artist=A&title=T",
                        headers=_headers(),
                    )
                    data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["covers"], ["https://example/a.jpg"])
            self.assertEqual(data["apple"], "https://music.apple.com/x")
