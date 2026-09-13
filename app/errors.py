from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_SECRET_RE = re.compile(
    r"(refresh_token|access_token|code_verifier|client_secret|bot_token|"
    r"authorization:\s*bearer)\s*[:=]\s*\S+",
    re.IGNORECASE,
)

USER_MESSAGES = {
    "unauthorized": "Not allowed.",
    "forbidden": "Not allowed.",
    "blacklisted": "Not allowed.",
    "not_found": "That item expired or was not found. Try again.",
    "rate_limited": "Slow down and retry in a minute.",
    "busy": "Busy. Retry in a moment.",
    "needs_login": "Google login expired. Use /login again.",
    "drive_quota": "Google Drive is out of space. Free some space, then retry.",
    "drive_denied": "Google Drive denied that action.",
    "unavailable": "Service unavailable. Retry shortly.",
    "timeout": "Timed out. Retry.",
    "conflict": "A file with that name already exists.",
    "bad_input": "Could not use that input.",
    "ephemeral_unsupported": "Open a private chat with the bot to continue.",
    "internal": "Failed. Retry.",
}

HTTP_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "blacklisted": 403,
    "not_found": 404,
    "rate_limited": 429,
    "busy": 429,
    "needs_login": 401,
    "drive_quota": 403,
    "drive_denied": 403,
    "unavailable": 503,
    "timeout": 504,
    "conflict": 409,
    "bad_input": 400,
    "ephemeral_unsupported": 400,
    "internal": 500,
}


class AppError(Exception):
    def __init__(self, code: str, *, cause: BaseException | None = None) -> None:
        self.code = code if code in USER_MESSAGES else "internal"
        self.cause = cause
        super().__init__(self.code)

    @property
    def user_message(self) -> str:
        return USER_MESSAGES[self.code]

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def as_json(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.user_message}


def redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text or "")


def to_app_error(exc: BaseException) -> AppError:
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    text = redact(str(exc)).lower()
    if "invalid_grant" in text or "use /login" in text:
        return AppError("needs_login", cause=exc)
    if "storagequotaexceeded" in text or "out of space" in text or "quota exceeded" in text:
        return AppError("drive_quota", cause=exc)
    if "drive denied" in text or "drive folder" in text:
        return AppError("drive_denied", cause=exc)
    if "flood" in text:
        return AppError("busy", cause=exc)
    if "timed out" in text or "timeout" in text:
        return AppError("timeout", cause=exc)
    return AppError("internal", cause=exc)


def log_error(
    code: str,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    pending_id: int | None = None,
    exc: BaseException | None = None,
) -> None:
    log.warning(
        "error code=%s user_id=%s chat_id=%s pending_id=%s",
        code,
        user_id,
        chat_id,
        pending_id,
        exc_info=bool(exc) and code == "internal",
    )
