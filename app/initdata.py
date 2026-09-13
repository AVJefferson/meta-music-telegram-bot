from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, unquote

from app.errors import AppError


def parse_init_data(bot_token: str, init_data: str, *, max_age: int = 300) -> int:
    if not init_data or not bot_token:
        raise AppError("unauthorized")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", "")
    if not received:
        raise AppError("unauthorized")
    data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, received):
        raise AppError("unauthorized")
    try:
        auth_date = int(pairs.get("auth_date") or 0)
    except ValueError:
        raise AppError("unauthorized") from None
    if max_age and auth_date and abs(time.time() - auth_date) > max_age:
        raise AppError("unauthorized")
    raw_user = pairs.get("user") or ""
    try:
        user = json.loads(unquote(raw_user))
    except (TypeError, ValueError):
        raise AppError("unauthorized") from None
    user_id = user.get("id")
    if not user_id:
        raise AppError("unauthorized")
    return int(user_id)
