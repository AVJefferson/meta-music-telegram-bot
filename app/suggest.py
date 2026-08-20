from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.genre import GenreMapper, genre_tokens
from app.models import TagSet, TrackRecord
from app.relocate import tags_from_track
from app.util import normalize_match_text, split_artist_field

log = logging.getLogger(__name__)

LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
SEED_ARTIST_CAP = 8
SEED_TRACK_CAP = 8
SIMILAR_ARTIST_CAP = 8
SIMILAR_ARTIST_LIMIT = 20
SIMILAR_TRACK_LIMIT = 15
TOP_TRACK_LIMIT = 10
SIMILAR_ARTIST_TRACK_LIMIT = 8
MAX_RESULTS = 40
PER_ARTIST_CAP = 2
MIN_SIMILAR_MATCH = 0.1
CACHE_DAYS = 7
SESSION_HOURS = 24
PAGE_SIZE = 8
LIBRARY_PAGE_RATIO = 0.5


@dataclass
class SeedTrack:
    artist: str
    title: str
    album: str = ""
    genre: str = ""
    topic_name: str = ""
    lastfm_tags: list[str] = field(default_factory=list)
    boosted: bool = False

    @property
    def tokens(self) -> list[str]:
        return unique_fold(genre_tokens(self.genre) + list(self.lastfm_tags))


@dataclass
class SimilarArtist:
    name: str
    match: float
    url: str = ""


@dataclass
class SimilarTrack:
    artist: str
    title: str
    match: float
    url: str = ""
    mbid: str | None = None


@dataclass
class Hit:
    artist: str
    title: str
    match: float
    url: str = ""
    mbid: str | None = None
    tags: tuple[str, ...] = ()
    vias: tuple[str, ...] = ()
    seed_tokens: tuple[str, ...] = ()
    boosted: bool = False
    artist_tags: tuple[str, ...] = ()


@dataclass
class Suggestion:
    artist: str
    title: str
    score: float
    why: str
    url: str
    mbid: str | None = None
    tags: list[str] = field(default_factory=list)
    in_library: bool = False
    track_id: int = 0
    chat_id: int = 0
    message_id: int = 0
    thread_id: int | None = None
    telegram_file_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Suggestion:
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})


class LastfmAPI(Protocol):
    async def similar_artists(self, artist: str) -> list[SimilarArtist]: ...

    async def similar_tracks(self, artist: str, title: str) -> list[SimilarTrack]: ...

    async def top_tracks(self, artist: str, limit: int = 10) -> list[SimilarTrack]: ...

    async def artist_tags(self, artist: str) -> list[str]: ...

    async def search_artist(self, query: str) -> str | None: ...

    async def search_track(self, query: str, artist: str = "") -> tuple[str, str] | None: ...


def owned_key(artist: str, title: str) -> str:
    return f"{normalize_match_text(artist)}|{normalize_match_text(title)}"


def primary_artist(artist: str) -> str:
    names = split_artist_field(artist)
    return names[0] if names else (artist or "").strip()


def unique_fold(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        token = " ".join((raw or "").split())
        if not token:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def lastfm_track_url(artist: str, title: str) -> str:
    artist_part = quote((artist or "").replace(" ", "+"), safe="+")
    title_part = quote((title or "").replace(" ", "+"), safe="+")
    return f"https://www.last.fm/music/{artist_part}/_/{title_part}"


def session_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")


def cache_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=CACHE_DAYS)).isoformat(timespec="seconds")


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _match_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def _loads(text: str | None, default):
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return default


def seed_from_track(track: TrackRecord, *, boosted: bool = False) -> SeedTrack:
    tags = tags_from_track(track)
    report = _loads(track.source_report_json, {})
    lastfm = report.get("lastfm_tags") if isinstance(report, dict) else None
    return SeedTrack(
        artist=tags.artist or track.artist or "",
        title=tags.title or track.title or "",
        album=tags.album or track.album or "",
        genre=tags.genre,
        topic_name=track.topic_name or "",
        lastfm_tags=[str(item) for item in lastfm or [] if item],
        boosted=boosted,
    )


