from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.library_index import (
    find_entry_for_message,
    load_index_entries,
    remember_library_tags,
)
from app.models import Ctx, TagSet, TrackRecord, tagset_from_dict

log = logging.getLogger(__name__)

PROBE_TEXT = "\u2060"
_DRIVE_FILE_RE = re.compile(
    r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)",
    re.I,
)
_DRIVE_OPEN_RE = re.compile(
    r"(?:drive\.google\.com/open|drive\.google\.com/uc|docs\.google\.com/uc)\?[^:\s]*?[?&]?id=([A-Za-z0-9_-]+)",
    re.I,
)
_DRIVE_ID_QUERY_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})", re.I)
_SAVED_KIND_RE = re.compile(r"Saved\s*\((library|review)\b", re.I)
_KIND_LINE_RE = re.compile(r"^(library|review)\b", re.I | re.M)
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/")
_CODE_HTML_RE = re.compile(r"<code>([^<]+)</code>", re.I)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_ARTIST_RE = re.compile(r"^Artist:\s*(.*)$", re.M)
_ALBUM_RE = re.compile(r"^Album:\s*(.*)$", re.M)
_FLAC_LINE_RE = re.compile(r"(?m)^(\S(?:.*\S)?\.flac)$", re.I)
_PROBE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


@dataclass
class CardHint:
    kind: str | None = None
    drive_file_id: str | None = None
    drive_url: str | None = None
    relative_path: str | None = None
    title: str = ""
    artist: str = ""
    album: str = ""
    telegram_file_id: str | None = None
    source_message_id: int | None = None
    card_message_id: int | None = None
    chat_id: int | None = None
    thread_id: int | None = None
    file_name: str | None = None
    topic_name: str = ""
    local_path: str | None = None
    tags_json: str | None = None
    is_source_audio: bool = False


def _probe_lock(chat_id: int, message_id: int) -> asyncio.Lock:
    key = (chat_id, message_id)
    lock = _PROBE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROBE_LOCKS[key] = lock
    return lock


def parse_drive_file_id(url: str) -> str | None:
    text = html.unescape(str(url or "").strip())
    if not text:
        return None
    match = _DRIVE_FILE_RE.search(text)
    if match:
        return match.group(1)
    match = _DRIVE_OPEN_RE.search(text)
    if match:
        return match.group(1)
    match = _DRIVE_ID_QUERY_RE.search(text)
    if match and "drive.google.com" in text.casefold():
        return match.group(1)
    return None


def looks_like_music_card(hint: CardHint) -> bool:
    if hint.drive_file_id or hint.relative_path or hint.telegram_file_id:
        return True
    if hint.kind and hint.title and hint.artist:
        return True
    return False


