from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.errors import AppError, redact, to_app_error
from app.membership import allow_user, clear_cache, is_admin, is_forum_member, user_is_blocked
from app.rate_limit import check_rate, reset


class EphemeralFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_ephemeral_fail_falls_back_to_dm(self) -> None:
        from aiogram.exceptions import TelegramBadRequest

        from app.ephemeral import send_private

        sent: list[tuple] = []

        async def call(method):
            raise TelegramBadRequest(method=method, message="ephemeral unsupported")

        async def send_message(chat_id, text, **kwargs):
            sent.append((chat_id, text))
            return SimpleNamespace(message_id=1, chat=SimpleNamespace(id=chat_id))

        ctx = SimpleNamespace(bot=SimpleNamespace(__call__=call, send_message=send_message))
        msg = await send_private(ctx, chat_id=-100, user_id=9, text="hi")
        self.assertEqual(sent, [(9, "hi")])
        self.assertIsNotNone(msg)


class ErrorRedactTests(unittest.TestCase):
    def test_redacts_tokens(self) -> None:
        text = redact("refresh_token=ya29.abc access_token: tok BOT_TOKEN=123")
        self.assertNotIn("ya29", text)
        self.assertIn("[redacted]", text)

    def test_invalid_grant_is_needs_login(self) -> None:
        err = to_app_error(RuntimeError("invalid_grant: Bad Request"))
        self.assertEqual(err.code, "needs_login")
        self.assertNotIn("refresh_token", err.user_message)

    def test_quota_maps(self) -> None:
        err = to_app_error(RuntimeError("storageQuotaExceeded"))
        self.assertEqual(err.code, "drive_quota")

    def test_json_envelope(self) -> None:
        payload = AppError("internal").as_json()
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["code"], "internal")
        self.assertNotIn("traceback", str(payload).casefold())


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def test_trips_then_blocks(self) -> None:
        for _ in range(2):
            check_rate("t", 9, 2)
        with self.assertRaises(AppError) as ctx:
            check_rate("t", 9, 2)
        self.assertEqual(ctx.exception.code, "rate_limited")


class MembershipTenancyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_cache()

    async def test_admin_never_blocked(self) -> None:
        catalog = SimpleNamespace(
            is_user_blacklisted=lambda uid: True,
            list_known_chat_ids=lambda: [],
            is_chat_blacklisted=lambda cid: False,
            get_chat=lambda cid: None,
        )
        ctx = SimpleNamespace(settings=SimpleNamespace(admin_telegram_user_id=7, dm_requires_known_chat=True), catalog=catalog, bot=None)
        self.assertTrue(is_admin(ctx, 7))
        self.assertFalse(await user_is_blocked(ctx, 7))
        self.assertTrue(await allow_user(ctx, 7, SimpleNamespace(id=7, type="private")))

    async def test_blacklisted_user_denied(self) -> None:
        catalog = SimpleNamespace(
            is_user_blacklisted=lambda uid: uid == 42,
            list_known_chat_ids=lambda: [-100],
            is_chat_blacklisted=lambda cid: False,
            get_chat=lambda cid: SimpleNamespace(active=True),
            upsert_chat=lambda *a, **k: None,
        )
        ctx = SimpleNamespace(settings=SimpleNamespace(admin_telegram_user_id=1, dm_requires_known_chat=True), catalog=catalog, bot=None)
        self.assertFalse(await allow_user(ctx, 42, SimpleNamespace(id=-100, type="supergroup", title="g")))

    async def test_dm_requires_known_chat(self) -> None:
        calls: list[tuple[int, int]] = []

        async def get_chat_member(chat_id: int, user_id: int):
            calls.append((chat_id, user_id))
            return SimpleNamespace(status="member")

        catalog = SimpleNamespace(
            is_user_blacklisted=lambda uid: False,
            list_known_chat_ids=lambda: [-100123],
            is_chat_blacklisted=lambda cid: False,
            get_chat=lambda cid: SimpleNamespace(active=True),
        )
        ctx = SimpleNamespace(
            settings=SimpleNamespace(admin_telegram_user_id=1, dm_requires_known_chat=True),
            catalog=catalog,
            bot=SimpleNamespace(get_chat_member=get_chat_member),
        )
        self.assertTrue(await is_forum_member(ctx, 42))
        self.assertEqual(calls, [(-100123, 42)])
        self.assertTrue(await is_forum_member(ctx, 42))
        self.assertEqual(len(calls), 1)