def parse_library_filename(stem: str, album_artist: str = "") -> tuple[str, str]:
    parts = [chunk.strip() for chunk in (stem or "").split(" - ") if chunk.strip()]
    if len(parts) >= 3 and parts[1].isdigit():
        return parts[0], " - ".join(parts[2:])
    if len(parts) >= 2:
        if parts[0].isdigit():
            return album_artist, " - ".join(parts[1:])
        return parts[0], " - ".join(parts[1:])
    return album_artist, stem


def parse_library_relative(relative: str) -> SeedTrack | None:
    path = Path((relative or "").replace("\\", "/"))
    parts = path.parts
    if not parts:
        return None
    filename = parts[-1]
    if not filename.lower().endswith(".flac"):
        return None
    topic = parts[0] if len(parts) >= 2 else ""
    album_artist = parts[1] if len(parts) >= 3 else ""
    album = parts[2] if len(parts) >= 4 else ""
    artist, title = parse_library_filename(Path(filename).stem, album_artist)
    artist = artist or album_artist
    title = title or Path(filename).stem
    if not artist and not title:
        return None
    return SeedTrack(artist=artist, title=title, album=album, genre=topic, topic_name=topic)


def track_from_library_item(
    *,
    relative_path: str,
    file_name: str = "",
    drive_file_id: str | None = None,
) -> TrackRecord | None:
    seed = parse_library_relative(relative_path)
    if seed is None:
        return None
    tags = TagSet(
        title=seed.title,
        artist=seed.artist,
        album=seed.album,
        albumartist=seed.artist,
        genre=seed.topic_name,
    )
    return TrackRecord(
        id=0,
        mb_recording_id=None,
        acoustid=None,
        kind="library",
        local_path=None,
        sidecar_path=None,
        drive_file_id=drive_file_id,
        drive_url=None,
        relative_path=relative_path,
        status="uploaded",
        bit_depth=None,
        sample_rate=None,
        title=seed.title,
        artist=seed.artist,
        album=seed.album,
        error=None,
        created_at="",
        uploaded_at=None,
        tags_json=json.dumps(asdict(tags), ensure_ascii=False),
        topic_name=seed.topic_name,
        file_name=file_name or Path(relative_path).name,
    )


def leftover_matches_seed(seed: SeedTrack, leftover: str) -> bool:
    query = normalize_match_text(leftover)
    if not query:
        return False
    hay = " ".join(
        [
            normalize_match_text(seed.artist),
            normalize_match_text(seed.title),
            normalize_match_text(seed.album),
        ]
    )
    return all(word in hay for word in query.split())


def track_matches_language(seed: SeedTrack, language: str, mapper: GenreMapper) -> bool:
    want = language.casefold()
    if (seed.topic_name or "").casefold() == want:
        return True
    for token in seed.tokens:
        label = mapper.canonical_label(token)
        if mapper.token_bucket(token) == "languages" and (label or token).casefold() == want:
            return True
    return False


def languages_in(tags: list[str] | tuple[str, ...], mapper: GenreMapper) -> set[str]:
    out: set[str] = set()
    for raw in tags:
        if mapper.token_bucket(raw) != "languages":
            continue
        label = mapper.canonical_label(raw)
        if label:
            out.add(label.casefold())
    return out


def select_library_seeds(
    tracks: list[TrackRecord],
    mapper: GenreMapper,
    *,
    language: str | None = None,
    leftover: str = "",
    boosted: TrackRecord | None = None,
) -> list[SeedTrack]:
    seeds = [seed_from_track(track) for track in tracks]
    if language:
        seeds = [seed for seed in seeds if track_matches_language(seed, language, mapper)]
    if leftover:
        matched = [seed for seed in seeds if leftover_matches_seed(seed, leftover)]
        if matched:
            for seed in matched:
                seed.boosted = True
    if boosted is not None:
        extra = seed_from_track(boosted, boosted=True)
        key = owned_key(extra.artist, extra.title)
        seeds = [seed for seed in seeds if owned_key(seed.artist, seed.title) != key]
        seeds.insert(0, extra)
    return [seed for seed in seeds if primary_artist(seed.artist)]