def _int_field(raw: object) -> int | None:
    text = str(raw or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def _entity_type(entity: object) -> str:
    raw = getattr(entity, "type", "")
    name = getattr(raw, "name", None) or getattr(raw, "value", None) or str(raw or "")
    return str(name).rsplit(".", 1)[-1].casefold()


def _utf16_slice(text: str, offset: int, length: int) -> str:
    try:
        encoded = text.encode("utf-16-le")
        start = offset * 2
        end = (offset + length) * 2
        return encoded[start:end].decode("utf-16-le")
    except Exception:
        return text[offset : offset + length]


def _plain_and_entities(message: object) -> tuple[str, list]:
    text = str(getattr(message, "text", None) or getattr(message, "caption", None) or "")
    entities = list(getattr(message, "entities", None) or getattr(message, "caption_entities", None) or [])
    return text, entities


def _flac_from_message(message: object | None) -> tuple[str | None, str | None]:
    if message is None:
        return None, None
    for attr in ("document", "audio"):
        media = getattr(message, attr, None)
        if not media:
            continue
        name = str(getattr(media, "file_name", None) or "")
        mime = str(getattr(media, "mime_type", None) or "").casefold()
        file_id = str(getattr(media, "file_id", None) or "") or None
        if file_id and (name.casefold().endswith(".flac") or "flac" in mime):
            return file_id, name or "track.flac"
    return None, None


def _normalize_relative(path: str) -> str:
    return html.unescape(path.replace("\\", "/")).strip().lstrip("/")


def infer_kind(relative: str | None, header_kind: str | None) -> str | None:
    if header_kind in {"library", "review"}:
        return header_kind
    if not relative:
        return None
    posix = _normalize_relative(relative)
    if _DATE_DIR_RE.match(posix):
        return "review"
    if posix.count("/") >= 2:
        return "library"
    return None


def _topic_from_relative(relative: str | None, kind: str | None) -> str:
    if not relative or kind == "review":
        return ""
    parts = Path(_normalize_relative(relative)).parts
    return parts[0] if parts else ""


def parse_card_message(message: object) -> CardHint:
    text, entities = _plain_and_entities(message)
    drive_url = None
    drive_file_id = None
    relative = None
    title = ""
    code_chunks: list[str] = []
    for entity in entities:
        kind = _entity_type(entity)
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        chunk = _utf16_slice(text, offset, length)
        if kind == "text_link":
            url = str(getattr(entity, "url", None) or "")
            parsed = parse_drive_file_id(url)
            if parsed:
                drive_file_id = drive_file_id or parsed
                drive_url = drive_url or url
        elif kind == "url":
            parsed = parse_drive_file_id(chunk)
            if parsed:
                drive_file_id = drive_file_id or parsed
                drive_url = drive_url or chunk
        elif kind in {"code", "pre"}:
            code_chunks.append(chunk)
        elif kind == "bold" and not title:
            title = chunk.strip()
    if drive_file_id is None:
        for match in _HREF_RE.finditer(text):
            parsed = parse_drive_file_id(html.unescape(match.group(1)))
            if parsed:
                drive_file_id = parsed
                drive_url = html.unescape(match.group(1))
                break
    if drive_file_id is None:
        drive_file_id = parse_drive_file_id(text)
    for chunk in reversed(code_chunks):
        posix = _normalize_relative(chunk)
        if posix.casefold().endswith(".flac"):
            relative = posix
            break
    if relative is None:
        for match in reversed(list(_CODE_HTML_RE.finditer(text))):
            posix = _normalize_relative(match.group(1))
            if posix.casefold().endswith(".flac"):
                relative = posix
                break
    if relative is None:
        for match in reversed(list(_FLAC_LINE_RE.finditer(text))):
            posix = _normalize_relative(match.group(1))
            if "/" in posix:
                relative = posix
                break
    header_kind = None
    saved = _SAVED_KIND_RE.search(text)
    if saved:
        header_kind = saved.group(1).casefold()
    else:
        line = _KIND_LINE_RE.search(text)
        if line:
            header_kind = line.group(1).casefold()
    kind = infer_kind(relative, header_kind)
    artist = (_ARTIST_RE.search(text).group(1).strip() if _ARTIST_RE.search(text) else "")
    album = (_ALBUM_RE.search(text).group(1).strip() if _ALBUM_RE.search(text) else "")
    if not title:
        for raw in text.splitlines():
            line = re.sub(r"<[^>]+>", "", raw).strip()
            if not line or line.casefold() in {"library", "review"}:
                continue
            if line.casefold().startswith("saved (") or line.casefold().startswith("artist:"):
                continue
            if line.casefold().startswith("drive:") or line.casefold().endswith(".flac"):
                continue
            title = html.unescape(line)
            break
    self_id, self_name = _flac_from_message(message)
    reply = getattr(message, "reply_to_message", None)
    reply_id, reply_name = _flac_from_message(reply)
    telegram_file_id = self_id or reply_id
    file_name = self_name or reply_name
    if relative:
        file_name = file_name or Path(relative).name
    source_message_id = None
    if self_id:
        source_message_id = _int_field(getattr(message, "message_id", None))
    elif reply_id:
        source_message_id = _int_field(getattr(reply, "message_id", None))
    return CardHint(
        kind=kind,
        drive_file_id=drive_file_id,
        drive_url=drive_url,
        relative_path=relative,
        title=html.unescape(title),
        artist=html.unescape(artist),
        album=html.unescape(album),
        telegram_file_id=telegram_file_id,
        source_message_id=source_message_id,
        card_message_id=None if self_id else _int_field(getattr(message, "message_id", None)),
        thread_id=_int_field(getattr(message, "message_thread_id", None)),
        file_name=file_name,
        topic_name=_topic_from_relative(relative, kind),
        is_source_audio=bool(self_id),
    )


def hint_from_index_entry(
    entry: dict[str, str],
    *,
    chat_id: int,
    message_id: int,
) -> CardHint:
    relative = str(entry.get("relative_path") or "") or None
    drive_id = str(entry.get("drive_file_id") or "") or None
    card_id = _int_field(entry.get("card_message_id"))
    source_id = _int_field(entry.get("message_id"))
    is_source = source_id == message_id
    tags = TagSet(
        title=str(entry.get("title") or ""),
        artist=str(entry.get("artist") or ""),
        album=str(entry.get("album") or ""),
        albumartist=str(entry.get("albumartist") or ""),
        genre=str(entry.get("genre") or ""),
    )
    return CardHint(
        kind="library",
        drive_file_id=drive_id,
        drive_url=f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else None,
        relative_path=relative,
        title=tags.title,
        artist=tags.artist,
        album=tags.album,
        telegram_file_id=str(entry.get("telegram_file_id") or "") or None,
        source_message_id=source_id,
        card_message_id=card_id if is_source else (card_id or message_id),
        chat_id=_int_field(entry.get("chat_id")) or chat_id,
        thread_id=_int_field(entry.get("thread_id")),
        file_name=Path(relative).name if relative else None,
        topic_name=str(entry.get("topic_name") or "") or _topic_from_relative(relative, "library"),
        tags_json=json.dumps(asdict(tags), ensure_ascii=False),
        is_source_audio=is_source,
    )


def find_index_entry_for_hint(entries: list[dict[str, str]], hint: CardHint) -> dict[str, str] | None:
    if hint.drive_file_id:
        for item in entries:
            if str(item.get("drive_file_id") or "") == hint.drive_file_id:
                return item
    if hint.relative_path:
        key = _normalize_relative(hint.relative_path).casefold()
        for item in entries:
            if _normalize_relative(str(item.get("relative_path") or "")).casefold() == key:
                return item
    if hint.telegram_file_id:
        for item in entries:
            if str(item.get("telegram_file_id") or "") == hint.telegram_file_id:
                return item
    if hint.title and hint.artist:
        title = hint.title.casefold()
        artist = hint.artist.casefold()
        hits = [
            item
            for item in entries
            if str(item.get("title") or "").casefold() == title
            and str(item.get("artist") or "").casefold() == artist
        ]
        if len(hits) == 1:
            return hits[0]
    return None


def _merge_entry(hint: CardHint, entry: dict[str, str]) -> CardHint:
    extra = hint_from_index_entry(
        entry,
        chat_id=hint.chat_id or 0,
        message_id=hint.card_message_id or hint.source_message_id or 0,
    )
    return replace(
        hint,
        kind=hint.kind or extra.kind,
        drive_file_id=hint.drive_file_id or extra.drive_file_id,
        drive_url=hint.drive_url or extra.drive_url,
        relative_path=hint.relative_path or extra.relative_path,
        title=hint.title or extra.title,
        artist=hint.artist or extra.artist,
        album=hint.album or extra.album,
        telegram_file_id=hint.telegram_file_id or extra.telegram_file_id,
        source_message_id=hint.source_message_id or extra.source_message_id,
        thread_id=hint.thread_id if hint.thread_id is not None else extra.thread_id,
        file_name=hint.file_name or extra.file_name,
        topic_name=hint.topic_name or extra.topic_name,
        tags_json=hint.tags_json or extra.tags_json,
    )


def _find_catalog(ctx: Ctx, hint: CardHint) -> TrackRecord | None:
    track = None
    if hint.drive_file_id:
        track = ctx.catalog.find_by_drive_file_id(hint.drive_file_id)
    if track is None and hint.relative_path:
        track = ctx.catalog.find_uploaded_by_relative(hint.relative_path)
    if track is None and hint.telegram_file_id:
        track = ctx.catalog.find_by_telegram_file_id(hint.telegram_file_id)
    if track is None or track.status == "deleted":
        return None
    return track


def _find_local(ctx: Ctx, hint: CardHint) -> Path | None:
    if not hint.relative_path:
        return None
    relative = Path(_normalize_relative(hint.relative_path))
    roots: list[Path] = []
    if hint.kind == "library":
        roots.append(ctx.settings.library_root)
    elif hint.kind == "review":
        roots.append(ctx.settings.review_root)
    else:
        roots.extend([ctx.settings.library_root, ctx.settings.review_root])
    for root in roots:
        path = root / relative
        if path.is_file():
            return path
    return None


async def _drive_id_from_path(ctx: Ctx, hint: CardHint) -> str | None:
    relative = hint.relative_path
    if not relative:
        return None
    kinds = [hint.kind] if hint.kind in {"library", "review"} else ["library", "review"]
    posix = Path(_normalize_relative(relative))
    for kind in kinds:
        root = (
            ctx.settings.gdrive_folder_id
            if kind == "library"
            else ctx.settings.gdrive_review_folder_id
        )
        try:
            parent = await asyncio.to_thread(ctx.drive.find_path, root, list(posix.parts[:-1]))
        except Exception:
            log.debug("drive path lookup failed kind=%s path=%s", kind, relative, exc_info=True)
            continue
        if not parent:
            continue
        try:
            hits = await asyncio.to_thread(ctx.drive.find_name_conflicts, parent, posix.name)
        except Exception:
            log.debug("drive name lookup failed kind=%s path=%s", kind, relative, exc_info=True)
            continue
        if hits:
            if hint.kind is None:
                hint.kind = kind
            return hits[0].id
    return None


async def probe_card_message(bot, chat_id: int, message_id: int):
    sent = None
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=PROBE_TEXT,
            reply_to_message_id=message_id,
            disable_notification=True,
        )
        return getattr(sent, "reply_to_message", None)
    except Exception:
        log.debug("reaction card probe failed chat=%s message=%s", chat_id, message_id, exc_info=True)
        return None
    finally:
        if sent is not None and getattr(sent, "message_id", None):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
            except Exception:
                log.debug("reaction probe delete failed chat=%s", chat_id, exc_info=True)


