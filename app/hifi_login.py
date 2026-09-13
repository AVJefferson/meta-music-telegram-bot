"""One-time Telethon login for HiFiAudioBot proxy.

  docker compose run --rm -it bot python -m app.hifi_login

Use a dummy USER account (phone + code). Never paste BOT_TOKEN — bots cannot
message HiFiAudioBot (USER_BOT_TO_BOT_DISABLED).

Treat /data/hifi.session as a full Telegram account secret.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings


def looks_like_bot_token(value: str) -> bool:
    raw = (value or "").strip()
    if ":" not in raw:
        return False
    left, right = raw.split(":", 1)
    return left.isdigit() and len(right) >= 20


def require_phone(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise SystemExit("Phone number required")
    if looks_like_bot_token(raw):
        raise SystemExit("That is a bot token. HiFi needs a USER phone number (dummy account).")
    return raw


def _ask_phone() -> str:
    return require_phone(input("USER phone (dummy account), not BOT_TOKEN: "))


def _unlink_session(path: Path) -> None:
    for candidate in (path, path.with_suffix(".session")):
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


async def main() -> None:
    settings = Settings()
    api_id = int(settings.telegram_api_id or 0)
    api_hash = str(settings.telegram_api_hash or "")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
    path = Path(settings.hifi_session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from telethon import TelegramClient

    client = TelegramClient(str(path.with_suffix("")), api_id, api_hash)
    await client.start(phone=_ask_phone)
    me = await client.get_me()
    if getattr(me, "bot", False):
        await client.disconnect()
        _unlink_session(path)
        raise SystemExit(
            "Logged in as a bot. Delete that and retry with a user phone. "
            "Never use BOT_TOKEN for HiFi."
        )
    await client.disconnect()
    try:
        session = path if path.exists() else path.with_suffix(".session")
        if session.exists():
            session.chmod(0o600)
    except OSError:
        pass
    print(f"HiFi session saved for user {getattr(me, 'username', None) or me.id}")


if __name__ == "__main__":
    asyncio.run(main())
