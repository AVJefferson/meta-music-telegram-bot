from __future__ import annotations

import json
import time
import unittest

from aiohttp.test_utils import TestClient, TestServer

from app.http_app import create_http_app
from app.initdata import parse_init_data
from tests.support import make_ctx, make_init_data, temp_catalog


class InitDataTests(unittest.TestCase):
    def test_tampered_hash_rejected(self) -> None:
        from app.errors import AppError

        raw = make_init_data("bot-token", 8)
        tampered = raw[:-2] + ("0" if raw[-1] != "0" else "1")
        with self.assertRaises(AppError) as ctx:
            parse_init_data("bot-token", tampered, max_age=300)
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_expired_auth_date_rejected(self) -> None:
        from app.errors import AppError

        raw = make_init_data("bot-token", 8, auth_date=int(time.time()) - 10_000)
        with self.assertRaises(AppError):
            parse_init_data("bot-token", raw, max_age=300)


class MiniAppIdorTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_scoped_to_initdata_user(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.insert_pending(
                kind="review",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="x.flac",
                bit_depth=None,
                sample_rate=None,
                title="Secret",
                artist="B",
                album="B",
                status="uploaded",
                user_id=22,
            )
            catalog.insert_pending(
                kind="review",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="y.flac",
                bit_depth=None,
                sample_rate=None,
                title="Mine",
                artist="A",
                album="A",
                status="uploaded",
                user_id=11,
            )
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            init = make_init_data("bot-token", 11)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(
                    "/api/review",
                    headers={"X-Telegram-Init-Data": init, "Origin": "https://music.example"},
                    params={"user_id": "22"},
                )
                data = await resp.json()
                self.assertTrue(data["ok"])
                titles = [t["title"] for t in data["tracks"]]
                self.assertEqual(titles, ["Mine"])
                steal = await client.post(
                    f"/api/review/{catalog.list_review_tracks(22)[0].id}/action",
                    headers={
                        "X-Telegram-Init-Data": init,
                        "Origin": "https://music.example",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps({"action": "library", "user_id": 22}),
                )
                self.assertEqual(steal.status, 404)
                me = await client.get(
                    "/api/me",
                    headers={"X-Telegram-Init-Data": init, "Origin": "https://music.example"},
                )
                body = await me.json()
                blob = json.dumps(body)
                self.assertNotIn("refresh_token", blob)
                self.assertNotIn("access_token", blob)