def _tags_json(hint: CardHint) -> str | None:
    if hint.tags_json:
        return hint.tags_json
    if not (hint.title or hint.artist or hint.album):
        return None
    return json.dumps(
        asdict(
            TagSet(
                title=hint.title,
                artist=hint.artist,
                album=hint.album,
                genre=hint.topic_name,
            )
        ),
        ensure_ascii=False,
    )


def _apply_hint(ctx: Ctx, track: TrackRecord, hint: CardHint) -> TrackRecord:
    fields: dict[str, object] = {}
    if hint.drive_file_id and not track.drive_file_id:
        fields["drive_file_id"] = hint.drive_file_id
    if hint.drive_url and not track.drive_url:
        fields["drive_url"] = hint.drive_url
    if hint.relative_path and not track.relative_path:
        fields["relative_path"] = hint.relative_path
    if hint.telegram_file_id and not track.telegram_file_id:
        fields["telegram_file_id"] = hint.telegram_file_id
    if hint.local_path and not track.local_path:
        fields["local_path"] = hint.local_path
    if hint.kind and hint.kind != track.kind:
        fields["kind"] = hint.kind
    if hint.topic_name and not track.topic_name:
        fields["topic_name"] = hint.topic_name
    if hint.file_name and not track.file_name:
        fields["file_name"] = hint.file_name
    if hint.title and not track.title:
        fields["title"] = hint.title
    if hint.artist and not track.artist:
        fields["artist"] = hint.artist
    if hint.album and not track.album:
        fields["album"] = hint.album
    tags_json = _tags_json(hint)
    if tags_json and not track.tags_json:
        fields["tags_json"] = tags_json
    if hint.thread_id is not None and track.thread_id is None:
        fields["thread_id"] = hint.thread_id
    if hint.chat_id and not track.source_chat_id:
        fields["source_chat_id"] = hint.chat_id
    if hint.source_message_id and not track.source_message_id:
        fields["source_message_id"] = hint.source_message_id
    if fields:
        ctx.catalog.update_track(track.id, **fields)
        refreshed = ctx.catalog.get_track(track.id)
        if refreshed:
            return refreshed
    return track


