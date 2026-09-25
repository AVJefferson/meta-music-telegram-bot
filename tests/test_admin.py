from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from app.admin_cmd import build_admin_router, format_chat_label, format_user_line
from app.catalog import Catalog
from app.http_app import create_http_app
from app.membership import allow_user
from app.models import ChatRecord, UserRecord
from tests.support import make_ctx, make_init_data, temp_catalog

ORIGIN = "https://music.example"


def _headers(user_id: int = 11, *, init: str | None = None, **extra) -> dict[str, str]:
    headers = {
        "X-Telegram-Init-Data": init if init is not None else make_init_data("bot-token", user_id),
        "Origin": ORIGIN,
    }
    headers.update(extra)
    return headers


def _named(router, observer_name: str, name: str):
    observer = getattr(router, observer_name)
    for handler in observer.handlers:
        callback = getattr(handler, "callback", handler)
        if getattr(callback, "__name__", "") == name:
            return callback
    raise AssertionError(f"{name} missing on {observer_name}")


def _user(**kwargs) -> UserRecord:
    data = dict(
        telegram_user_id=7,
        first_seen_at="2026-09-13T18:57:07+00:00",
        last_active_at="2026-09-13T18:57:07+00:00",
    )
    data.update(kwargs)
    return UserRecord(**data)


def _chat(**kwargs) -> ChatRecord:
    data = dict(
        chat_id=-5,
        type="supergroup",
        title="Band",
        active=True,
        added_at="2026-09-13T18:57:07+00:00",
        last_active_at="2026-09-13T18:57:07+00:00",
    )
    data.update(kwargs)
    return ChatRecord(**data)


