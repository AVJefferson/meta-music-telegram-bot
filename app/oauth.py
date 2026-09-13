from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app.drive import LIBRARY_FOLDER, REVIEW_FOLDER, DriveClient
from app.errors import AppError
from app.models import Ctx

log = logging.getLogger(__name__)
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.install",
]


def public_base_url(settings) -> str:
    url = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if not url:
        raise AppError("unavailable")
    lower = url.lower()
    if not (lower.startswith("https://") or lower.startswith("http://localhost") or lower.startswith("http://127.0.0.1")):
        raise AppError("unavailable")
    return url


def redirect_uri(settings) -> str:
    return f"{public_base_url(settings)}/oauth/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _expires(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def issue_login_ticket(ctx: Ctx, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    ttl = int(getattr(ctx.settings, "oauth_ticket_ttl_seconds", 600) or 600)
    ctx.catalog.create_oauth_ticket(token, user_id, _expires(ttl))
    return f"{public_base_url(ctx.settings)}/oauth/start?token={token}"


def google_auth_url(ctx: Ctx, user_id: int) -> str:
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    ttl = int(getattr(ctx.settings, "oauth_ticket_ttl_seconds", 600) or 600)
    ctx.catalog.create_oauth_state(state, user_id, verifier, _expires(ttl))
    params = {
        "client_id": (ctx.settings.google_client_id or "").strip(),
        "redirect_uri": redirect_uri(ctx.settings),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_URI}?{urlencode(params)}"


async def exchange_code(ctx: Ctx, *, code: str, verifier: str) -> dict[str, Any]:
    data = {
        "code": code,
        "client_id": (ctx.settings.google_client_id or "").strip(),
        "redirect_uri": redirect_uri(ctx.settings),
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    secret = (ctx.settings.google_client_secret or "").strip()
    if secret:
        data["client_secret"] = secret
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(TOKEN_URI, data=data)
    if response.status_code >= 400:
        log.warning("oauth token exchange failed status=%s", response.status_code)
        raise AppError("needs_login")
    payload = response.json()
    if not payload.get("refresh_token"):
        raise AppError("needs_login")
    return payload


def _email_from_id_token(id_token: str) -> str:
    try:
        payload = id_token.split(".")[1]
        pad = "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + pad))
        return str(data.get("email") or "")
    except Exception:
        return ""


async def finish_login(ctx: Ctx, user_id: int, token_payload: dict[str, Any]) -> None:
    existing = ctx.catalog.ensure_user(user_id)
    refresh = str(token_payload.get("refresh_token") or "")
    email = _email_from_id_token(str(token_payload.get("id_token") or ""))
    if existing.google_refresh_token and existing.google_email and email and existing.google_email != email:
        raise AppError("forbidden")
    client = DriveClient.from_refresh(
        client_id=(ctx.settings.google_client_id or "").strip(),
        client_secret=(ctx.settings.google_client_secret or "").strip(),
        refresh_token=refresh,
        access_token=str(token_payload.get("access_token") or "") or None,
    )
    if not email:
        email = client.email
    library_id, _link = client.ensure_named_folder(LIBRARY_FOLDER)
    review_id, _rlink = client.ensure_named_folder(REVIEW_FOLDER)
    ctx.catalog.update_user(
        user_id,
        google_refresh_token=refresh,
        google_email=email,
        gdrive_folder_id=library_id,
        gdrive_review_folder_id=review_id,
    )
    hub = getattr(ctx, "drive", None)
    drop = getattr(hub, "drop", None)
    if callable(drop):
        drop(user_id)


def validate_origin(settings, origin: str | None) -> None:
    if not origin:
        return
    base = (getattr(settings, "public_base_url", None) or "").rstrip("/")
    if origin.rstrip("/") != base:
        raise AppError("unauthorized")


def telegram_init_data_user(bot_token: str, init_data: str, *, max_age: int) -> int:
    from app.initdata import parse_init_data

    return parse_init_data(bot_token, init_data, max_age=max_age)