def _insert_track(ctx: Ctx, hint: CardHint) -> TrackRecord | None:
    if not (hint.drive_file_id or hint.relative_path or hint.local_path):
        return None
    kind = hint.kind or "library"
    relative = hint.relative_path or ""
    drive_id = hint.drive_file_id
    drive_url = hint.drive_url
    if drive_id and not drive_url:
        drive_url = f"https://drive.google.com/file/d/{drive_id}/view"
    tags_json = _tags_json(hint)
    tags = tagset_from_dict(json.loads(tags_json) if tags_json else {})
    track_id = ctx.catalog.insert_pending(
        kind=kind,
        mb_recording_id=None,
        acoustid=None,
        local_path=hint.local_path or "",
        sidecar_path=None,
        relative_path=relative,
        bit_depth=None,
        sample_rate=None,
        title=hint.title or tags.title or None,
        artist=hint.artist or tags.artist or None,
        album=hint.album or tags.album or None,
        status="uploaded",
        telegram_file_id=hint.telegram_file_id,
        tags_json=tags_json,
        topic_name=hint.topic_name or None,
        file_name=hint.file_name or (Path(relative).name if relative else None),
        drive_file_id=drive_id,
        drive_url=drive_url,
    )
    if drive_id:
        ctx.catalog.mark_uploaded(track_id, drive_id, drive_url)
    ctx.catalog.update_track(
        track_id,
        thread_id=hint.thread_id,
        source_chat_id=hint.chat_id,
        source_message_id=hint.source_message_id,
        file_name=hint.file_name or (Path(relative).name if relative else None),
        topic_name=hint.topic_name or None,
    )
    return ctx.catalog.get_track(track_id)


