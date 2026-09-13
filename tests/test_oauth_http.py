from __future__ import annotations

import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from app.errors import AppError
from app.http_app import create_http_app
from app.oauth import exchange_code, finish_login, google_auth_url, issue_login_ticket, redirect_uri
from tests.support import make_ctx, temp_catalog


class OauthHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_ticket_no_redirect(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/oauth/start", params={"token": "missing"}, allow_redirects=False)
                body = await resp.text()
            self.assertEqual(resp.status, 404)
            self.assertNotIn("accounts.google.com", body)

    async def test_ticket_replay(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            url = issue_login_ticket(ctx, 9)
            token = url.rsplit("token=", 1)[-1]
            self.assertEqual(catalog.consume_oauth_ticket(token), 9)
            self.assertIsNone(catalog.consume_oauth_ticket(token))

    async def test_host_header_ignored_for_redirect_uri(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            self.assertEqual(redirect_uri(ctx.settings), "https://music.example/oauth/callback")
            url = google_auth_url(ctx, 3)
            self.assertIn("music.example", url)
            self.assertNotIn("evil.com", url)
            token_url = issue_login_ticket(ctx, 3)
            token = token_url.rsplit("token=", 1)[-1]
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(
                    "/oauth/start",
                    params={"token": token, "folder_id": "attacker-folder"},
                    headers={"Host": "evil.com"},
                    allow_redirects=False,
                )
            self.assertIn(resp.status, {302, 303, 307})
            loc = resp.headers.get("Location", "")
            self.assertIn("accounts.google.com", loc)
            self.assertIn("music.example", loc)
            self.assertNotIn("evil.com", loc)
            self.assertNotIn("attacker-folder", loc)

    async def test_callback_wrong_state_stores_nothing(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            catalog.ensure_user(4)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/oauth/callback", params={"state": "nope", "code": "x"})
            self.assertEqual(resp.status, 404)
            user = catalog.get_user(4)
            self.assertFalse(user.logged_in if user else True)

    async def test_pkce_sends_verifier(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            captured: dict = {}

            class FakeResp:
                status_code = 200

                def json(self):
                    return {"refresh_token": "rt", "access_token": "at", "id_token": ""}

            async def fake_post(url, data=None):
                captured.update(data)
                return FakeResp()

            class FakeClient:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    return None

                post = staticmethod(fake_post)

            with patch("app.oauth.httpx.AsyncClient", return_value=FakeClient()):
                payload = await exchange_code(ctx, code="authcode", verifier="the-verifier")
            self.assertEqual(captured["code_verifier"], "the-verifier")
            self.assertEqual(captured["redirect_uri"], "https://music.example/oauth/callback")
            self.assertEqual(payload["refresh_token"], "rt")

    async def test_second_google_email_refused(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            catalog.ensure_user(5)
            catalog.update_user(5, google_refresh_token="old", google_email="a@x.com")
            with self.assertRaises(AppError) as err:
                await finish_login(ctx, 5, {"refresh_token": "new", "id_token": _id_token("b@x.com")})
            self.assertEqual(err.exception.code, "forbidden")
            self.assertEqual(catalog.get_user(5).google_refresh_token, "old")

    async def test_callback_html_has_no_secret(self) -> None:
        directory, catalog = temp_catalog()
        with directory:
            ctx = make_ctx(catalog)
            app = create_http_app(ctx)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/oauth/callback", params={"error": "access_denied"})
                text = await resp.text()
            self.assertNotIn("csec", text)
            self.assertNotIn("client_secret", text.casefold())


def _id_token(email: str) -> str:
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).rstrip(b"=").decode()
    return f"e.{payload}.x"
