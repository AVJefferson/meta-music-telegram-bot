from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from app.drive import DriveReviewItem
from app.library import unlink_quiet
from app.models import Ctx, TagSet, TrackRecord, tagset_from_dict
from app.suggest import parse_library_relative, track_from_library_item
from app.tags import overlay_tagset, read_tagset

log = logging.getLogger(__name__)

INDEX_FOLDER = "library"
INDEX_FILE = "tracks.json"
INDEX_RELATIVE = Path(INDEX_FOLDER) / INDEX_FILE
_INDEX_LOCK = threading.Lock()


def index_entry(
    *,
    relative_path: str,
    drive_file_id: str | None,
    topic_name: str,
    tags: TagSet,
    telegram_file_id: str | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    card_message_id: int | None = None,
    thread_id: int | None = None,
) -> dict[str, str]:
    return {
        "relative_path": relative_path,
        "drive_file_id": drive_file_id or "",
        "topic_name": topic_name,
        "title": tags.title,
        "artist": tags.artist,
        "album": tags.album,
        "albumartist": tags.albumartist,
        "genre": tags.genre,
        "telegram_file_id": telegram_file_id or "",
        "chat_id": str(chat_id or "") if chat_id else "",
        "message_id": str(message_id or "") if message_id else "",
        "card_message_id": str(card_message_id or "") if card_message_id else "",
        "thread_id": str(thread_id) if thread_id is not None else "",
    }


def tags_from_path(relative_path: str) -> TagSet:
    seed = parse_library_relative(relative_path)
    if seed is None:
        return TagSet()
    parts = Path((relative_path or "").replace("\\", "/")).parts
    albumartist = parts[1] if len(parts) >= 3 else seed.artist
    return TagSet(
        title=seed.title,
        artist=seed.artist,
        album=seed.album,
        albumartist=albumartist or seed.artist,
        genre=seed.topic_name,
    )


def tags_from_sidecar_payload(payload: object) -> TagSet:
    if not isinstance(payload, dict):
        return TagSet()
    proposed = payload.get("proposed") or payload.get("chosen") or payload.get("tags") or {}
    if not isinstance(proposed, dict):
        return TagSet()
    return tagset_from_dict(proposed)


def tags_from_songlog(text: str) -> TagSet:
    chosen: dict[str, str] = {}
    file_tags: dict[str, str] = {}
    current: dict[str, str] | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("== ") and line.endswith(" =="):
            name = line[3:-3].strip().casefold()
            if name == "chosen":
                current = chosen
            elif name == "file tags":
                current = file_tags
            else:
                current = None
            continue
        if current is None or ":" not in line:
            continue
        key, _, value = line.partition(":")
        current[key.strip().casefold()] = value.strip()
    src = chosen if chosen.get("title") or chosen.get("artist") else file_tags
    return TagSet(
        title=src.get("title") or "",
        artist=src.get("artist") or "",
        album=src.get("album") or "",
        albumartist=src.get("albumartist") or "",
        composer=src.get("composer") or "",
        genre=src.get("genre") or "",
        date=src.get("year") or "",
        tracknumber=src.get("track") or "",
        discnumber=src.get("disc") or "",
    )


def _index_tags_ready(tags: TagSet) -> bool:
    return bool(tags.title and tags.artist)


