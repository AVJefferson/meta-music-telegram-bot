from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.errors import AppError
from app.models import Ctx

log = logging.getLogger(__name__)

_NEXT = ("next", "prev", "previous", "➡️", "⬅️", "»", "«", "▶", "◀")
_lock = asyncio.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_hifi_alerted = False


def is_nav_label(text: str) -> bool:
    folded = (text or "").strip().casefold()
    if not folded:
        return True
    return any(token in folded for token in _NEXT)


def strip_hifi_keyboard(rows: list[list[Any]]) -> list[tuple[str, str]]:
    """Return (label, hifi_callback) minus next/prev."""
    out: list[tuple[str, str]] = []
    for row in rows:
        for button in row:
            text = str(getattr(button, "text", None) or (button.get("text") if isinstance(button, dict) else "") or "")
            data = str(
                getattr(button, "data", None)
                or getattr(getattr(button, "callback_data", None), "data", None)
                or (button.get("callback_data") if isinstance(button, dict) else "")
                or ""
            )
            if is_nav_label(text) or not data:
                continue
            out.append((text, data))
    return out


def user_keyboard(ctx: Ctx, user_id: int, session_id: str, picks: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    rows: list[list[InlineKeyboardButton]] = []
    for label, callback in picks[:40]:
        pick_id = secrets.token_urlsafe(8)
        ctx.catalog.put_hifi_pick(pick_id, user_id, session_id, callback, label, expires)
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"hf:{pick_id}")])
    rows.append(
        [
            InlineKeyboardButton(text="More", callback_data=f"hfmore:{session_id}"),
            InlineKeyboardButton(text="Cancel", callback_data=f"hfcancel:{session_id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def search_songs(ctx: Ctx, user_id: int, query: str) -> InlineKeyboardMarkup:
    max_q = int(getattr(ctx.settings, "hifi_queue_max", 8) or 8)
    if _lock.locked() and len(_sessions) >= max_q:
        raise AppError("busy")
    async with _lock:
        session_id, picks = await _search_locked(ctx, user_id, query)
    return user_keyboard(ctx, user_id, session_id, picks)


async def _search_locked(ctx: Ctx, user_id: int, query: str) -> tuple[str, list[tuple[str, str]]]:
    session_id = secrets.token_urlsafe(8)
    client = await _client(ctx)
    if client is None:
        raise AppError("unavailable")
    timeout = int(getattr(ctx.settings, "hifi_timeout_seconds", 90) or 90)
    username = str(getattr(ctx.settings, "hifi_bot_username", "HiFiAudioBot") or "HiFiAudioBot")
    rows = await asyncio.wait_for(client.search(username, query), timeout=timeout)
    picks = strip_hifi_keyboard(rows)
    nav = [btn for row in rows for btn in row if is_nav_label(str(getattr(btn, "text", None) or ""))]
    _sessions[session_id] = {"user_id": user_id, "client": client, "nav": nav, "username": username}
    if not picks:
        raise AppError("not_found")
    return session_id, picks


async def more_results(ctx: Ctx, user_id: int, session_id: str) -> InlineKeyboardMarkup:
    session = _sessions.get(session_id)
    if not session or session.get("user_id") != user_id:
        raise AppError("not_found")
    client = session["client"]
    nav = session.get("nav") or []
    next_btn = None
    for button in nav:
        text = str(getattr(button, "text", None) or "").casefold()
        if "next" in text or "»" in text:
            next_btn = button
            break
    if next_btn is None:
        raise AppError("not_found")
    data = str(getattr(next_btn, "data", None) or getattr(next_btn, "callback_data", None) or "")
    timeout = int(getattr(ctx.settings, "hifi_timeout_seconds", 90) or 90)
    async with _lock:
        rows = await asyncio.wait_for(client.click(data), timeout=timeout)
    picks = strip_hifi_keyboard(rows)
    return user_keyboard(ctx, user_id, session_id, picks)


async def pick_song(ctx: Ctx, user_id: int, pick_id: str) -> str:
    found = ctx.catalog.get_hifi_pick(pick_id, user_id)
    if not found:
        raise AppError("not_found")
    session_id, callback, _label = found
    session = _sessions.get(session_id)
    if not session or session.get("user_id") != user_id:
        raise AppError("not_found")
    client = session["client"]
    timeout = int(getattr(ctx.settings, "hifi_timeout_seconds", 90) or 90)
    async with _lock:
        path = await asyncio.wait_for(client.download(callback), timeout=timeout)
    return path


async def _alert_hifi_missing(ctx: Ctx) -> None:
    global _hifi_alerted
    if _hifi_alerted:
        return
    _hifi_alerted = True
    from app.notify import alert_admin

    await alert_admin(
        ctx,
        "HiFi session missing or unauthorized. Search is down. "
        "Run: docker compose run --rm -it bot python -m app.hifi_login",
    )


async def _client(ctx: Ctx):
    factory = getattr(ctx, "hifi_factory", None)
    if callable(factory):
        return await factory()
    cached = getattr(ctx, "hifi_client", None)
    tele = getattr(cached, "client", None) if cached is not None else None
    connected = False
    if tele is not None:
        check = getattr(tele, "is_connected", None)
        connected = bool(check() if callable(check) else check)
    if cached is not None and connected:
        return cached
    from app.hifi_telethon import TelethonHifi

    try:
        client = await TelethonHifi.connect(ctx)
    except AppError:
        await _alert_hifi_missing(ctx)
        raise
    ctx.hifi_client = client
    return client


def build_hifi_router() -> Router:
    from aiogram import F
    from aiogram.types import CallbackQuery

    from app.ephemeral import edit_private_markup
    from app.membership import allow_from_callback, check_upload_rate, touch
    from app.notify import notify_error

    router = Router()

    @router.callback_query(F.data.startswith("hf:"))
    async def on_pick(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.from_user:
            await callback.answer()
            return
        user_id = callback.from_user.id
        if not await allow_from_callback(ctx, callback):
            await callback.answer("Not allowed.", show_alert=True)
            return
        touch(ctx, user_id, callback.from_user)
        pick_id = (callback.data or "")[3:]
        try:
            check_upload_rate(ctx, user_id)
            path = await pick_song(ctx, user_id, pick_id)
        except AppError as exc:
            await callback.answer(exc.user_message, show_alert=True)
            return
        except Exception as exc:
            await notify_error(ctx, user_id, exc)
            await callback.answer("Failed.", show_alert=True)
            return
        from app.formats import is_allowed_audio, user_allowed_formats
        from app.intake import ingest_local_audio

        file_name = Path(path).name or "track.flac"
        if not is_allowed_audio(file_name, "", user_allowed_formats(ctx, user_id)):
            await callback.answer("That format is off in Settings.", show_alert=True)
            return
        await callback.answer()
        chat = callback.message.chat if callback.message else None
        if chat is None:
            return
        await ingest_local_audio(ctx, chat=chat, user_id=user_id, path=path, file_name=file_name)

    @router.callback_query(F.data.startswith("hfmore:"))
    async def on_more(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.from_user:
            await callback.answer()
            return
        if not await allow_from_callback(ctx, callback):
            await callback.answer("Not allowed.", show_alert=True)
            return
        session_id = (callback.data or "").split(":", 1)[-1]
        try:
            markup = await more_results(ctx, callback.from_user.id, session_id)
        except AppError as exc:
            await callback.answer(exc.user_message, show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await edit_private_markup(
                ctx,
                chat_id=callback.message.chat.id,
                user_id=callback.from_user.id,
                message=callback.message,
                reply_markup=markup,
                text="More results",
            )

    @router.callback_query(F.data.startswith("hfcancel:"))
    async def on_cancel(callback: CallbackQuery, ctx: Ctx) -> None:
        session_id = (callback.data or "").split(":", 1)[-1]
        session = _sessions.get(session_id)
        if session and callback.from_user and session.get("user_id") != callback.from_user.id:
            await callback.answer("Not your search.", show_alert=True)
            return
        _sessions.pop(session_id, None)
        await callback.answer("Cancelled")
        if callback.message and callback.from_user:
            await edit_private_markup(
                ctx,
                chat_id=callback.message.chat.id,
                user_id=callback.from_user.id,
                message=callback.message,
                reply_markup=None,
            )

    return router
