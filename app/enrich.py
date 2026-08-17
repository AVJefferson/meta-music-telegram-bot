from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

import httpx
from PIL import Image

from app.genre import GenreMapper
from app.models import Enrichment, Identity
from app.util import format_clock, is_synced_lrc, normalize_match_text

log = logging.getLogger(__name__)

MAX_COVER_BYTES = 2_000_000
MAX_COVER_PX = 1400
CAA_FRONT_LIMIT = 5
_LRC_STAMP = re.compile(r"\[\d{1,2}:\d{2}(?:[\.:]\d+)?\]")


@dataclass
class LyricsHit:
    lyrics: str = ""
    duration: float | None = None
    instrumental: bool = False


def _stamp_seconds(stamp: str) -> float | None:
    inner = stamp.strip().strip("[]")
    parts = re.split(r"[:.]", inner)
    if len(parts) < 2:
        return None
    try:
        minutes = int(parts[0])
        secs = int(parts[1])
    except ValueError:
        return None
    frac = 0.0
    if len(parts) >= 3 and parts[2]:
        try:
            frac = float(f"0.{parts[2]}")
        except ValueError:
            frac = 0.0
    return minutes * 60 + secs + frac


def lyrics_preview(text: str, *, lines: int = 3, keep_time: bool = False) -> str:
    out: list[str] = []
    for raw in (text or "").splitlines():
        stamp = _LRC_STAMP.search(raw)
        line = _LRC_STAMP.sub("", raw).strip()
        if not line or (line.startswith("[") and line.endswith("]")):
            continue
        if keep_time and stamp:
            clock = format_clock(_stamp_seconds(stamp.group(0)), seekable=True)
            if clock:
                line = f"{clock}  {line}"
        out.append(line)
        if len(out) >= lines:
            break
    return "\n".join(out)


def lyrics_card_text(text: str | None, *, lines: int = 3) -> str:
    raw = (text or "").strip()
    if not raw:
        return "Lyrics: none"
    preview = lyrics_preview(raw, lines=lines, keep_time=is_synced_lrc(raw))
    if not preview:
        return "Lyrics: present"
    return f"Lyrics:\n{preview}"


def _fit_cover(data: bytes) -> tuple[bytes, str]:
    image = Image.open(io.BytesIO(data))
    image = image.convert("RGB")
    image.thumbnail((MAX_COVER_PX, MAX_COVER_PX))
    quality = 90
    buf = io.BytesIO()
    while quality >= 65:
        buf.seek(0)
        buf.truncate()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_COVER_BYTES:
            return buf.getvalue(), "image/jpeg"
        quality -= 5
    return buf.getvalue(), "image/jpeg"


async def _download_cover(http: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    try:
        response = await http.get(url, follow_redirects=True, timeout=45.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("cover download failed %s: %s", url, exc)
        return None
    data = response.content
    if not data:
        return None
    mime = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if len(data) > MAX_COVER_BYTES or mime not in {"image/jpeg", "image/png", "image/webp"}:
        try:
            return _fit_cover(data)
        except Exception:
            log.debug("cover resize failed")
            return None
    if mime != "image/jpeg":
        try:
            return _fit_cover(data)
        except Exception:
            return data, mime
    return data, mime


async def _caa_payloads(http: httpx.AsyncClient, identity: Identity) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for kind, mbid in (
        ("release-group", identity.mb_release_group_id),
        ("release", identity.mb_release_id),
    ):
        if not mbid:
            continue
        url = f"https://coverartarchive.org/{kind}/{mbid}"
        try:
            response = await http.get(url, follow_redirects=True, timeout=30.0)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError:
            continue
        out.append((mbid, payload))
    return out


async def _download_caa_entry(
    http: httpx.AsyncClient, image: dict
) -> tuple[tuple[bytes, str], str] | None:
    thumbs = image.get("thumbnails") or {}
    for key in ("1200", "large", "500", "small"):
        thumb = thumbs.get(key)
        if thumb:
            got = await _download_cover(http, str(thumb))
            if got:
                return got, str(thumb)
    original = image.get("image")
    if original:
        got = await _download_cover(http, str(original))
        if got:
            return got, str(original)
    return None


def _caa_image_key(image: dict) -> str:
    return str(image.get("id") or image.get("image") or "")


async def list_caa_fronts(
    http: httpx.AsyncClient,
    identity: Identity,
    *,
    limit: int = CAA_FRONT_LIMIT,
    fronts_only: bool = True,
) -> list[tuple[tuple[bytes, str], str, str]]:
    payloads = await _caa_payloads(http, identity)
    buckets: list[tuple[str, list, list]] = []
    for mbid, payload in payloads:
        images = payload.get("images") or []
        fronts = [img for img in images if img.get("front")]
        buckets.append((mbid, fronts, images))
    picks: list[tuple[str, dict]] = []
    for mbid, fronts, _images in buckets:
        if fronts:
            picks.extend((mbid, img) for img in fronts)
            break
    if not picks and not fronts_only:
        for mbid, _fronts, images in buckets:
            if images:
                picks.append((mbid, images[0]))
                break
    results: list[tuple[tuple[bytes, str], str, str]] = []
    seen: set[str] = set()
    for mbid, image in picks:
        if len(results) >= limit:
            break
        key = _caa_image_key(image)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        got = await _download_caa_entry(http, image)
        if got:
            cover, url = got
            results.append((cover, mbid, url))
    return results


async def _cover_from_caa(http: httpx.AsyncClient, identity: Identity) -> tuple[bytes, str] | None:
    listed = await list_caa_fronts(http, identity, limit=1, fronts_only=False)
    return listed[0][0] if listed else None


async def _itunes_search(http: httpx.AsyncClient, identity: Identity) -> list[dict]:
    term = " ".join(p for p in [identity.artists[0] if identity.artists else "", identity.title] if p)
    if not term:
        return []
    try:
        response = await http.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "song", "limit": 5},
            timeout=20.0,
        )
        response.raise_for_status()
        return response.json().get("results") or []
    except httpx.HTTPError:
        return []