def _bind_messages(ctx: Ctx, track: TrackRecord, hint: CardHint, *, chat_id: int, message_id: int) -> tuple[int | None, int | None]:
    ctx.catalog.bind_track_message(track.id, chat_id, message_id)
    source_id = hint.source_message_id or track.source_message_id
    card_id = message_id if not hint.is_source_audio else hint.card_message_id
    if source_id:
        ctx.catalog.bind_track_message(track.id, chat_id, source_id)
    if card_id:
        ctx.catalog.bind_track_message(track.id, chat_id, card_id)
    return source_id, card_id


def _remember_resolved(ctx: Ctx, track: TrackRecord, hint: CardHint, *, chat_id: int, source_id: int | None, card_id: int | None) -> None:
    tags = (
        tagset_from_dict(json.loads(track.tags_json))
        if track.tags_json
        else TagSet(title=track.title or "", artist=track.artist or "", album=track.album or "")
    )
    remember_library_tags(
        ctx,
        kind=track.kind,
        relative_path=track.relative_path,
        drive_file_id=track.drive_file_id,
        topic_name=track.topic_name or hint.topic_name or "",
        tags=tags,
        telegram_file_id=track.telegram_file_id,
        chat_id=track.source_chat_id or chat_id,
        message_id=source_id,
        card_message_id=card_id,
        thread_id=track.thread_id if track.thread_id is not None else hint.thread_id,
    )


