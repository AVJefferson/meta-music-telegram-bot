from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.errors import AppError
from app.models import Ctx

log = logging.getLogger(__name__)


class TelethonHifi:
    def __init__(self, client, downloads: Path) -> None:
        self.client = client
        self.downloads = downloads
        self._last = None
        self._username = ""

    @classmethod
    async def connect(cls, ctx: Ctx) -> TelethonHifi:
        path = Path(getattr(ctx.settings, "hifi_session_path", "/data/hifi.session"))
        api_id = int(getattr(ctx.settings, "telegram_api_id", 0) or 0)
        api_hash = str(getattr(ctx.settings, "telegram_api_hash", "") or "")
        if not api_id or not api_hash:
            log.warning("hifi unavailable: TELEGRAM_API_ID/HASH missing")
            raise AppError("unavailable")
        session_file = path if path.suffix == ".session" else path.with_suffix(".session")
        if not path.exists() and not session_file.exists():
            log.warning("hifi unavailable: session file missing path=%s", path)
            raise AppError("unavailable")
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise AppError("unavailable") from exc
        client = TelegramClient(str(path.with_suffix("")), api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            log.warning("hifi unavailable: session not authorized")
            raise AppError("unavailable")
        try:
            path.chmod(0o600)
        except OSError:
            session = path.with_suffix(".session")
            if session.exists():
                session.chmod(0o600)
        return cls(client, Path(ctx.settings.tmp_root) / "hifi")

    def _rows(self, message):
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "rows", None) or []
        out = []
        for row in rows:
            buttons = []
            for button in getattr(row, "buttons", None) or []:
                buttons.append(button)
            if buttons:
                out.append(buttons)
        return out

    async def search(self, username: str, query: str):
        from telethon import events

        self._username = username
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        async def handler(event):
            if not fut.done():
                fut.set_result(event.message)

        self.client.add_event_handler(handler, events.NewMessage(from_users=username))
        try:
            await self.client.send_message(username, query)
            message = await asyncio.wait_for(fut, timeout=60)
        finally:
            self.client.remove_event_handler(handler)
        self._last = message
        return self._rows(message)

    async def click(self, data: str):
        if self._last is None:
            raise AppError("not_found")
        await self._last.click(data=data)
        await asyncio.sleep(1.5)
        message = await self.client.get_messages(self._last.chat_id, ids=self._last.id)
        self._last = message or self._last
        return self._rows(self._last)

    async def download(self, data: str) -> str:
        from telethon import events

        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        async def handler(event):
            msg = event.message
            if (msg.document or msg.audio or msg.file) and not fut.done():
                fut.set_result(msg)

        username = self._username or "HiFiAudioBot"
        self.client.add_event_handler(handler, events.NewMessage(from_users=username))
        try:
            await self.click(data)
            message = await asyncio.wait_for(fut, timeout=60)
        finally:
            self.client.remove_event_handler(handler)
        self.downloads.mkdir(parents=True, exist_ok=True)
        dest = await message.download_media(file=str(self.downloads))
        if not dest:
            raise AppError("unavailable")
        return dest