def owned_keys_from_tracks(tracks: list[TrackRecord]) -> set[str]:
    keys: set[str] = set()
    for track in tracks:
        seed = seed_from_track(track)
        key = owned_key(seed.artist, seed.title)
        if key != "|":
            keys.add(key)
        if track.artist or track.title:
            keys.add(owned_key(track.artist or "", track.title or ""))
    return {key for key in keys if key != "|"}


def parse_similar_artists(payload: dict) -> list[SimilarArtist]:
    block = (payload.get("similarartists") or {}).get("artist")
    out: list[SimilarArtist] = []
    for item in _as_list(block):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(
            SimilarArtist(
                name=name,
                match=_match_float(item.get("match")),
                url=str(item.get("url") or ""),
            )
        )
    return out


def parse_similar_tracks(payload: dict) -> list[SimilarTrack]:
    block = (payload.get("similartracks") or {}).get("track")
    return _parse_track_list(block, default_match=0.0)


def parse_top_tracks(payload: dict, *, default_match: float = 0.8) -> list[SimilarTrack]:
    block = (payload.get("toptracks") or {}).get("track")
    return _parse_track_list(block, default_match=default_match)


def parse_artist_tags(payload: dict) -> list[str]:
    block = (payload.get("toptags") or {}).get("tag")
    out: list[str] = []
    for item in _as_list(block):
        if isinstance(item, dict) and item.get("name"):
            out.append(str(item["name"]))
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def parse_search_artist(payload: dict) -> str | None:
    matches = ((payload.get("results") or {}).get("artistmatches") or {}).get("artist")
    for item in _as_list(matches):
        if isinstance(item, dict) and item.get("name"):
            return str(item["name"]).strip() or None
    return None


def parse_search_track(payload: dict) -> tuple[str, str] | None:
    matches = ((payload.get("results") or {}).get("trackmatches") or {}).get("track")
    for item in _as_list(matches):
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        artist = item.get("artist")
        if isinstance(artist, dict):
            artist = artist.get("name")
        artist_name = str(artist or "").strip()
        if title and artist_name:
            return artist_name, title
    return None


def _parse_track_list(block: Any, *, default_match: float) -> list[SimilarTrack]:
    out: list[SimilarTrack] = []
    for item in _as_list(block):
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        artist = item.get("artist")
        if isinstance(artist, dict):
            artist = artist.get("name")
        artist_name = str(artist or "").strip()
        if not title or not artist_name:
            continue
        mbid = str(item.get("mbid") or "").strip() or None
        match = _match_float(item.get("match"), default_match)
        if item.get("match") in (None, ""):
            match = default_match
        out.append(
            SimilarTrack(
                artist=artist_name,
                title=title,
                match=match,
                url=str(item.get("url") or "") or lastfm_track_url(artist_name, title),
                mbid=mbid,
            )
        )
    return out


def merge_hits(hits: list[Hit]) -> list[Hit]:
    grouped: dict[str, Hit] = {}
    for hit in hits:
        key = owned_key(hit.artist, hit.title)
        if key == "|" or not hit.title:
            continue
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = hit
            continue
        grouped[key] = Hit(
            artist=existing.artist,
            title=existing.title,
            match=max(existing.match, hit.match),
            url=existing.url or hit.url,
            mbid=existing.mbid or hit.mbid,
            tags=tuple(unique_fold([*existing.tags, *hit.tags])),
            vias=tuple(unique_fold([*existing.vias, *hit.vias])),
            seed_tokens=tuple(unique_fold([*existing.seed_tokens, *hit.seed_tokens])),
            boosted=existing.boosted or hit.boosted,
            artist_tags=tuple(unique_fold([*existing.artist_tags, *hit.artist_tags])),
        )
    return list(grouped.values())


def token_overlap(left: list[str] | tuple[str, ...], right: list[str] | tuple[str, ...], mapper: GenreMapper) -> float:
    if not left or not right:
        return 0.0
    left_keys = { (mapper.canonical_label(item) or item).casefold() for item in left }
    right_keys = { (mapper.canonical_label(item) or item).casefold() for item in right }
    if not left_keys or not right_keys:
        return 0.0
    return len(left_keys & right_keys) / len(left_keys)


