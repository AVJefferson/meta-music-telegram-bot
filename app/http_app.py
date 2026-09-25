from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, fields
from pathlib import Path

from aiohttp import web

from app.errors import AppError, to_app_error
from app.formats import (
    clamp_suggest_similarity,
    normalize_allowed,
    normalize_save_dest,
    normalize_suggest_library,
    save_prefs_from_settings,
    truthy_setting,
    user_settings_dict,
)
from app.initdata import parse_init_user
from app.membership import check_api_rate, is_admin, user_is_blocked
from app.models import Ctx, TagSet, tagset_from_dict
from app.oauth import (
    exchange_code,
    finish_login,
    google_auth_url,
    issue_login_ticket,
    public_base_url,
    validate_origin,
)
from app.util import html_esc

log = logging.getLogger(__name__)
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"


def _html(title: str, body: str, status: int = 200) -> web.Response:
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>{html_esc(title)}</title>
  <style>
    :root {{ color-scheme: dark light; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 1.5rem;
      font: 17px/1.5 ui-sans-serif, system-ui, sans-serif;
      background: #12100e;
      color: #f3eee6;
    }}
    main {{
      width: min(28rem, 100%);
      padding: 1.5rem 1.35rem;
      border-radius: 1rem;
      background: #1c1814;
      border: 1px solid rgba(243, 238, 230, 0.12);
    }}
    h1 {{ font-size: 1.25rem; margin: 0 0 0.6rem; letter-spacing: -0.03em; }}
    p {{ margin: 0; color: #b7aea1; }}
  </style>
</head>
<body>
  <main>
    <h1>{html_esc(title)}</h1>
    <p>{body}</p>
  </main>
</body>
</html>"""
    return web.Response(text=page, content_type="text/html", status=status)


def create_http_app(ctx: Ctx) -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app["ctx"] = ctx
    app.router.add_get("/oauth/start", oauth_start)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/app", webapp_index)
    app.router.add_get("/app/{page}", webapp_index)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/admin/overview", api_admin_overview)
    app.router.add_get("/api/login", api_login)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/review", api_review)
    app.router.add_post("/api/review/{track_id}/action", api_review_action)
    app.router.add_get("/api/suggest", api_suggest)
    app.router.add_get("/api/suggest/art", api_suggest_art)
    app.router.add_get("/api/suggest/cover", api_suggest_cover)
    static_dir = WEBAPP_DIR / "static"
    if static_dir.is_dir():
        app.router.add_static("/app/static/", static_dir)
    return app


async def start_http(ctx: Ctx) -> web.AppRunner | None:
    try:
        public_base_url(ctx.settings)
    except AppError:
        log.warning("PUBLIC_BASE_URL missing or not https; OAuth HTTP not started")
        return None
    app = create_http_app(ctx)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(getattr(ctx.settings, "oauth_http_port", 8080) or 8080)
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("oauth http listening on %s", port)
    return runner


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except AppError as exc:
        if request.path.startswith("/api/"):
            return web.json_response(exc.as_json(), status=exc.http_status)
        return _html("Error", html_esc(exc.user_message), exc.http_status)
    except Exception:
        log.exception("http handler failed path=%s", request.path)
        if request.path.startswith("/api/"):
            return web.json_response(AppError("internal").as_json(), status=500)
        return _html("Error", "Failed. Retry.", 500)


async def oauth_start(request: web.Request) -> web.Response:
    ctx: Ctx = request.app["ctx"]
    token = request.query.get("token") or ""
    user_id = ctx.catalog.consume_oauth_ticket(token)
    if not user_id:
        return _html("Login", "This login link expired. Use /login again.", 404)
    try:
        url = google_auth_url(ctx, user_id)
    except AppError as exc:
        return _html("Login", html_esc(exc.user_message), exc.http_status)
    raise web.HTTPFound(url)


async def oauth_callback(request: web.Request) -> web.Response:
    ctx: Ctx = request.app["ctx"]
    if request.query.get("error"):
        return _html("Login", "Google denied access. Use /login again.", 403)
    state = request.query.get("state") or ""
    code = request.query.get("code") or ""
    got = ctx.catalog.consume_oauth_state(state)
    if not got or not code:
        return _html("Login", "This login session expired. Use /login again.", 404)
    user_id, verifier = got
    try:
        payload = await exchange_code(ctx, code=code, verifier=verifier)
        await finish_login(ctx, user_id, payload)
    except AppError as exc:
        return _html("Login", html_esc(exc.user_message), exc.http_status)
    except Exception:
        log.exception("oauth callback failed")
        return _html("Login", "Failed. Use /login again.", 502)
    return _html("Login", "Google Drive connected. You can close this and return to Telegram.")


def _page_html(page: str) -> str:
    return (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")


async def webapp_index(request: web.Request) -> web.Response:
    if not (WEBAPP_DIR / "index.html").is_file():
        return _html("App", "Mini App missing.", 500)
    return web.Response(text=_page_html(request.match_info.get("page") or ""), content_type="text/html")


async def _api_user(request: web.Request) -> tuple[Ctx, int]:
    ctx: Ctx = request.app["ctx"]
    validate_origin(ctx.settings, request.headers.get("Origin"))
    init_data = request.headers.get("X-Telegram-Init-Data") or request.query.get("initData") or ""
    max_age = int(getattr(ctx.settings, "initdata_max_age_seconds", 300) or 300)
    profile = parse_init_user(ctx.settings.bot_token, init_data, max_age=max_age)
    user_id = int(profile["id"])
    if await user_is_blocked(ctx, user_id) and not is_admin(ctx, user_id):
        raise AppError("blacklisted")
    check_api_rate(ctx, user_id)
    ctx.catalog.touch_user(
        user_id,
        username=profile.get("username"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
    )
    return ctx, user_id


def _read_proc_rss() -> int:
    try:
        text = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return 0


def _read_proc_uptime() -> float:
    try:
        stat = Path("/proc/self/stat").read_text(encoding="utf-8")
        right = stat.rsplit(")", 1)[1].strip().split()
        start_ticks = int(right[19])
        clk = os.sysconf("SC_CLK_TCK")
        boot = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return 0.0
    if not clk:
        return 0.0
    return max(0.0, boot - (start_ticks / clk))


def _server_stats(ctx: Ctx) -> dict[str, float | int]:
    path = getattr(ctx.catalog, "path", None)
    size = 0
    if path:
        try:
            size = int(Path(path).stat().st_size)
        except OSError:
            size = 0
    return {
        "rss_bytes": _read_proc_rss(),
        "sqlite_bytes": size,
        "uptime_seconds": round(_read_proc_uptime(), 1),
    }


async def api_admin_overview(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
    except AppError as exc:
        return web.json_response(exc.as_json(), status=exc.http_status)
    if not is_admin(ctx, user_id):
        return web.json_response(AppError("forbidden").as_json(), status=403)
    payload = ctx.catalog.admin_overview()
    payload["server"] = _server_stats(ctx)
    return _json_ok(payload)


def _json_ok(data: dict, status: int = 200) -> web.Response:
    payload = {"ok": True, **data}
    return web.json_response(payload, status=status)


_TAG_KEYS = tuple(item.name for item in fields(TagSet))


def _count_library(ctx: Ctx, user_id: int) -> int:
    sqlite_n = ctx.catalog.count_library_tracks(user_id)
    try:
        from app.library_index import load_index_entries
        from app.relocate import ctx_for_user

        bound = ctx_for_user(ctx, user_id)
        entries = load_index_entries(bound)
    except Exception:
        log.debug("library index count failed user=%s", user_id, exc_info=True)
        return sqlite_n
    if entries:
        return len(entries)
    return sqlite_n


def _suggest_prefs(settings: dict) -> tuple[float, str]:
    sim = clamp_suggest_similarity(settings.get("suggest_similarity", 0.5))
    library = normalize_suggest_library(settings.get("suggest_library"))
    return sim, library


def _save_prefs_payload(settings: dict) -> dict:
    prefs = save_prefs_from_settings(settings)
    return {
        "default_dest": prefs.dest,
        "correct_telegram": prefs.correct_telegram,
        "skip_save_prompt": prefs.skip_save_prompt,
        "delete_original": prefs.delete_original,
    }


def _tags_from_body(body: dict, current: TagSet) -> TagSet:
    data = asdict(current)
    for key in _TAG_KEYS:
        if key in body:
            data[key] = str(body.get(key) or "")
    return tagset_from_dict(data)


def _track_payload(track) -> dict:
    from app.relocate import tags_from_track

    tags = asdict(tags_from_track(track))
    return {
        "id": track.id,
        "kind": track.kind,
        "file_name": track.file_name or "",
        "relative_path": track.relative_path or "",
        "drive_url": track.drive_url or "",
        "status": track.status or "",
        **tags,
    }


def _decorate_suggest_row(row: dict, *, hifi_user: str) -> dict:
    from app.suggest import suggest_links

    artist = str(row.get("artist") or "")
    title = str(row.get("title") or "")
    mbid = str(row.get("mbid") or "")
    links = row.get("links") if isinstance(row.get("links"), dict) else None
    if not links:
        links = suggest_links(artist, title, mbid=mbid, hifi_user=hifi_user)
    return {**row, "mbid": mbid, "links": links}


async def _relocate_review(ctx: Ctx, track, *, kind: str, body: dict):
    from app.relocate import hydrate_track_tags, identity_from_track, relocate_track, tags_from_track

    try:
        track = await hydrate_track_tags(ctx, track)
    except Exception:
        log.debug("hydrate before review action failed track=%s", track.id, exc_info=True)
        track = ctx.catalog.get_track(track.id) or track
    tags = _tags_from_body(body, tags_from_track(track))
    await relocate_track(
        ctx,
        track,
        kind=kind,
        tags=tags,
        identity=identity_from_track(track),
        source_report={},
        topic_name=track.topic_name or "Unknown",
        file_name=track.file_name or "track.flac",
    )
    return ctx.catalog.get_track(track.id) or track


async def api_me(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
    except AppError as cop:
        return web.json_response(cop.as_json(), status=cop.http_status)
    user = ctx.catalog.ensure_user(user_id)
    months = int(getattr(ctx.settings, "user_inactive_months", 3) or 3)
    settings = user_settings_dict(user)
    sim, library = _suggest_prefs(settings)
    library_count, review_count = await asyncio.gather(
        asyncio.to_thread(_count_library, ctx, user_id),
        asyncio.to_thread(ctx.catalog.count_review_tracks, user_id),
    )
    return _json_ok(
        {
            "user_since": user.first_seen_at,
            "songs_edited": user.songs_edited,
            "google_email": user.google_email,
            "logged_in": user.logged_in,
            "inactive_months": months,
            **_save_prefs_payload(settings),
            "allowed_formats": normalize_allowed(settings.get("allowed_formats")),
            "suggest_similarity": sim,
            "suggest_library": library,
            "admin": is_admin(ctx, user_id),
            "library_count": library_count,
            "review_count": review_count,
        }
    )


async def api_settings(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
        body = await request.json()
    except AppError as cop:
        return web.json_response(cop.as_json(), status=cop.http_status)
    except Exception:
        return web.json_response(AppError("bad_input").as_json(), status=400)
    body.pop("user_id", None)
    if body.get("unlink"):
        ctx.catalog.clear_user_google(user_id)
        hub = getattr(ctx, "drive", None)
        drop = getattr(hub, "drop", None)
        if callable(drop):
            drop(user_id)
        return _json_ok({"logged_in": False})
    dest = None
    if "default_dest" in body:
        dest = normalize_save_dest(body.get("default_dest"))
    user = ctx.catalog.ensure_user(user_id)
    settings = user_settings_dict(user)
    if dest is not None:
        settings["default_dest"] = dest
    if "allowed_formats" in body:
        settings["allowed_formats"] = normalize_allowed(body.get("allowed_formats"))
    if "suggest_similarity" in body:
        settings["suggest_similarity"] = clamp_suggest_similarity(body.get("suggest_similarity"))
    if "suggest_library" in body:
        settings["suggest_library"] = normalize_suggest_library(body.get("suggest_library"))
    if "correct_telegram" in body:
        settings["correct_telegram"] = truthy_setting(body.get("correct_telegram"))
    if "skip_save_prompt" in body:
        settings["skip_save_prompt"] = truthy_setting(body.get("skip_save_prompt"))
    if "delete_original" in body:
        settings["delete_original"] = truthy_setting(body.get("delete_original"))
    if (
        dest is None
        and "allowed_formats" not in body
        and "suggest_similarity" not in body
        and "suggest_library" not in body
        and "correct_telegram" not in body
        and "skip_save_prompt" not in body
        and "delete_original" not in body
    ):
        settings["default_dest"] = normalize_save_dest(body.get("default_dest"))
    ctx.catalog.update_user(user_id, settings_json=json.dumps(settings))
    sim, library = _suggest_prefs(settings)
    return _json_ok(
        {
            **_save_prefs_payload(settings),
            "allowed_formats": normalize_allowed(settings.get("allowed_formats")),
            "suggest_similarity": sim,
            "suggest_library": library,
            "logged_in": user.logged_in,
            "google_email": user.google_email,
        }
    )


async def api_review(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
    except AppError as cop:
        return web.json_response(cop.as_json(), status=cop.http_status)
    from app.review_cmd import _sync_drive_review

    await _sync_drive_review(ctx, user_id)
    tracks = ctx.catalog.list_review_tracks(user_id)
    return _json_ok({"tracks": [_track_payload(track) for track in tracks[:100]]})


async def api_review_action(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
        track_id = int(request.match_info["track_id"])
        body = await request.json()
    except AppError as cop:
        return web.json_response(cop.as_json(), status=cop.http_status)
    except Exception:
        return web.json_response(AppError("bad_input").as_json(), status=400)
    body.pop("user_id", None)
    track = ctx.catalog.get_track(track_id)
    if track is None or getattr(track, "user_id", 0) != user_id:
        return web.json_response(AppError("not_found").as_json(), status=404)
    action = str(body.get("action") or "")
    if action == "cancel":
        from app.relocate import delete_track

        try:
            await delete_track(ctx, track)
        except AppError as cop:
            return web.json_response(cop.as_json(), status=cop.http_status)
        except Exception as cop:
            err = to_app_error(cop)
            return web.json_response(err.as_json(), status=err.http_status)
        return _json_ok({"action": "cancel"})
    if action in {"library", "tags"}:
        kind = "library" if action == "library" else "review"
        try:
            updated = await _relocate_review(ctx, track, kind=kind, body=body)
        except AppError as cop:
            return web.json_response(cop.as_json(), status=cop.http_status)
        except Exception as cop:
            err = to_app_error(cop)
            return web.json_response(err.as_json(), status=err.http_status)
        payload: dict = {"action": action}
        if updated is not None:
            payload["track"] = _track_payload(updated)
        return _json_ok(payload)
    return web.json_response(AppError("bad_input").as_json(), status=400)


async def api_suggest(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
    except AppError as cop:
        return web.json_response(cop.as_json(), status=cop.http_status)
    query = str(request.query.get("q") or "").strip()
    from app.suggest import clamp_suggest_count, owned_key, suggest_for_user

    count = clamp_suggest_count(request.query.get("n"))
    lastfm = bool((getattr(ctx.settings, "lastfm_api_key", None) or "").strip())
    try:
        rows = await suggest_for_user(ctx, user_id, query, limit=count)
    except Exception as cop:
        err = to_app_error(cop)
        return web.json_response(err.as_json(), status=err.http_status)
    hifi = str(getattr(ctx.settings, "hifi_bot_username", None) or "HiFiAudioBot")
    shown = [_decorate_suggest_row(row, hifi_user=hifi) for row in rows[:count]]
    ctx.catalog.mark_suggest_shown(
        user_id,
        [owned_key(str(row.get("artist") or ""), str(row.get("title") or "")) for row in shown],
        keep=max(200, count),
    )
    return _json_ok({"results": shown, "lastfm": lastfm})


async def api_suggest_art(request: web.Request) -> web.Response:
    try:
        ctx, _user_id = await _api_user(request)
    except AppError as cop:
        return web.json_response(cop.as_json(), status=cop.http_status)
    artist = str(request.query.get("artist") or "").strip()
    title = str(request.query.get("title") or "").strip()
    album = str(request.query.get("album") or "").strip()
    mbid = str(request.query.get("mbid") or "").strip() or None
    if not artist and not title:
        return _json_ok({"covers": [], "apple": ""})
    from app.enrich import list_cover_urls

    http = getattr(ctx, "http", None)
    if http is None:
        return _json_ok({"covers": [], "apple": ""})
    try:
        data = await list_cover_urls(http, artist=artist, title=title, album=album, mbid=mbid)
    except Exception as cop:
        err = to_app_error(cop)
        return web.json_response(err.as_json(), status=err.http_status)
    covers = data.get("covers") if isinstance(data, dict) else None
    apple = data.get("apple") if isinstance(data, dict) else ""
    return _json_ok({"covers": covers if isinstance(covers, list) else [], "apple": apple or ""})


async def api_suggest_cover(request: web.Request) -> web.Response:
    try:
        ctx, _user_id = await _api_user(request)
    except AppError as cop:
        return web.json_response(cop.as_json(), status=cop.http_status)
    from app.enrich import cover_url_allowed, fetch_proxied_cover

    url = str(request.query.get("u") or "").strip()
    if not cover_url_allowed(url):
        return web.Response(status=400)
    http = getattr(ctx, "http", None)
    if http is None:
        return web.Response(status=404)
    try:
        fetched = await fetch_proxied_cover(http, url)
    except Exception:
        log.debug("suggest cover proxy failed", exc_info=True)
        return web.Response(status=404)
    if not fetched:
        return web.Response(status=404)
    body, content_type = fetched
    return web.Response(
        body=body,
        content_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


async def api_login(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
        url = issue_login_ticket(ctx, user_id)
    except AppError as exc:
        return web.json_response(exc.as_json(), status=exc.http_status)
    except Exception:
        log.exception("api login failed")
        return web.json_response(AppError("internal").as_json(), status=500)
    return _json_ok({"url": url})

