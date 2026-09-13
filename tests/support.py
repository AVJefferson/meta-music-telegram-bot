from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from app.catalog import Catalog
from app.models import Ctx


def make_init_data(bot_token: str, user_id: int, *, auth_date: int | None = None, extra_user: dict | None = None) -> str:
    user = {"id": user_id}
    if extra_user:
        user.update(extra_user)
    payload = {"auth_date": str(auth_date if auth_date is not None else int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    data_check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    payload["hash"] = digest
    return urlencode(payload)


def temp_catalog() -> tuple[tempfile.TemporaryDirectory, Catalog]:
    directory = tempfile.TemporaryDirectory()
    catalog = Catalog(Path(directory.name) / "state.sqlite")
    return directory, catalog


def settings(**overrides):
    data = dict(
        bot_token="bot-token",
        admin_telegram_user_id=1,
        acoustid_api_key="k",
        public_base_url="https://music.example",
        google_client_id="cid",
        google_client_secret="csec",
        oauth_ticket_ttl_seconds=600,
        initdata_max_age_seconds=300,
        dm_requires_known_chat=True,
        user_inactive_months=6,
        lastfm_api_key="",
        gdrive_folder_id="",
        gdrive_review_folder_id="",
        google_refresh_token="",
        cache_root=Path("/tmp/cache"),
        library_root=Path("/tmp/lib"),
        review_root=Path("/tmp/rev"),
        pending_root=Path("/tmp/pend"),
        tmp_root=Path("/tmp/tmp"),
        oauth_http_port=8080,
        rate_api_per_minute=60,
        rate_search_per_minute=8,
        hifi_queue_max=8,
        hifi_timeout_seconds=90,
        hifi_bot_username="HiFiAudioBot",
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def make_ctx(catalog: Catalog, **overrides) -> Ctx:
    return Ctx(
        settings=settings(**overrides),
        catalog=catalog,
        drive=SimpleNamespace(),
        http=None,
        genre=SimpleNamespace(),
        bot=SimpleNamespace(),
        mb=None,
        jobs=None,
    )
