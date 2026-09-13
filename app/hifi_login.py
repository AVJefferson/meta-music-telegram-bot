"""One-time Telethon login for HiFiAudioBot proxy.

  docker compose run --rm -it bot python -m app.hifi_login

Treat /data/hifi.session as a full Telegram account secret.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings


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
    await client.start()
    me = await client.get_me()
    await client.disconnect()
    try:
        session = path if path.exists() else path.with_suffix(".session")
        if session.exists():
            session.chmod(0o600)
    except OSError:
        pass
    print(f"HiFi session saved for {getattr(me, 'username', None) or me.id}")


if __name__ == "__main__":
    asyncio.run(main())
