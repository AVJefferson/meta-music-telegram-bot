from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import logging
import shutil
import socket
import ssl
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from PIL import Image

from app.botapi import discard_download
from app.edit_ui import EDITOR_FIELDS, current_edit_field, handle_edit_text, show_field_menu
from app.genre import genre_tokens
from app.membership import is_forum_member
from app.models import Ctx, Identity, Job, PendingReview, tagset_from_dict
from app.tags import audio_info, normalize_tagset, read_cover, read_tagset, write_tags
from app.util import format_bytes, html_esc, sanitize_filename

log = logging.getLogger(__name__)
PAGE_SIZE = 8
TOPIC_PAGE_SIZE = 10


class FlacMessageFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        from app.bot import is_flac_message

        return is_flac_message(message)


def _expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")


def _loads(value: str | None, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _next_editor_phase(index: int) -> str:
    next_index = index + 1
    return f"edit:{next_index}" if next_index < len(EDITOR_FIELDS) else "edit:cover"


def _previous_editor_phase(phase: str) -> str:
    if phase == "edit:cover":
        return f"edit:{len(EDITOR_FIELDS) - 1}"
    if phase == "edit:confirm":
        return "edit:cover"
    return f"edit:{max(0, int(phase.split(':', 1)[1]) - 1)}"


def _control_keyboard(
    pending_id: int, *, back: bool = True, confirm: bool = False
) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if back:
        row.append(InlineKeyboardButton(text="Back", callback_data=f"d{pending_id}:back"))
    if confirm:
        row.append(InlineKeyboardButton(text="Confirm", callback_data=f"d{pending_id}:confirm"))
    row.append(InlineKeyboardButton(text="Cancel", callback_data=f"d{pending_id}:cancel"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def is_authorized_private(message: Message, ctx: Ctx) -> bool:
    if message.chat.type != "private" or not message.from_user:
        return False
    return await is_forum_member(ctx, message.from_user.id)


async def _require_private(message: Message, ctx: Ctx) -> bool:
    if await is_authorized_private(message, ctx):
        return True
    await message.reply("Private access requires current membership in configured forum group.")
    return False


def _topic_keyboard(
    pending_id: int, topics: list[tuple[int, str]], page: int
) -> InlineKeyboardMarkup:
    pages = max(1, (len(topics) + TOPIC_PAGE_SIZE - 1) // TOPIC_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * TOPIC_PAGE_SIZE
    rows = [
        [
            InlineKeyboardButton(
                text=name[:50], callback_data=f"d{pending_id}:topic:{thread_id}"
            )
        ]
        for thread_id, name in topics[start : start + TOPIC_PAGE_SIZE]
    ]
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"d{pending_id}:topics:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"d{pending_id}:topics:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=f"d{pending_id}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_topics(ctx: Ctx, row: PendingReview, page: int = 0) -> None:
    topics = ctx.catalog.list_topics()
    text = (
        f"Choose library topic for <code>{html_esc(row.file_name)}</code>."
        if topics
        else "No forum topics recorded yet. Send a message in each topic, then retry."
    )
    markup = _topic_keyboard(row.id, topics, page)
    await ctx.bot.edit_message_text(
        text,
        chat_id=row.chat_id,
        message_id=row.status_message_id,
        parse_mode="HTML",
        reply_markup=markup,
    )


def _review_keyboard(pending_id: int, count: int, page: int) -> InlineKeyboardMarkup:
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    rows = [
        [
            InlineKeyboardButton(
                text=f"Edit {index + 1}", callback_data=f"d{pending_id}:review:{index}"
            )
        ]
        for index in range(start, min(start + PAGE_SIZE, count))
    ]
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton(text="Previous", callback_data=f"d{pending_id}:reviews:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next", callback_data=f"d{pending_id}:reviews:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Cancel", callback_data=f"d{pending_id}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_reviews(ctx: Ctx, row: PendingReview, page: int = 0) -> None:
    items = _loads(row.candidates_json, [])
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    lines = [f"<b>Drive review list</b> — page {page + 1}/{pages}", ""]
    for index, item in enumerate(items[start : start + PAGE_SIZE], start=start):
        relative = str(item.get("relative_path", item.get("name", "")))
        if len(relative) > 320:
            relative = "…" + relative[-319:]
        lines.append(
            f"{index + 1}. <code>{html_esc(relative)}</code>"
            f" — {html_esc(format_bytes(item.get('size')))}"
        )
    await ctx.bot.edit_message_text(
        "\n".join(lines),
        chat_id=row.chat_id,
        message_id=row.status_message_id,
        parse_mode="HTML",
        reply_markup=_review_keyboard(row.id, len(items), page),
    )


async def show_editor_prompt(ctx: Ctx, row: PendingReview) -> None:
    await show_field_menu(ctx, row)


async def _show_cover_prompt(ctx: Ctx, row: PendingReview) -> None:
    from app.edit_ui import show_cover_prompt

    await show_cover_prompt(ctx, row)


async def _show_confirm(ctx: Ctx, row: PendingReview) -> None:
    from app.queue import edit_status, tag_preview
    from app.tags import read_audio_metrics

    tags = tagset_from_dict(_loads(row.working_json, {}))
    report = _loads(row.source_report_json, {})
    cover_mode = (report.get("manual_cover") or {}).get("mode", "keep")
    metrics = None
    if row.local_path:
        path = Path(row.local_path)
        if path.is_file():
            try:
                metrics = await asyncio.to_thread(read_audio_metrics, path)
            except Exception:
                log.debug("confirm audio metrics failed", exc_info=True)
    await edit_status(
        ctx,
        Job(row.chat_id, None, row.topic_name, "", row.file_name, row.status_message_id, private=True),
        f"<b>Confirm changes</b>\n\n{tag_preview(tags, metrics)}\nCover: {html_esc(cover_mode)}\n"
        f"Library topic: {html_esc(row.topic_name or 'General')}",
        _control_keyboard(row.id, confirm=True),
    )


def _normalize_image(data: bytes) -> bytes:
    with Image.open(BytesIO(data)) as image:
        image = image.convert("RGB")
        image.thumbnail((1400, 1400))
        output = BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
        return output.getvalue()


async def _public_addresses(host: str) -> list[str]:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return []
    addresses: list[str] = []
    for info in infos:
        try:
            # Strips any IPv6 scope id, which ip_address rejects.
            address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            return []
        if not address.is_global:
            return []
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, address: str, hostname: str, port: int, timeout: float) -> None:
        super().__init__(
            address,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._server_hostname = hostname

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(
            sock, server_hostname=self._server_hostname
        )


def _fetch_pinned_once(url: str, address: str) -> tuple[int, dict[str, str], bytes]:
    parsed = urlparse(url)
    assert parsed.hostname is not None
    hostname = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
            address, hostname, port, 20
        )
    else:
        connection = http.client.HTTPConnection(address, port=port, timeout=20)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    default_port = 443 if parsed.scheme == "https" else 80
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    host_header = display_host if port == default_port else f"{display_host}:{port}"
    try:
        connection.request(
            "GET",
            target,
            headers={"Host": host_header, "User-Agent": "telegram-music-bot/1.0"},
        )
        response = connection.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        data = bytearray()
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 15 * 1024 * 1024:
                raise ValueError("Image exceeds 15 MB.")
        return response.status, headers, bytes(data)
    finally:
        connection.close()


async def _fetch_image(url: str) -> bytes:
    current = url
    for _attempt in range(4):
        parsed = urlparse(current)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Use a public http/https image URL.")
        addresses = await _public_addresses(parsed.hostname)
        if not addresses:
            raise ValueError("Private or local image URLs are not allowed.")
        status, headers, data = await asyncio.to_thread(
            _fetch_pinned_once, current, addresses[0]
        )
        if status in {301, 302, 303, 307, 308}:
            location = headers.get("location")
            if not location:
                raise ValueError("Image redirect has no destination.")
            current = urljoin(current, location)
            continue
        if status < 200 or status >= 300:
            raise ValueError(f"Image server returned HTTP {status}.")
        return _normalize_image(data)
    raise ValueError("Too many image redirects.")


async def _store_manual_cover(
    ctx: Ctx, row: PendingReview, data: bytes, *, normalized: bool = False
) -> None:
    payload = data if normalized else await asyncio.to_thread(_normalize_image, data)
    path = Path(row.local_path).parent / "manual-cover.jpg"
    path.write_bytes(payload)
    report = _loads(row.source_report_json, {})
    report["manual_cover"] = {"mode": "replace", "path": path.name, "label": "upload"}
    ctx.catalog.update_pending_review(
        row.id,
        source_report_json=_dumps(report),
        status="waiting",
    )
    refreshed = ctx.catalog.get_pending_review(row.id)
    if refreshed:
        await show_field_menu(ctx, refreshed)


async def _cancel(ctx: Ctx, row: PendingReview) -> None:
    from app.queue import cancel_pending

    await cancel_pending(ctx, row)


async def _confirm_typed_review(ctx: Ctx, row: PendingReview) -> None:
    from app.queue import _apply_confirm

    report = _loads(row.source_report_json, {})
    manual = report.get("manual_cover") or {"mode": "keep"}
    tags = normalize_tagset(tagset_from_dict(_loads(row.working_json, {})), ctx.genre)
    if tags.genre:
        tags = replace(tags, genre=ctx.genre.classify(genre_tokens(tags.genre)))
    cover, mime = await asyncio.to_thread(read_cover, Path(row.local_path))
    if manual.get("mode") == "remove":
        cover, mime = None, None
    elif manual.get("mode") == "replace":
        cover_path = Path(row.local_path).parent / str(manual.get("path") or "")
        cover, mime = cover_path.read_bytes(), "image/jpeg"
    await asyncio.to_thread(write_tags, Path(row.local_path), tags, cover, mime)
    manual_path = Path(row.local_path).parent / "manual-cover.jpg"
    manual_path.unlink(missing_ok=True)
    if row.sidecar_path:
        Path(row.sidecar_path).unlink(missing_ok=True)
    report["manual_cover"] = {"mode": "keep"}
    report["manual_cover_final"] = True
    ctx.catalog.update_pending_review(
        row.id,
        source_report_json=_dumps(report),
        sidecar_path=None,
        phase="tags",
    )
    refreshed = ctx.catalog.get_pending_review(row.id)
    if refreshed:
        await _apply_confirm(ctx, refreshed, kind="library")


async def _run_private_claimed(ctx: Ctx, row: PendingReview, operation) -> None:
    try:
        await operation
    except Exception:
        log.exception("private pending action failed id=%s phase=%s", row.id, row.phase)
        ctx.catalog.update_pending_review(
            row.id, phase="edit:fields", status="waiting"
        )
        await ctx.bot.send_message(
            row.chat_id, "Action failed. Nothing was discarded; retry or cancel."
        )


async def _recall_review(ctx: Ctx, row: PendingReview, index: int) -> None:
    items = _loads(row.candidates_json, [])
    if index < 0 or index >= len(items):
        return
    item = items[index]
    pending_dir = ctx.settings.pending_root / str(uuid.uuid4())
    pending_dir.mkdir(parents=True, exist_ok=True)
    local = pending_dir / sanitize_filename(item["name"])
    ctx.catalog.update_pending_review(row.id, local_path=str(local))
    await asyncio.to_thread(ctx.drive.download_to, item["file_id"], local)
    sidecar_path: Path | None = None
    sidecar: dict = {}
    if item.get("sidecar_id"):
        sidecar_path = local.with_suffix(".json")
        await asyncio.to_thread(ctx.drive.download_to, item["sidecar_id"], sidecar_path)
        sidecar = _loads(sidecar_path.read_text(encoding="utf-8"), {})
    tags = await asyncio.to_thread(read_tagset, local)
    duration, bit_depth, sample_rate = await asyncio.to_thread(audio_info, local)
    identity = Identity(
        confidence="low",
        duration=duration,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        acoustid=sidecar.get("acoustid"),
        acoustid_score=sidecar.get("acoustid_score"),
        mb_recording_id=sidecar.get("mb_recording_id"),
        source=str(sidecar.get("source") or "drive-review"),
        confidence_reason=str(sidecar.get("confidence_reason") or "Recalled from Drive review"),
    )
    topic = str(sidecar.get("topic") or "General")
    ctx.catalog.update_pending_review(
        row.id,
        phase="edit:0",
        local_path=str(local),
        sidecar_path=str(sidecar_path) if sidecar_path else None,
        original_json=_dumps(asdict(tags)),
        recommended_json=_dumps(asdict(tags)),
        working_json=_dumps(asdict(tags)),
        identity_json=_dumps(asdict(identity)),
        source_report_json=_dumps({"recalled_review": item["relative_path"]}),
        topic_name=topic,
        file_name=item["name"],
        source_drive_file_id=item["file_id"],
        source_drive_sidecar_id=item.get("sidecar_id"),
        status="waiting",
    )
    refreshed = ctx.catalog.get_pending_review(row.id)
    if refreshed:
        await show_editor_prompt(ctx, refreshed)


def build_private_router(jobs: asyncio.Queue[Job]) -> Router:
    router = Router()

    @router.message(F.chat.type == "private", Command("start"))
    async def private_start(message: Message, ctx: Ctx) -> None:
        if not await _require_private(message, ctx):
            return
        await message.reply(
            "Send a FLAC to run tagging, or /review to open the review queue."
        )

    @router.message(F.chat.type == "private", FlacMessageFilter())
    async def private_media(message: Message, ctx: Ctx) -> None:
        from app.bot import file_info

        if not await _require_private(message, ctx):
            return
        if ctx.catalog.get_active_for_chat(message.chat.id):
            await message.reply("Finish or cancel current action first.")
            return
        file_id, file_name = file_info(message)
        pending_dir = ctx.settings.pending_root / str(uuid.uuid4())
        pending_dir.mkdir(parents=True, exist_ok=True)
        local = pending_dir / (sanitize_filename(Path(file_name).stem) + ".flac")
        status = await message.reply(f"Downloading <code>{html_esc(file_name)}</code>…", parse_mode="HTML")
        try:
            telegram_file = await ctx.bot.get_file(file_id)
            await ctx.bot.download(telegram_file, destination=local)
            await asyncio.to_thread(discard_download, telegram_file.file_path)
        except Exception:
            shutil.rmtree(pending_dir, ignore_errors=True)
            log.exception("private FLAC download failed")
            await status.edit_text("Could not download FLAC. Try again.")
            return
        try:
            pending_id = ctx.catalog.insert_pending_review(
                phase="dm_topic",
                local_path=str(local),
                sidecar_path=None,
                relative_path=None,
                kind="library",
                original_json="{}",
                recommended_json="{}",
                working_json="{}",
                candidates_json="[]",
                identity_json="{}",
                source_report_json="{}",
                chat_id=message.chat.id,
                thread_id=None,
                status_message_id=status.message_id,
                topic_name="",
                file_name=file_name,
                telegram_file_id=file_id,
                expires_at=_expires_at(),
            )
        except RuntimeError:
            shutil.rmtree(pending_dir, ignore_errors=True)
            await status.edit_text("Another private action is already active.")
            return
        row = ctx.catalog.get_pending_review(pending_id)
        if row:
            await _show_topics(ctx, row)

    @router.callback_query(F.data.regexp(r"^d\d+:"))
    async def private_callback(callback: CallbackQuery, ctx: Ctx) -> None:
        if not callback.message or callback.message.chat.type != "private":
            await callback.answer()
            return
        raw = callback.data or ""
        prefix, action = raw.split(":", 1)
        pending_id = int(prefix[1:])
        row = ctx.catalog.get_pending_review(pending_id)
        if not row or row.status != "waiting" or row.chat_id != callback.message.chat.id:
            await callback.answer("Already handled.")
            return
        if not callback.from_user:
            await callback.answer()
            return
        if not await is_forum_member(ctx, callback.from_user.id):
            await callback.answer("Access denied.", show_alert=True)
            return
        mutates = (
            action in {"cancel", "back", "confirm"}
            or action.startswith("topic:")
            or action.startswith("review:")
        )
        if mutates and not ctx.catalog.claim_pending(row.id, "processing"):
            await callback.answer("Already handled.")
            return
        await callback.answer()
        if action == "cancel":
            await _cancel(ctx, row)
        elif action == "back" and row.phase.startswith("edit:"):
            previous = _previous_editor_phase(row.phase)
            ctx.catalog.update_pending_review(row.id, phase=previous, status="waiting")
            refreshed = ctx.catalog.get_pending_review(row.id)
            if refreshed:
                if previous == "edit:cover":
                    await _show_cover_prompt(ctx, refreshed)
                else:
                    await show_editor_prompt(ctx, refreshed)
        elif action.startswith("topics:") and row.phase == "dm_topic":
            await _show_topics(ctx, row, int(action.split(":", 1)[1]))
        elif action.startswith("topic:") and row.phase == "dm_topic":
            thread_id = int(action.split(":", 1)[1])
            topic = dict(ctx.catalog.list_topics()).get(thread_id, "General")
            ctx.catalog.update_pending_review(
                row.id, status="queued", topic_name=topic, thread_id=None
            )
            await jobs.put(
                Job(
                    chat_id=row.chat_id,
                    thread_id=None,
                    topic_name=topic,
                    file_id=row.telegram_file_id or "",
                    file_name=row.file_name,
                    status_message_id=row.status_message_id,
                    local_path=row.local_path,
                    private=True,
                    source_pending_id=row.id,
                )
            )
            try:
                await ctx.bot.edit_message_text(
                    f"Queued <code>{html_esc(row.file_name)}</code>…",
                    chat_id=row.chat_id,
                    message_id=row.status_message_id,
                    parse_mode="HTML",
                )
            except Exception:
                log.debug("queued private status edit failed", exc_info=True)
        elif action.startswith("reviews:") and row.phase == "review_list":
            await _show_reviews(ctx, row, int(action.split(":", 1)[1]))
        elif action.startswith("review:") and row.phase == "review_list":
            await ctx.bot.edit_message_text(
                "Downloading review track…",
                chat_id=row.chat_id,
                message_id=row.status_message_id,
            )
            try:
                await _recall_review(ctx, row, int(action.split(":", 1)[1]))
            except Exception:
                log.exception("Drive review recall failed")
                refreshed = ctx.catalog.get_pending_review(row.id)
                if refreshed and refreshed.local_path:
                    shutil.rmtree(Path(refreshed.local_path).parent, ignore_errors=True)
                    ctx.catalog.update_pending_review(
                        row.id, local_path="", status="waiting"
                    )
                await ctx.bot.send_message(
                    row.chat_id, "Could not download that review track. Check logs."
                )
                refreshed = ctx.catalog.get_pending_review(row.id)
                if refreshed:
                    await _show_reviews(ctx, refreshed)
        elif action == "confirm" and row.phase == "edit:confirm":
            await _run_private_claimed(
                ctx, row, _confirm_typed_review(ctx, row)
            )
        elif mutates:
            ctx.catalog.update_pending_review(row.id, status="waiting")

    @router.message(F.chat.type == "private", F.photo | F.document)
    async def private_cover(message: Message, ctx: Ctx) -> None:
        row = ctx.catalog.get_waiting_for_chat(message.chat.id)
        if not row or not await _require_private(message, ctx):
            return
        if (
            current_edit_field(row) != "cover"
            and row.phase != "edit:cover"
            and (
                not message.reply_to_message
                or message.reply_to_message.message_id != row.status_message_id
            )
        ):
            return
        media = message.photo[-1] if message.photo else message.document
        if not media or (message.document and not (message.document.mime_type or "").startswith("image/")):
            return
        if not ctx.catalog.claim_pending(row.id, "processing"):
            return
        reported_size = getattr(media, "file_size", None)
        if not reported_size or reported_size > 15 * 1024 * 1024:
            ctx.catalog.update_pending_review(row.id, status="waiting")
            await message.reply("Image size is unavailable or exceeds 15 MB.")
            return
        try:
            file = await ctx.bot.get_file(media.file_id)
            buffer = BytesIO()
            await ctx.bot.download(file, destination=buffer)
            await asyncio.to_thread(discard_download, file.file_path)
            if buffer.tell() > 15 * 1024 * 1024:
                raise ValueError("Image exceeds 15 MB.")
            await _store_manual_cover(ctx, row, buffer.getvalue())
        except Exception:
            ctx.catalog.update_pending_review(row.id, status="waiting")
            await message.reply("Invalid image. Send JPEG, PNG, or WebP.")

    @router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
    async def private_text(message: Message, ctx: Ctx) -> None:
        if not await _require_private(message, ctx):
            return
        row = ctx.catalog.get_waiting_for_chat(message.chat.id)
        if row and current_edit_field(row) == "cover":
            text = (message.text or "").strip()
            if not ctx.catalog.claim_pending(row.id, "processing"):
                return
            if text.startswith(("http://", "https://")):
                try:
                    data = await _fetch_image(text)
                    await _store_manual_cover(ctx, row, data, normalized=True)
                except Exception as exc:
                    ctx.catalog.update_pending_review(row.id, status="waiting")
                    await message.reply(f"Could not load image: {html_esc(exc)}", parse_mode="HTML")
                return
            ctx.catalog.update_pending_review(row.id, status="waiting")
            await message.reply("Send a photo, image document, or public image URL.")
            return
        await handle_edit_text(message, ctx)

    return router