def other_language(hit: Hit, language: str | None, mapper: GenreMapper) -> bool:
    if not language:
        return False
    want = language.casefold()
    found = languages_in(hit.tags, mapper) | languages_in(hit.artist_tags, mapper)
    if not found:
        return False
    return want not in found


def query_tokens_ok(hit: Hit, query_tokens: list[str], mapper: GenreMapper) -> bool:
    if not query_tokens:
        return True
    want = { (mapper.canonical_label(item) or item).casefold() for item in query_tokens }
    have = { (mapper.canonical_label(item) or item).casefold() for item in (*hit.tags, *hit.seed_tokens, *hit.artist_tags) }
    return bool(want & have)


def score_hit(hit: Hit, query_tokens: list[str], seed_profile: list[str], mapper: GenreMapper) -> float:
    extra = max(0, len(unique_fold(list(hit.vias))) - 1)
    score = hit.match * (1.0 + 0.8 * extra)
    score += 0.2 * token_overlap(query_tokens, (*hit.tags, *hit.seed_tokens, *hit.artist_tags), mapper)
    score += 0.15 * token_overlap(seed_profile, (*hit.tags, *hit.artist_tags), mapper)
    if hit.boosted:
        score += 0.25
    return score


def why_text(hit: Hit) -> str:
    vias = unique_fold(list(hit.vias))
    if not vias:
        return ""
    if len(vias) == 1:
        return vias[0]
    if len(vias) == 2:
        return f"{vias[0]}, {vias[1]}"
    return f"{vias[0]}, {vias[1]} +"


def rank_suggestions(
    hits: list[Hit],
    *,
    owned: set[str],
    shown: set[str],
    mapper: GenreMapper,
    language: str | None = None,
    query_tokens: list[str] | None = None,
    seed_profile: list[str] | None = None,
    exclude: set[str] | None = None,
    per_artist: int = PER_ARTIST_CAP,
    limit: int = MAX_RESULTS,
) -> list[Suggestion]:
    query_tokens = query_tokens or []
    seed_profile = seed_profile or []
    skip = exclude or set()
    merged = merge_hits(hits)
    fresh: list[Hit] = []
    repeats: list[Hit] = []
    for hit in merged:
        key = owned_key(hit.artist, hit.title)
        if key in skip or key == "|" or not hit.title:
            continue
        if other_language(hit, language, mapper):
            continue
        if not query_tokens_ok(hit, query_tokens, mapper):
            continue
        if key in shown:
            repeats.append(hit)
        else:
            fresh.append(hit)

    def order(hit: Hit) -> float:
        return score_hit(hit, query_tokens, seed_profile, mapper)

    ranked = sorted(fresh, key=order, reverse=True) + sorted(repeats, key=order, reverse=True)
    in_hits = [hit for hit in ranked if owned_key(hit.artist, hit.title) in owned]
    out_hits = [hit for hit in ranked if owned_key(hit.artist, hit.title) not in owned]
    picked = apply_diversity(in_hits, per_artist=per_artist, limit=limit) + apply_diversity(
        out_hits, per_artist=per_artist, limit=limit
    )
    out: list[Suggestion] = []
    for hit in picked:
        url = hit.url or lastfm_track_url(hit.artist, hit.title)
        key = owned_key(hit.artist, hit.title)
        out.append(
            Suggestion(
                artist=hit.artist,
                title=hit.title,
                score=order(hit),
                why=why_text(hit),
                url=url,
                mbid=hit.mbid,
                tags=list(hit.tags),
                in_library=key in owned,
            )
        )
    return mix_library_pages(out)


def _weave(left: list[Suggestion], right: list[Suggestion]) -> list[Suggestion]:
    mixed: list[Suggestion] = []
    for index in range(max(len(left), len(right))):
        if index < len(left):
            mixed.append(left[index])
        if index < len(right):
            mixed.append(right[index])
    return mixed