def _itunes_match(identity: Identity, results: list[dict]) -> dict | None:
    title_cf = identity.title.casefold()
    for item in results:
        track = str(item.get("trackName") or "").casefold()
        if title_cf and title_cf not in track and track not in title_cf:
            continue
        return item
    return results[0] if results else None


def _itunes_report(item: dict | None) -> dict:
    if not item:
        return {}
    art = item.get("artworkUrl100") or item.get("artworkUrl60")
    year = ""
    raw_date = str(item.get("releaseDate") or "")
    if len(raw_date) >= 4 and raw_date[:4].isdigit():
        year = raw_date[:4]
    return {
        "title": item.get("trackName") or "",
        "artist": item.get("artistName") or "",
        "album": item.get("collectionName") or "",
        "year": year,
        "genre": item.get("primaryGenreName") or "",
        "artwork": bool(art),
    }


async def _cover_from_itunes_item(http: httpx.AsyncClient, item: dict) -> tuple[bytes, str] | None:
    art = item.get("artworkUrl100") or item.get("artworkUrl60")
    if not art:
        return None
    for size in ("1200x1200bb", "600x600bb"):
        candidate = art.replace("100x100bb", size).replace("60x60bb", size)
        got = await _download_cover(http, candidate)
        if got:
            return got
    return None


async def _itunes_album_search(http: httpx.AsyncClient, identity: Identity) -> list[dict]:
    artist = ""
    if identity.album_artists:
        artist = identity.album_artists[0]
    elif identity.artists:
        artist = identity.artists[0]
    album = identity.album or ""
    terms: list[str] = []
    if artist and album:
        terms.append(f"{artist} {album}")
    if album:
        terms.append(album)
    seen: set[str] = set()
    out: list[dict] = []
    for term in terms:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            response = await http.get(
                "https://itunes.apple.com/search",
                params={"term": term, "entity": "album", "limit": 5},
                timeout=20.0,
            )
            response.raise_for_status()
            out.extend(response.json().get("results") or [])
        except httpx.HTTPError:
            continue
    return out


def _itunes_album_match(identity: Identity, results: list[dict]) -> dict | None:
    album_n = normalize_match_text(identity.album)
    if not album_n:
        return results[0] if results else None
    for item in results:
        name = normalize_match_text(str(item.get("collectionName") or ""))
        if name == album_n or album_n in name or name in album_n:
            return item
    return results[0] if results else None


async def list_itunes_album_cover(
    http: httpx.AsyncClient, identity: Identity
) -> tuple[tuple[bytes, str], str] | None:
    album_item = _itunes_album_match(identity, await _itunes_album_search(http, identity))
    if not album_item:
        return None
    cover = await _cover_from_itunes_item(http, album_item)
    if not cover:
        return None
    art = str(album_item.get("artworkUrl100") or album_item.get("artworkUrl60") or "")
    url = ""
    if art:
        url = art.replace("100x100bb", "1200x1200bb").replace("60x60bb", "1200x1200bb")
    return cover, url