async def _bind_and_remember(ctx: Ctx, track: TrackRecord, hint: CardHint, *, chat_id: int, message_id: int) -> None:
    source_id, card_id = _bind_messages(ctx, track, hint, chat_id=chat_id, message_id=message_id)
    await asyncio.to_thread(
        _remember_resolved, ctx, track, hint, chat_id=chat_id, source_id=source_id, card_id=card_id
    )


async def resolve_and_materialize(ctx: Ctx, hint: CardHint) -> TrackRecord | None:
    track = _find_catalog(ctx, hint)
    if track:
        return _apply_hint(ctx, track, hint)
    entries = await asyncio.to_thread(load_index_entries, ctx) or []
    entry = find_index_entry_for_hint(entries, hint)
    if entry:
        hint = _merge_entry(hint, entry)
        track = _find_catalog(ctx, hint)
        if track:
            return _apply_hint(ctx, track, hint)
    local = _find_local(ctx, hint)
    if local:
        hint.local_path = str(local)
        if hint.kind is None:
            try:
                local.resolve().relative_to(Path(ctx.settings.library_root).resolve())
                hint.kind = "library"
            except ValueError:
                hint.kind = "review"
    if not hint.drive_file_id and hint.relative_path:
        hint.drive_file_id = await _drive_id_from_path(ctx, hint)
        if hint.drive_file_id and not hint.drive_url:
            hint.drive_url = f"https://drive.google.com/file/d/{hint.drive_file_id}/view"
    elif hint.drive_file_id and not hint.relative_path and not hint.local_path:
        try:
            meta = await asyncio.to_thread(ctx.drive.get_child_meta, hint.drive_file_id)
        except Exception:
            log.debug("drive meta lookup failed id=%s", hint.drive_file_id, exc_info=True)
            meta = None
        if meta is None and not hint.local_path and not hint.relative_path:
            log.info("reaction card drive id missing id=%s", hint.drive_file_id)
            hint.drive_file_id = None
    return _insert_track(ctx, hint)


async def resolve_track_for_reaction(ctx: Ctx, event) -> TrackRecord | None:
    chat_id = int(event.chat.id)
    message_id = int(event.message_id)
    async with _probe_lock(chat_id, message_id):
        existing = ctx.catalog.get_track_by_message(chat_id, message_id)
        if existing and existing.status != "deleted":
            return existing
        entries = await asyncio.to_thread(load_index_entries, ctx) or []
        entry = find_entry_for_message(entries, chat_id, message_id)
        if entry:
            hint = hint_from_index_entry(entry, chat_id=chat_id, message_id=message_id)
            hint.chat_id = chat_id
            if hint.is_source_audio:
                hint.source_message_id = message_id
            else:
                hint.card_message_id = message_id
            track = await resolve_and_materialize(ctx, hint)
            if track:
                await _bind_and_remember(ctx, track, hint, chat_id=chat_id, message_id=message_id)
                return ctx.catalog.get_track(track.id) or track
        message = await probe_card_message(ctx.bot, chat_id, message_id)
        if message is None:
            log.info("reaction ignored; card probe failed chat=%s message=%s", chat_id, message_id)
            return None
        hint = parse_card_message(message)
        hint.chat_id = chat_id
        if hint.is_source_audio:
            hint.source_message_id = hint.source_message_id or message_id
        else:
            hint.card_message_id = message_id
        if hint.thread_id is None:
            hint.thread_id = _int_field(getattr(message, "message_thread_id", None))
        if not looks_like_music_card(hint):
            log.debug("reaction probe not a music card chat=%s message=%s", chat_id, message_id)
            return None
        track = await resolve_and_materialize(ctx, hint)
        if track is None:
            log.info(
                "reaction card resolve missed chat=%s message=%s path=%s drive=%s",
                chat_id,
                message_id,
                hint.relative_path,
                hint.drive_file_id,
            )
            return None
        await _bind_and_remember(ctx, track, hint, chat_id=chat_id, message_id=message_id)
        return ctx.catalog.get_track(track.id) or track