def parse_index_payload(raw: str | None) -> list[dict[str, str]]:
    data = _loads(raw, {})
    if isinstance(data, dict):
        tracks = data.get("tracks")
        if isinstance(tracks, list):
            return [item for item in tracks if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def dump_index_payload(entries: list[dict[str, str]]) -> str:
    return json.dumps({"version": 1, "tracks": entries}, ensure_ascii=False, separators=(",", ":"))


def payload_sha(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _keep_meta(old: dict[str, str], new: dict[str, str], key: str) -> str:
    return str(new.get(key) or old.get(key) or "")


def upsert_entries(entries: list[dict[str, str]], entry: dict[str, str]) -> list[dict[str, str]]:
    key = (entry.get("relative_path") or "").casefold()
    out: list[dict[str, str]] = []
    replaced = False
    for item in entries:
        if (item.get("relative_path") or "").casefold() != key:
            out.append(item)
            continue
        merged = dict(item)
        merged.update(entry)
        for field in (
            "telegram_file_id",
            "chat_id",
            "message_id",
            "card_message_id",
            "thread_id",
            "drive_file_id",
        ):
            merged[field] = _keep_meta(item, entry, field)
        out.append(merged)
        replaced = True
    if not replaced:
        out.append(entry)
    return out


def remove_entry(entries: list[dict[str, str]], relative_path: str) -> list[dict[str, str]]:
    key = (relative_path or "").casefold()
    return [item for item in entries if (item.get("relative_path") or "").casefold() != key]


def entries_to_tracks(entries: list[dict[str, str]], *, topic: str | None = None) -> list[TrackRecord]:
    want = (topic or "").casefold()
    tracks: list[TrackRecord] = []
    for item in entries:
        relative = str(item.get("relative_path") or "")
        topic_name = str(item.get("topic_name") or "")
        if want and topic_name.casefold() != want:
            continue
        tags = TagSet(
            title=str(item.get("title") or ""),
            artist=str(item.get("artist") or ""),
            album=str(item.get("album") or ""),
            albumartist=str(item.get("albumartist") or ""),
            genre=str(item.get("genre") or topic_name),
        )
        track = track_from_library_item(
            relative_path=relative,
            file_name=Path(relative).name,
            drive_file_id=str(item.get("drive_file_id") or "") or None,
        )
        if track is None:
            continue
        track.tags_json = json.dumps(asdict(tags), ensure_ascii=False)
        track.title = tags.title or track.title
        track.artist = tags.artist or track.artist
        track.album = tags.album or track.album
        track.topic_name = topic_name or track.topic_name
        track.telegram_file_id = str(item.get("telegram_file_id") or "") or None
        chat_raw = str(item.get("chat_id") or "")
        msg_raw = str(item.get("message_id") or "")
        thread_raw = str(item.get("thread_id") or "")
        track.source_chat_id = int(chat_raw) if chat_raw.lstrip("-").isdigit() else None
        track.source_message_id = int(msg_raw) if msg_raw.isdigit() else None
        if thread_raw.lstrip("-").isdigit():
            track.thread_id = int(thread_raw)
        tracks.append(track)
    return tracks


def _loads(text: str | None, default):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return default


def _read_cached(ctx: Ctx) -> list[dict[str, str]] | None:
    raw, _drive_id, _sha = ctx.catalog.get_library_tag_index_meta()
    if not raw:
        return None
    return parse_index_payload(raw)


def _write_cached(
    ctx: Ctx,
    entries: list[dict[str, str]],
    *,
    drive_file_id: str | None = None,
    payload_sha_value: str | None = None,
) -> str:
    payload = dump_index_payload(entries)
    ctx.catalog.set_library_tag_index(
        payload,
        drive_file_id=drive_file_id,
        payload_sha=payload_sha_value or payload_sha(payload),
    )
    return payload


def load_index_from_drive(ctx: Ctx) -> tuple[list[dict[str, str]], str] | None:
    log.info("library tag index: probing Drive %s/%s", INDEX_FOLDER, INDEX_FILE)
    root = ctx.settings.gdrive_folder_id
    parent = ctx.drive.find_path(root, [INDEX_FOLDER])
    if not parent:
        log.info("library tag index: Drive folder %s missing", INDEX_FOLDER)
        return None
    hits = ctx.drive.find_by_name(parent, INDEX_FILE)
    if not hits:
        log.info("library tag index: %s missing", INDEX_FILE)
        return None
    log.info("library tag index: downloading %s id=%s", INDEX_FILE, hits[0].id)
    raw = ctx.drive.download_bytes(hits[0].id).decode("utf-8")
    entries = parse_index_payload(raw)
    log.info("library tag index: Drive rows=%s", len(entries))
    return entries, hits[0].id


def save_index_to_drive(ctx: Ctx, payload: bytes, *, replace_id: str | None) -> str | None:
    if replace_id:
        try:
            file_id, _url = ctx.drive.upload_bytes(
                payload, "", INDEX_FILE, "application/json", replace_id=replace_id
            )
            return file_id or replace_id
        except Exception:
            log.info("library tag index in-place update missed file, creating tracks.json")
    parent = ctx.drive.ensure_parent(ctx.settings.gdrive_folder_id, INDEX_RELATIVE)
    hits = ctx.drive.find_by_name(parent, INDEX_FILE)
    found_id = hits[0].id if hits else None
    file_id, _url = ctx.drive.upload_bytes(
        payload, parent, INDEX_FILE, "application/json", replace_id=found_id
    )
    return file_id or found_id


def persist_index(ctx: Ctx, entries: list[dict[str, str]]) -> None:
    payload = dump_index_payload(entries)
    sha = payload_sha(payload)
    _cached, drive_id, old_sha = ctx.catalog.get_library_tag_index_meta()
    ctx.catalog.set_library_tag_index(payload, payload_sha=sha)
    if old_sha == sha and drive_id:
        return
    try:
        new_id = save_index_to_drive(ctx, payload.encode("utf-8"), replace_id=drive_id)
    except Exception:
        log.warning("library tag index Drive write failed", exc_info=True)
        return
    if new_id:
        ctx.catalog.set_library_tag_index(payload, drive_file_id=new_id, payload_sha=sha)


def load_index_entries(ctx: Ctx) -> list[dict[str, str]] | None:
    cached = _read_cached(ctx)
    if cached is not None:
        return cached
    try:
        remote = load_index_from_drive(ctx)
    except Exception:
        log.warning("library tag index Drive read failed", exc_info=True)
        return None
    if remote is None:
        return None
    entries, drive_id = remote
    _write_cached(ctx, entries, drive_file_id=drive_id)
    return entries


def upsert_library_index(
    ctx: Ctx,
    *,
    relative_path: str,
    drive_file_id: str | None,
    topic_name: str,
    tags: TagSet,
    telegram_file_id: str | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    card_message_id: int | None = None,
    thread_id: int | None = None,
) -> None:
    with _INDEX_LOCK:
        entries = load_index_entries(ctx)
        if entries is None:
            try:
                entries = rebuild_index(ctx)
            except Exception:
                log.warning("library tag index rebuild before upsert failed", exc_info=True)
                entries = []
        entries = upsert_entries(
            entries,
            index_entry(
                relative_path=relative_path,
                drive_file_id=drive_file_id,
                topic_name=topic_name,
                tags=tags,
                telegram_file_id=telegram_file_id,
                chat_id=chat_id,
                message_id=message_id,
                card_message_id=card_message_id,
                thread_id=thread_id,
            ),
        )
        persist_index(ctx, entries)


def remove_library_index(ctx: Ctx, relative_path: str | None) -> None:
    if not relative_path:
        return
    with _INDEX_LOCK:
        entries = load_index_entries(ctx)
        if entries is None:
            return
        persist_index(ctx, remove_entry(entries, relative_path))


def find_entry_for_message(
    entries: list[dict[str, str]], chat_id: int, message_id: int
) -> dict[str, str] | None:
    want_chat = str(chat_id)
    want_msg = str(message_id)
    found_source = None
    for item in entries:
        item_chat = str(item.get("chat_id") or "")
        if item_chat and item_chat != want_chat:
            continue
        if str(item.get("card_message_id") or "") == want_msg:
            return item
        if str(item.get("message_id") or "") == want_msg:
            found_source = item
    return found_source


def extract_item_tags(ctx: Ctx, item: DriveReviewItem, tmp_root: Path) -> TagSet:
    tags = tags_from_path(item.relative_path)
    if item.sidecar_id:
        try:
            payload = _loads(ctx.drive.download_bytes(item.sidecar_id).decode("utf-8"), {})
            tags = overlay_tagset(tags, tags_from_sidecar_payload(payload))
        except Exception:
            log.debug("index sidecar read failed %s", item.relative_path, exc_info=True)
    if item.log_id:
        try:
            tags = overlay_tagset(
                tags,
                tags_from_songlog(ctx.drive.download_bytes(item.log_id).decode("utf-8")),
            )
        except Exception:
            log.debug("index song log read failed %s", item.relative_path, exc_info=True)
    if _index_tags_ready(tags):
        return tags
    tmp = tmp_root / "index" / f"{uuid.uuid4().hex}.flac"
    try:
        ctx.drive.download_to(item.file_id, tmp)
        tags = overlay_tagset(tags, read_tagset(tmp))
    except Exception:
        log.debug("index flac tag read failed %s", item.relative_path, exc_info=True)
    finally:
        unlink_quiet(tmp)
    return tags


def rebuild_index(ctx: Ctx, *, on_progress: Callable[[int, int], None] | None = None) -> list[dict[str, str]]:
    log.info("library tag index: walking Drive FLACs (once)")
    items = ctx.drive.list_library_items(ctx.settings.gdrive_folder_id)
    previous = load_index_entries(ctx) or []
    by_path = {(item.get("relative_path") or "").casefold(): item for item in previous}
    entries: list[dict[str, str]] = []
    total = len(items)
    log.info("library tag index: walk found %s FLACs", total)
    tmp_root = ctx.settings.tmp_root

    def _progress(done: int, all_count: int) -> None:
        log.info("library tag index: tagged %s/%s", done, all_count)
        if on_progress:
            on_progress(done, all_count)

    for index, item in enumerate(items, start=1):
        tags = extract_item_tags(ctx, item, tmp_root)
        topic = Path(item.relative_path).parts[0] if item.relative_path else ""
        old = by_path.get(item.relative_path.casefold()) or {}
        entries.append(
            index_entry(
                relative_path=item.relative_path,
                drive_file_id=item.file_id,
                topic_name=topic,
                tags=tags,
                telegram_file_id=str(old.get("telegram_file_id") or "") or None,
                chat_id=int(old["chat_id"]) if str(old.get("chat_id") or "").lstrip("-").isdigit() else None,
                message_id=int(old["message_id"]) if str(old.get("message_id") or "").isdigit() else None,
                card_message_id=int(old["card_message_id"])
                if str(old.get("card_message_id") or "").isdigit()
                else None,
                thread_id=int(old["thread_id"]) if str(old.get("thread_id") or "").lstrip("-").isdigit() else None,
            )
        )
        if index == 1 or index == total or index % 25 == 0:
            _progress(index, total)
    persist_index(ctx, entries)
    log.info("library tag index rebuilt tracks=%s", len(entries))
    return entries


def library_tracks_from_index(ctx: Ctx, *, topic: str | None = None, rebuild: bool = False) -> tuple[list[TrackRecord], bool]:
    """Return in-memory tracks from the tag index. rebuilt=True if Drive FLACs were walked."""
    with _INDEX_LOCK:
        entries = None if rebuild else load_index_entries(ctx)
        rebuilt = False
        if entries is None:
            entries = rebuild_index(ctx)
            rebuilt = True
        return entries_to_tracks(entries, topic=topic), rebuilt


def ensure_library_index(ctx: Ctx) -> str:
    """Load sqlite cache, else Drive tracks.json, else walk FLACs once (after volume wipe)."""
    with _INDEX_LOCK:
        cached = load_index_entries(ctx)
        if cached is not None:
            log.info("library tag index ready source=cache tracks=%s", len(cached))
            return "cache"
        entries = rebuild_index(ctx)
        log.info("library tag index ready source=walk tracks=%s", len(entries))
        return "walk"


def remember_library_tags(
    ctx: Ctx,
    *,
    kind: str,
    relative_path: str | None,
    drive_file_id: str | None,
    topic_name: str,
    tags: TagSet,
    telegram_file_id: str | None = None,
    chat_id: int | None = None,
    message_id: int | None = None,
    card_message_id: int | None = None,
    thread_id: int | None = None,
) -> None:
    if kind != "library" or not relative_path:
        return
    try:
        upsert_library_index(
            ctx,
            relative_path=relative_path,
            drive_file_id=drive_file_id,
            topic_name=topic_name,
            tags=tags,
            telegram_file_id=telegram_file_id,
            chat_id=chat_id,
            message_id=message_id,
            card_message_id=card_message_id,
            thread_id=thread_id,
        )
    except Exception:
        log.warning("library tag index upsert failed path=%s", relative_path, exc_info=True)