def mix_library_pages(
    items: list[Suggestion],
    *,
    page_size: int = PAGE_SIZE,
    library_ratio: float = LIBRARY_PAGE_RATIO,
) -> list[Suggestion]:
    in_lib = [item for item in items if item.in_library]
    out_lib = [item for item in items if not item.in_library]
    in_quota = max(0, round(page_size * library_ratio))
    mixed: list[Suggestion] = []
    in_pos = 0
    out_pos = 0
    while in_pos < len(in_lib) or out_pos < len(out_lib):
        take_in = min(in_quota, len(in_lib) - in_pos, page_size)
        take_out = min(page_size - take_in, len(out_lib) - out_pos)
        leftover = page_size - take_in - take_out
        if leftover:
            extra_in = min(leftover, len(in_lib) - in_pos - take_in)
            take_in += extra_in
            leftover -= extra_in
        if leftover:
            take_out += min(leftover, len(out_lib) - out_pos - take_out)
        mixed.extend(_weave(in_lib[in_pos : in_pos + take_in], out_lib[out_pos : out_pos + take_out]))
        in_pos += take_in
        out_pos += take_out
    return mixed


def apply_diversity(hits: list[Hit], *, per_artist: int, limit: int) -> list[Hit]:
    counts: dict[str, int] = defaultdict(int)
    picked: list[Hit] = []
    for hit in hits:
        key = normalize_match_text(primary_artist(hit.artist))
        if counts[key] >= per_artist:
            continue
        counts[key] += 1
        picked.append(hit)
        if len(picked) >= limit:
            break
    return picked


def top_seed_artists(seeds: list[SeedTrack], cap: int = SEED_ARTIST_CAP) -> list[str]:
    counts: dict[str, tuple[int, str]] = {}
    for seed in seeds:
        name = primary_artist(seed.artist)
        key = normalize_match_text(name)
        if not key:
            continue
        count, label = counts.get(key, (0, name))
        bump = 3 if seed.boosted else 1
        counts[key] = (count + bump, label)
    ranked = sorted(counts.values(), key=lambda item: (-item[0], item[1].casefold()))
    return [name for _count, name in ranked[:cap]]


def seed_tracks_for_similar(seeds: list[SeedTrack], cap: int = SEED_TRACK_CAP) -> list[SeedTrack]:
    boosted = [seed for seed in seeds if seed.boosted and seed.title]
    rest = [seed for seed in seeds if not seed.boosted and seed.title]
    seen: set[str] = set()
    out: list[SeedTrack] = []
    for seed in boosted + rest:
        key = owned_key(seed.artist, seed.title)
        if key in seen or key == "|":
            continue
        seen.add(key)
        out.append(seed)
        if len(out) >= cap:
            break
    return out


def narrow_seeds_for_query(seeds: list[SeedTrack], query_tokens: list[str], mapper: GenreMapper) -> list[SeedTrack]:
    if not query_tokens:
        return seeds
    tagged = [seed for seed in seeds if token_overlap(query_tokens, seed.tokens, mapper) > 0]
    if not tagged:
        return seeds
    keys = {owned_key(seed.artist, seed.title) for seed in tagged}
    extra = [seed for seed in seeds if seed.boosted and owned_key(seed.artist, seed.title) not in keys]
    return tagged + extra


def seed_profile_tokens(seeds: list[SeedTrack]) -> list[str]:
    tokens: list[str] = []
    for seed in seeds:
        tokens.extend(seed.tokens)
    return unique_fold(tokens)


class LastfmClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, catalog: Any | None = None) -> None:
        self.http = http
        self.api_key = api_key
        self.catalog = catalog

    async def similar_artists(self, artist: str) -> list[SimilarArtist]:
        payload = await self._call("artist.getsimilar", artist=artist, limit=str(SIMILAR_ARTIST_LIMIT))
        return parse_similar_artists(payload)

    async def similar_tracks(self, artist: str, title: str) -> list[SimilarTrack]:
        payload = await self._call(
            "track.getsimilar",
            artist=artist,
            track=title,
            limit=str(SIMILAR_TRACK_LIMIT),
        )
        return parse_similar_tracks(payload)

    async def top_tracks(self, artist: str, limit: int = TOP_TRACK_LIMIT) -> list[SimilarTrack]:
        payload = await self._call("artist.gettoptracks", artist=artist, limit=str(limit))
        return parse_top_tracks(payload)

    async def artist_tags(self, artist: str) -> list[str]:
        payload = await self._call("artist.gettoptags", artist=artist)
        return parse_artist_tags(payload)

    async def search_artist(self, query: str) -> str | None:
        payload = await self._call("artist.search", artist=query, limit="5")
        return parse_search_artist(payload)

    async def search_track(self, query: str, artist: str = "") -> tuple[str, str] | None:
        params = {"track": query, "limit": "5"}
        if artist:
            params["artist"] = artist
        payload = await self._call("track.search", **params)
        return parse_search_track(payload)

    async def _call(self, method: str, **params: str) -> dict:
        key_payload = json.dumps({"method": method, **params}, sort_keys=True, ensure_ascii=False)
        if self.catalog is not None:
            cached = self.catalog.get_lastfm_cache(key_payload)
            if cached:
                data = _loads(cached, {})
                if isinstance(data, dict):
                    return data
        try:
            response = await self.http.get(
                LASTFM_URL,
                params={"method": method, "api_key": self.api_key, "format": "json", **params},
                timeout=20.0,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("last.fm %s failed: %s", method, exc)
            return {}
        if not isinstance(payload, dict):
            return {}
        if self.catalog is not None:
            self.catalog.set_lastfm_cache(key_payload, json.dumps(payload, ensure_ascii=False), cache_expires_at())
        return payload


async def resolve_leftover(client: LastfmAPI, leftover: str) -> SeedTrack | None:
    text = " ".join((leftover or "").split())
    if not text:
        return None
    if " - " in text:
        artist, title = text.split(" - ", 1)
        hit = await client.search_track(title.strip(), artist.strip())
        if hit:
            return SeedTrack(artist=hit[0], title=hit[1], boosted=True)
    artist = await client.search_artist(text)
    if artist:
        return SeedTrack(artist=artist, title="", boosted=True)
    hit = await client.search_track(text)
    if hit:
        return SeedTrack(artist=hit[0], title=hit[1], boosted=True)
    return None


async def collect_hits(client: LastfmAPI, seeds: list[SeedTrack], *, language: str | None, mapper: GenreMapper) -> list[Hit]:
    seed_artists = top_seed_artists(seeds)
    seed_artist_keys = {normalize_match_text(name) for name in seed_artists}
    track_seeds = seed_tracks_for_similar(seeds)
    hits: list[Hit] = []
    similar_scores: dict[str, tuple[float, str]] = {}

    for artist in seed_artists:
        seed_tok = _tokens_for_artist(seeds, artist)
        boosted = any(
            seed.boosted and normalize_match_text(primary_artist(seed.artist)) == normalize_match_text(artist)
            for seed in seeds
        )
        for track in await client.top_tracks(artist, TOP_TRACK_LIMIT):
            hits.append(
                _hit_from_track(
                    track,
                    via=f"more from {artist}",
                    seed_tokens=seed_tok,
                    boosted=boosted,
                )
            )
        for similar in await client.similar_artists(artist):
            if similar.match < MIN_SIMILAR_MATCH:
                continue
            key = normalize_match_text(similar.name)
            if not key or key in seed_artist_keys:
                continue
            score, name = similar_scores.get(key, (0.0, similar.name))
            similar_scores[key] = (score + similar.match, name)

    for seed in track_seeds:
        artist = primary_artist(seed.artist)
        for track in await client.similar_tracks(artist, seed.title):
            if track.match < MIN_SIMILAR_MATCH:
                continue
            hits.append(
                _hit_from_track(
                    track,
                    via=f"similar to {artist}",
                    seed_tokens=seed.tokens,
                    boosted=seed.boosted,
                )
            )

    ranked_similar = sorted(similar_scores.values(), key=lambda item: -item[0])[:SIMILAR_ARTIST_CAP]
    for score, name in ranked_similar:
        tags = await client.artist_tags(name) if language else []
        probe = Hit(artist=name, title="x", match=0, artist_tags=tuple(tags))
        if language and other_language(probe, language, mapper):
            continue
        seed_tok, seed_name = _nearest_seed_tokens(seeds, name)
        via = f"similar to {seed_name}" if seed_name else f"similar artist {name}"
        track_match = max(MIN_SIMILAR_MATCH, min(0.85, score))
        for track in await client.top_tracks(name, SIMILAR_ARTIST_TRACK_LIMIT):
            hits.append(
                _hit_from_track(
                    track,
                    via=via,
                    seed_tokens=seed_tok,
                    artist_tags=tags,
                    match=track_match,
                )
            )
    return hits


def _tokens_for_artist(seeds: list[SeedTrack], artist: str) -> list[str]:
    key = normalize_match_text(artist)
    tokens: list[str] = []
    for seed in seeds:
        if normalize_match_text(primary_artist(seed.artist)) == key:
            tokens.extend(seed.tokens)
    return unique_fold(tokens)


def _nearest_seed_tokens(seeds: list[SeedTrack], _similar_name: str) -> tuple[list[str], str]:
    if not seeds:
        return [], ""
    boosted = next((seed for seed in seeds if seed.boosted), seeds[0])
    return boosted.tokens, primary_artist(boosted.artist)


def _hit_from_track(
    track: SimilarTrack,
    *,
    via: str,
    seed_tokens: list[str],
    boosted: bool = False,
    artist_tags: list[str] | None = None,
    match: float | None = None,
) -> Hit:
    return Hit(
        artist=track.artist,
        title=track.title,
        match=track.match if match is None else match,
        url=track.url,
        mbid=track.mbid,
        vias=(via,),
        seed_tokens=tuple(seed_tokens),
        boosted=boosted,
        artist_tags=tuple(artist_tags or ()),
    )


def hits_from_library(
    library: list[TrackRecord],
    seeds: list[SeedTrack],
    mapper: GenreMapper,
    language: str | None,
) -> list[Hit]:
    seed_keys = {owned_key(seed.artist, seed.title) for seed in seeds if seed.title}
    seed_artists = {normalize_match_text(primary_artist(seed.artist)) for seed in seeds}
    profile = seed_profile_tokens(seeds)
    hits: list[Hit] = []
    for track in library:
        seed = seed_from_track(track)
        key = owned_key(seed.artist, seed.title)
        if not seed.title or key in seed_keys:
            continue
        if language and not track_matches_language(seed, language, mapper):
            continue
        artist_key = normalize_match_text(primary_artist(seed.artist))
        match = 0.75 if artist_key in seed_artists else 0.35
        via = f"more from {primary_artist(seed.artist)}" if artist_key in seed_artists else "in your library"
        match = min(1.0, match + 0.2 * token_overlap(profile, seed.tokens, mapper))
        hits.append(
            Hit(
                artist=seed.artist,
                title=seed.title,
                match=match,
                vias=(via,),
                seed_tokens=tuple(seed.tokens),
            )
        )
    return hits


def attach_library_meta(items: list[Suggestion], tracks: list[TrackRecord]) -> list[Suggestion]:
    by_key: dict[str, TrackRecord] = {}
    for track in tracks:
        seed = seed_from_track(track)
        by_key[owned_key(seed.artist, seed.title)] = track
    for item in items:
        track = by_key.get(owned_key(item.artist, item.title))
        if track is None:
            continue
        item.in_library = True
        item.track_id = track.id
        item.chat_id = track.source_chat_id or 0
        item.message_id = track.source_message_id or 0
        item.thread_id = track.thread_id
        item.telegram_file_id = track.telegram_file_id or ""
    return items


async def suggest_tracks(
    client: LastfmAPI,
    seeds: list[SeedTrack],
    *,
    owned: set[str],
    shown: set[str],
    mapper: GenreMapper,
    language: str | None = None,
    query_tokens: list[str] | None = None,
    library: list[TrackRecord] | None = None,
) -> list[Suggestion]:
    query_tokens = query_tokens or []
    seeds = narrow_seeds_for_query(seeds, query_tokens, mapper)
    hits = await collect_hits(client, seeds, language=language, mapper=mapper)
    if library:
        hits.extend(hits_from_library(library, seeds, mapper, language))
    exclude = {owned_key(seed.artist, seed.title) for seed in seeds if seed.title}
    items = rank_suggestions(
        hits,
        owned=owned,
        shown=shown,
        mapper=mapper,
        language=language,
        query_tokens=query_tokens,
        seed_profile=seed_profile_tokens(seeds),
        exclude=exclude,
    )
    return attach_library_meta(items, library or [])