class CatalogProfileTests(unittest.TestCase):
    def test_profile_columns_round_trip_and_migration(self) -> None:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "state.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE users (
              telegram_user_id INTEGER PRIMARY KEY,
              first_seen_at TEXT NOT NULL,
              last_active_at TEXT NOT NULL,
              songs_edited INTEGER NOT NULL DEFAULT 0,
              google_refresh_token TEXT,
              google_email TEXT,
              gdrive_folder_id TEXT,
              gdrive_review_folder_id TEXT,
              settings_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chats (
              chat_id INTEGER PRIMARY KEY,
              type TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              active INTEGER NOT NULL DEFAULT 1,
              added_at TEXT NOT NULL,
              last_active_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO users(telegram_user_id, first_seen_at, last_active_at) "
            "VALUES (4, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()
        catalog = Catalog(path)
        user_cols = {row[1] for row in catalog._conn.execute("PRAGMA table_info(users)")}
        chat_cols = {row[1] for row in catalog._conn.execute("PRAGMA table_info(chats)")}
        self.assertTrue({"username", "first_name", "last_name"} <= user_cols)
        self.assertIn("username", chat_cols)
        stored = catalog.touch_user(4, username="@neo", first_name="Neo", last_name="Anderson")
        self.assertEqual(stored.username, "neo")
        self.assertEqual(stored.first_name, "Neo")
        self.assertEqual(stored.last_name, "Anderson")
        catalog.upsert_chat(-5, type="channel", title="Feed", username="@feed")
        self.assertEqual(catalog.get_chat(-5).username, "feed")
        catalog.touch_user(4)
        catalog.upsert_chat(-5, type="channel", title="Feed 2")
        kept = catalog.get_user(4)
        chat = catalog.get_chat(-5)
        self.assertEqual(kept.username, "neo")
        self.assertEqual(kept.first_name, "Neo")
        self.assertEqual(chat.username, "feed")
        self.assertEqual(chat.title, "Feed 2")
        catalog.close()
        again = Catalog(path)
        self.assertEqual(again.get_user(4).last_name, "Anderson")
        self.assertEqual(again.get_chat(-5).username, "feed")
        again.close()
        directory.cleanup()


class FormatterTests(unittest.TestCase):
    def test_user_line_includes_username_and_name(self) -> None:
        line = format_user_line(_user(username="ada", first_name="Ada", last_name="Lovelace"))
        self.assertEqual(line, "<code>7</code> @ada Ada Lovelace since 26-09-13")

    def test_user_line_omits_null_username_and_name(self) -> None:
        line = format_user_line(_user())
        self.assertEqual(line, "<code>7</code> since 26-09-13")
        only_first = format_user_line(_user(first_name="Ada"))
        self.assertEqual(only_first, "<code>7</code> Ada since 26-09-13")
        self.assertNotIn("@", only_first)

    def test_chat_label_appends_username_or_omits_it(self) -> None:
        self.assertEqual(format_chat_label(_chat(title="Band", username="band")), "Band @band")
        self.assertEqual(format_chat_label(_chat(title="Band", username=None)), "Band")
        self.assertEqual(format_chat_label(_chat(title="", username=None)), "")
        self.assertEqual(format_chat_label(_chat(title="", username="only")), "@only")
        self.assertEqual(format_chat_label(_chat(title="<Band>", username="a<b")), "&lt;Band&gt; @a&lt;b")


class AdminGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_allow_and_my_chat_member_store_username(self) -> None:
        from app.bot import build_router

        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            chat = SimpleNamespace(id=-77, type="group", title="Room", username="room", full_name=None)
            self.assertTrue(await allow_user(ctx, 8, chat))
            self.assertEqual(catalog.get_chat(-77).username, "room")
            router = build_router(asyncio.Queue())
            handler = _named(router, "my_chat_member", "on_my_chat_member")
            event = SimpleNamespace(
                chat=SimpleNamespace(id=-88, type="channel", title="Feed", username="feed", full_name=None),
                new_chat_member=SimpleNamespace(status="member"),
            )
            await handler(event, ctx)
            row = catalog.get_chat(-88)
            self.assertEqual(row.username, "feed")
            self.assertEqual(row.title, "Feed")
            self.assertTrue(row.active)

    async def test_non_admin_listusers_sends_nothing(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            ctx.bot = SimpleNamespace(get_chat=AsyncMock(), get_chat_administrators=AsyncMock())
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=9, username="nope", first_name="Nope", last_name=None),
                chat=SimpleNamespace(id=9),
            )
            handler = _named(build_admin_router(), "message", "listusers")
            with patch("app.admin_cmd.send_private", AsyncMock()) as send:
                await handler(message, ctx)
            send.assert_not_awaited()
            ctx.bot.get_chat.assert_not_awaited()

    async def test_non_admin_callback_rejected(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            handler = _named(build_admin_router(), "callback_query", "admin_cb")
            answer = AsyncMock()
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=9),
                data="ad:bu:3",
                answer=answer,
            )
            await handler(callback, ctx)
            answer.assert_awaited_with("Not allowed.", show_alert=True)
            self.assertFalse(catalog.is_user_blacklisted(3))

    async def test_blank_chat_refreshed_only_for_admin(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.upsert_chat(-42, type="supergroup", title="", username="")
            ctx = make_ctx(catalog)
            remote = SimpleNamespace(id=-42, type="supergroup", title="Studio", username="studio")
            ctx.bot = SimpleNamespace(
                get_chat=AsyncMock(return_value=remote),
                get_chat_administrators=AsyncMock(return_value=[]),
            )
            stranger = SimpleNamespace(
                from_user=SimpleNamespace(id=9, username=None, first_name=None, last_name=None),
                chat=SimpleNamespace(id=9),
            )
            admin = SimpleNamespace(
                from_user=SimpleNamespace(id=1, username="root", first_name="Root", last_name=None),
                chat=SimpleNamespace(id=1),
            )
            groups = _named(build_admin_router(), "message", "listgroups")
            with patch("app.admin_cmd.send_private", AsyncMock()) as send:
                await groups(stranger, ctx)
            send.assert_not_awaited()
            ctx.bot.get_chat.assert_not_awaited()
            self.assertEqual(catalog.get_chat(-42).title, "")
            sent: list[dict] = []

            async def capture(_ctx, **kwargs):
                sent.append(kwargs)

            with patch("app.admin_cmd.send_private", capture):
                await groups(admin, ctx)
            ctx.bot.get_chat.assert_awaited_once()
            stored = catalog.get_chat(-42)
            self.assertEqual(stored.title, "Studio")
            self.assertEqual(stored.username, "studio")
            self.assertIn("@studio", sent[0]["text"])
            self.assertIn("Studio", sent[0]["text"])


class AdminHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_me_stores_initdata_profile_without_admin_lists(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.touch_user(11, username="old", first_name="Old", last_name="Name")
            catalog.touch_user(1, username="root", first_name="Root", last_name=None)
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            init = make_init_data(
                "bot-token",
                11,
                extra_user={"username": "sam", "first_name": "Sam", "last_name": "Lee"},
            )
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/me", headers=_headers(init=init))
                data = await resp.json()
            self.assertTrue(data["ok"])
            self.assertIs(data["admin"], False)
            for key in ("users", "groups", "channels", "server", "counts", "activity", "usage"):
                self.assertNotIn(key, data)
            user = catalog.get_user(11)
            self.assertEqual(user.username, "sam")
            self.assertEqual(user.first_name, "Sam")
            self.assertEqual(user.last_name, "Lee")

    async def test_admin_overview_and_rejections(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            catalog.touch_user(11, username="sam", first_name="Sam", last_name="Lee")
            catalog.update_user(11, songs_edited=4, google_refresh_token="secret-token")
            catalog.upsert_chat(-100, type="supergroup", title="Band", username="band")
            catalog.upsert_chat(-200, type="channel", title="News", username="news")
            catalog.blacklist_user(99)
            catalog.blacklist_chat(-200)
            catalog.insert_pending(
                kind="library",
                mb_recording_id=None,
                acoustid=None,
                local_path="",
                sidecar_path=None,
                relative_path="a.flac",
                bit_depth=None,
                sample_rate=None,
                title="Song",
                artist="A",
                album="B",
                status="uploaded",
                user_id=11,
            )
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                admin = await client.get("/api/admin/overview", headers=_headers(1))
                admin_body = await admin.json()
                other = await client.get("/api/admin/overview?admin=true", headers=_headers(11))
                other_body = await other.json()
                bad = await client.get(
                    "/api/admin/overview",
                    headers=_headers(init="auth_date=1&user=%7B%22id%22%3A11%7D&hash=deadbeef"),
                )
                bad_body = await bad.json()
                catalog.blacklist_user(11)
                blocked = await client.get("/api/admin/overview", headers=_headers(11))
                blocked_body = await blocked.json()
            self.assertEqual(admin.status, 200)
            self.assertTrue(admin_body["ok"])
            self.assertGreaterEqual(admin_body["counts"]["users"], 1)
            self.assertEqual(admin_body["counts"]["groups"], 1)
            self.assertEqual(admin_body["counts"]["channels"], 1)
            self.assertEqual(admin_body["counts"]["blacklisted"], 2)
            row = next(item for item in admin_body["users"] if item["id"] == 11)
            self.assertEqual(row["username"], "sam")
            self.assertEqual(row["name"], "Sam Lee")
            self.assertEqual(row["songs_edited"], 4)
            self.assertNotIn("google_refresh_token", row)
            channel = admin_body["channels"][0]
            self.assertEqual(channel["username"], "news")
            self.assertTrue(channel["blocked"])
            self.assertEqual(channel["title"], "News")
            self.assertEqual(len(admin_body["activity"]["hours"]), 24)
            self.assertGreaterEqual(admin_body["activity"]["active_24h"], 1)
            self.assertGreaterEqual(admin_body["activity"]["active_7d"], 1)
            self.assertEqual(len(admin_body["usage"]["days"]), 14)
            self.assertGreaterEqual(sum(day["count"] for day in admin_body["usage"]["days"]), 1)
            self.assertGreater(sum(admin_body["activity"]["hours"]), 0)
            server = admin_body["server"]
            self.assertGreater(server["rss_bytes"], 0)
            self.assertGreater(server["sqlite_bytes"], 0)
            self.assertGreaterEqual(server["uptime_seconds"], 0)
            blob = json.dumps(admin_body)
            self.assertNotIn("secret-token", blob)
            self.assertEqual(other.status, 403)
            self.assertEqual(set(other_body), {"ok", "code", "error"})
            self.assertFalse(other_body["ok"])
            self.assertIn(bad.status, (401, 403))
            self.assertFalse(bad_body.get("ok"))
            for key in ("users", "groups", "channels", "server", "counts"):
                self.assertNotIn(key, bad_body)
            self.assertEqual(blocked.status, 403)
            self.assertNotIn("users", blocked_body)
            self.assertNotIn("server", blocked_body)