async def fetch_cover(
    http: httpx.AsyncClient,
    identity: Identity,
    *,
    album_search: bool,
) -> tuple[tuple[bytes, str] | None, str, str | None]:
    cover = await _cover_from_caa(http, identity)
    if cover is not None:
        caa = identity.mb_release_group_id or identity.mb_release_id
        return cover, "caa", caa
    if album_search:
        album_item = _itunes_album_match(identity, await _itunes_album_search(http, identity))
        if album_item:
            cover = await _cover_from_itunes_item(http, album_item)
            if cover is not None:
                return cover, "itunes", None
        return None, "none", None
    results = await _itunes_search(http, identity)
    item = _itunes_match(identity, results)
    if item:
        cover = await _cover_from_itunes_item(http, item)
        if cover is not None:
            return cover, "itunes", None
    return None, "none", None


async def _lastfm_tags(http: httpx.AsyncClient, api_key: str, identity: Identity) -> list[str]:
    if not api_key or not identity.title or not identity.artists:
        return []
    try:
        response = await http.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "track.gettoptags",
                "artist": identity.artists[0],
                "track": identity.title,
                "api_key": api_key,
                "format": "json",
            },
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError:
        return []
    tags = ((payload.get("toptags") or {}).get("tag")) or []
    out: list[str] = []
    for tag in tags[:15]:
        name = tag.get("name") if isinstance(tag, dict) else None
        try:
            count = int(tag.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if name and count >= 2:
            out.append(str(name))
    return out


async def fetch_lrclib(http: httpx.AsyncClient, identity: Identity) -> LyricsHit | None:
    if not identity.title or not identity.artists:
        return None
    params = {
        "track_name": identity.title,
        "artist_name": identity.artists[0],
    }
    if identity.album:
        params["album_name"] = identity.album
    if identity.duration:
        params["duration"] = str(round(identity.duration))
    try:
        response = await http.get("https://lrclib.net/api/get", params=params, timeout=20.0)
        if response.status_code == 404:
            response = await http.get(
                "https://lrclib.net/api/search",
                params={"track_name": identity.title, "artist_name": identity.artists[0]},
                timeout=20.0,
            )
            response.raise_for_status()
            results = response.json() or []
            payload = results[0] if results else None
        else:
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError:
        return None
    if not payload:
        return None
    duration_raw = payload.get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    if payload.get("instrumental"):
        return LyricsHit(duration=duration, instrumental=True)
    synced = str(payload.get("syncedLyrics") or "")
    plain = str(payload.get("plainLyrics") or "")
    lyrics = synced if is_synced_lrc(synced) else plain
    if not lyrics.strip():
        return None
    return LyricsHit(lyrics=lyrics, duration=duration)


async def _lrclib(http: httpx.AsyncClient, identity: Identity) -> tuple[str | None, bool]:
    hit = await fetch_lrclib(http, identity)
    if hit is None:
        return None, False
    if hit.instrumental:
        return None, True
    if is_synced_lrc(hit.lyrics):
        return hit.lyrics, False
    return None, False


async def enrich(
    http: httpx.AsyncClient,
    identity: Identity,
    genre: GenreMapper,
    lastfm_api_key: str,
    topic_language: str | None,
    *,
    cover: bytes | None = None,
    cover_mime: str | None = None,
    cover_source: str = "none",
    caa_release: str | None = None,
) -> Enrichment:
    itunes_results = await _itunes_search(http, identity)
    itunes_item = _itunes_match(identity, itunes_results)
    itunes_meta = _itunes_report(itunes_item)

    lyrics, instrumental = await _lrclib(http, identity)
    extra_tags = list(identity.raw_genre_tags)
    itunes_genre = str(itunes_meta.get("genre") or "")
    if itunes_genre:
        extra_tags.insert(0, itunes_genre)
    lastfm_tags = await _lastfm_tags(http, lastfm_api_key, identity)
    extra_tags.extend(lastfm_tags)
    if instrumental:
        extra_tags.append("Instrumental")

    genre_str = genre.classify(extra_tags, extra_language=topic_language)
    return Enrichment(
        cover=cover,
        cover_mime=cover_mime,
        lyrics=lyrics,
        genre=genre_str,
        instrumental=instrumental,
        cover_source=cover_source,
        caa_release=caa_release,
        itunes_report=itunes_meta,
        lastfm_tags=lastfm_tags,
    )
