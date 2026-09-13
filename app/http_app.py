from __future__ import annotations

import json
import logging
from pathlib import Path

from aiohttp import web

from app.errors import AppError, to_app_error
from app.initdata import parse_init_data
from app.membership import check_api_rate, is_admin, user_is_blocked
from app.models import Ctx
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
    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html_esc(title)}</title></head><body>"
        f"<p>{body}</p></body></html>"
    )
    return web.Response(text=page, content_type="text/html", status=status)


def create_http_app(ctx: Ctx) -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app["ctx"] = ctx
    app.router.add_get("/oauth/start", oauth_start)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/app", webapp_index)
    app.router.add_get("/app/{page}", webapp_index)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/login", api_login)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/review", api_review)
    app.router.add_post("/api/review/{track_id}/action", api_review_action)
    app.router.add_get("/api/suggest", api_suggest)
    if WEBAPP_DIR.is_dir():
        app.router.add_static("/app/static/", WEBAPP_DIR)
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
    return _html("Login", "Google Drive connected. You can close this window.")


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
    user_id = parse_init_data(ctx.settings.bot_token, init_data, max_age=max_age)
    if await user_is_blocked(ctx, user_id) and not is_admin(ctx, user_id):
        raise AppError("blacklisted")
    check_api_rate(ctx, user_id)
    ctx.catalog.touch_user(user_id)
    return ctx, user_id


def _json_ok(data: dict, status: int = 200) -> web.Response:
    payload = {"ok": True, **data}
    return web.json_response(payload, status=status)


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


async def api_me(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
    except AppError as exc:
        return web.json_response(exc.as_json(), status=exc.http_status)
    user = ctx.catalog.ensure_user(user_id)
    months = int(getattr(ctx.settings, "user_inactive_months", 6) or 6)
    settings = {}
    try:
        settings = json.loads(user.settings_json or "{}")
    except (TypeError, ValueError):
        settings = {}
    return _json_ok(
        {
            "user_since": user.first_seen_at,
            "songs_edited": user.songs_edited,
            "google_email": user.google_email,
            "logged_in": user.logged_in,
            "inactive_months": months,
            "default_dest": settings.get("default_dest") or "none",
            "admin": is_admin(ctx, user_id),
        }
    )


async def api_settings(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
        body = await request.json()
    except AppError as exc:
        return web.json_response(exc.as_json(), status=exc.http_status)
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
    dest = str(body.get("default_dest") or "none")
    if dest not in {"library", "review", "none"}:
        dest = "none"
    user = ctx.catalog.ensure_user(user_id)
    try:
        settings = json.loads(user.settings_json or "{}")
    except (TypeError, ValueError):
        settings = {}
    settings["default_dest"] = dest
    ctx.catalog.update_user(user_id, settings_json=json.dumps(settings))
    return _json_ok({"default_dest": dest, "logged_in": user.logged_in, "google_email": user.google_email})


async def api_review(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
    except AppError as exc:
        return web.json_response(exc.as_json(), status=exc.http_status)
    tracks = ctx.catalog.list_review_tracks(user_id)
    return _json_ok(
        {
            "tracks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "artist": t.artist,
                    "album": t.album,
                    "kind": t.kind,
                }
                for t in tracks[:100]
            ]
        }
    )


async def api_review_action(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
        track_id = int(request.match_info["track_id"])
        body = await request.json()
    except AppError as exc:
        return web.json_response(exc.as_json(), status=exc.http_status)
    except Exception:
        return web.json_response(AppError("bad_input").as_json(), status=400)
    body.pop("user_id", None)
    track = ctx.catalog.get_track(track_id)
    if track is None or getattr(track, "user_id", 0) != user_id:
        return web.json_response(AppError("not_found").as_json(), status=404)
    action = str(body.get("action") or "")
    if action == "cancel":
        return _json_ok({"action": "cancel"})
    if action in {"library", "draft"}:
        from app.relocate import identity_from_track, relocate_track, tags_from_track

        kind = "library" if action == "library" else "review"
        try:
            await relocate_track(
                ctx,
                track,
                kind=kind,
                tags=tags_from_track(track),
                identity=identity_from_track(track),
                source_report={},
                topic_name=track.topic_name or "Unknown",
                file_name=track.file_name or "track.flac",
            )
        except AppError as exc:
            return web.json_response(exc.as_json(), status=exc.http_status)
        except Exception as exc:
            err = to_app_error(exc)
            return web.json_response(err.as_json(), status=err.http_status)
        return _json_ok({"action": action})
    return web.json_response(AppError("bad_input").as_json(), status=400)


async def api_suggest(request: web.Request) -> web.Response:
    try:
        ctx, user_id = await _api_user(request)
    except AppError as exc:
        return web.json_response(exc.as_json(), status=exc.http_status)
    query = str(request.query.get("q") or "").strip()
    from app.suggest import suggest_for_user

    try:
        rows = await suggest_for_user(ctx, user_id, query)
    except Exception as exc:
        err = to_app_error(exc)
        return web.json_response(err.as_json(), status=err.http_status)
    return _json_ok({"results": rows[:20]})
