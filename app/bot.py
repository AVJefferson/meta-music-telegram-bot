from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ChatMemberUpdated, ErrorEvent, Message, TelegramObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.admin_cmd import build_admin_router
from app.catalog import Catalog
from app.cleanup import run_cleanup, run_expire_pending
from app.config import Settings
from app.drive import DriveHub
from app.edit_ui import build_edit_router
from app.errors import AppError, log_error, to_app_error
from app.genre import GenreMapper
from app.hifi import build_hifi_router
from app.http_app import start_http
from app.identify import MBClient
from app.library_index import ensure_library_index
from app.membership import allow_user, ignore_bot_update, message_from_bot, touch
from app.models import Ctx, Job
from app.notify import notify_user
from app.private_ui import build_private_router
from app.queue import recover_interrupted, worker
from app.reactions import build_reactions_router
from app.review_cmd import build_review_command_router
from app.review_ui import build_review_router
from app.suggest_cmd import build_suggest_command_router
from app.user_cmd import build_search_text_router, build_user_command_router

log = logging.getLogger(__name__)

REQUIRED_ALLOWED_UPDATES = ("message", "message_reaction", "my_chat_member", "channel_post")

NOISY_LOGGERS = (
    "musicbrainzngs",
    "httpx",
    "httpcore",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.http",
    "apscheduler",
    "aiogram.event",
    "urllib3",
    "telethon",
)


def setup_logging(level_name: str) -> None:
    mapping = {"debug": logging.DEBUG, "info": logging.INFO, "error": logging.ERROR}
    level = mapping.get((level_name or "info").strip().lower(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    if level > logging.DEBUG:
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("app").setLevel(level)


class CtxMiddleware(BaseMiddleware):
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["ctx"] = self.ctx
        if ignore_bot_update(event, getattr(self.ctx, "bot", None)):
            log.debug("ignored bot-originated update")
            return None
        return await handler(event, data)


def polling_allowed_updates(dp: Dispatcher) -> list[str]:
    return sorted(set(dp.resolve_used_update_types()) | set(REQUIRED_ALLOWED_UPDATES))


def intake_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")


def is_flac_message(message: Message) -> bool:
    doc = message.document
    if doc:
        name = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        return name.endswith(".flac") or "flac" in mime
    audio = message.audio
    if audio:
        name = (audio.file_name or "").lower()
        mime = (audio.mime_type or "").lower()
        return name.endswith(".flac") or "flac" in mime
    return False


def file_info(message: Message) -> tuple[str, str]:
    if message.document:
        return message.document.file_id, message.document.file_name or "track.flac"
    assert message.audio is not None
    return message.audio.file_id, message.audio.file_name or "track.flac"


def resolve_topic(message: Message, ctx: Ctx) -> tuple[int | None, str]:
    thread_id = message.message_thread_id
    if not message.is_topic_message:
        return thread_id, "General"
    if thread_id == 1:
        return 1, ctx.catalog.get_topic(1) or "General"
    cached = ctx.catalog.get_topic(thread_id) if thread_id else None
    if cached:
        return thread_id, cached
    reply = message.reply_to_message
    if reply and reply.forum_topic_created:
        name = reply.forum_topic_created.name
        if thread_id:
            ctx.catalog.upsert_topic(thread_id, name)
        return thread_id, name
    return thread_id, f"Topic {thread_id}"


def build_router(jobs: asyncio.Queue[Job]) -> Router:
    router = Router()

    @router.message(Command("chatid"))
    async def chatid(message: Message) -> None:
        await message.reply(
            f"chat_id=<code>{message.chat.id}</code>\n"
            f"thread_id=<code>{message.message_thread_id}</code>",
            parse_mode="HTML",
        )

    @router.my_chat_member()
    async def on_my_chat_member(event: ChatMemberUpdated, ctx: Ctx) -> None:
        chat = event.chat
        status = str(getattr(event.new_chat_member.status, "value", event.new_chat_member.status))
        active = status in {"member", "administrator", "creator"}
        ctx.catalog.upsert_chat(
            chat.id,
            type=chat.type or "",
            title=chat.title or getattr(chat, "full_name", None) or "",
            active=active,
        )

    @router.message(F.forum_topic_created)
    async def topic_created(message: Message, ctx: Ctx) -> None:
        if not await allow_user(ctx, message.from_user.id if message.from_user else None, message.chat):
            return
        created = message.forum_topic_created
        if created and message.message_thread_id:
            ctx.catalog.upsert_topic(message.message_thread_id, created.name)

    @router.message(F.forum_topic_edited)
    async def topic_edited(message: Message, ctx: Ctx) -> None:
        if not await allow_user(ctx, message.from_user.id if message.from_user else None, message.chat):
            return
        edited = message.forum_topic_edited
        if edited and edited.name and message.message_thread_id:
            ctx.catalog.upsert_topic(message.message_thread_id, edited.name)

    async def _intake_flac(message: Message, ctx: Ctx) -> None:
        if not is_flac_message(message):
            return
        if message_from_bot(message, ctx.bot) or not message.from_user:
            log.debug("ignored bot or anonymous FLAC chat=%s", message.chat.id)
            return
        if not await allow_user(ctx, message.from_user.id, message.chat):
            return
        touch(ctx, message.from_user.id)
        file_id, file_name = file_info(message)
        thread_id, _topic_name = resolve_topic(message, ctx)
        from app.botapi import discard_download
        from app.intake import ingest_local_flac

        pending_dir = ctx.settings.pending_root / f"{message.chat.id}-{message.message_id}"
        pending_dir.mkdir(parents=True, exist_ok=True)
        from pathlib import Path

        from app.util import sanitize_filename

        local = pending_dir / (sanitize_filename(Path(file_name).stem) + ".flac")
        try:
            telegram_file = await ctx.bot.get_file(file_id)
            await ctx.bot.download(telegram_file, destination=local)
            await asyncio.to_thread(discard_download, telegram_file.file_path)
            await ingest_local_flac(
                ctx,
                chat=message.chat,
                user_id=message.from_user.id,
                path=local,
                file_name=file_name,
                thread_id=thread_id,
                topic_name="",
                telegram_file_id=file_id,
                source_message_id=message.message_id,
            )
        except AppError as exc:
            await notify_user(ctx, message.from_user.id, exc.user_message, chat_id=message.chat.id)
        except Exception as exc:
            log.exception("group FLAC ingest failed")
            await notify_user(ctx, message.from_user.id, to_app_error(exc).user_message, chat_id=message.chat.id)

    @router.message(F.document | F.audio)
    async def on_media(message: Message, ctx: Ctx) -> None:
        if message.chat.type == "private":
            return
        await _intake_flac(message, ctx)

    @router.channel_post(F.document | F.audio)
    async def on_channel_media(message: Message, ctx: Ctx) -> None:
        await _intake_flac(message, ctx)

    return router


async def wait_for_telegram(bot: Bot) -> None:
    last: Exception | None = None
    for attempt in range(60):
        try:
            me = await bot.get_me()
            log.info("logged in as @%s", me.username)
            return
        except Exception as exc:
            last = exc
            log.info("waiting for local Bot API (%s/60): %s", attempt + 1, exc)
            await asyncio.sleep(2)
    raise RuntimeError(f"telegram-bot-api not ready: {last}")


async def _warm_library_index(ctx: Ctx) -> None:
    log.info("library tag index: starting in background")
    try:
        source = await asyncio.to_thread(ensure_library_index, ctx)
        log.info("library tag index ready source=%s", source)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("library tag index startup failed", exc_info=True)


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)
    for path in (
        settings.library_root,
        settings.review_root,
        settings.pending_root,
        settings.tmp_root,
        settings.covers_root,
        settings.cache_root,
        settings.state_db.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)

    catalog = Catalog(settings.state_db)
    drive = DriveHub(settings, catalog)
    genre = GenreMapper(settings.genre_map_path)
    mb = MBClient(settings.musicbrainz_user_agent)
    http = httpx.AsyncClient(
        headers={"User-Agent": settings.musicbrainz_user_agent},
        follow_redirects=True,
        timeout=30.0,
    )

    api = TelegramAPIServer.from_base(settings.telegram_api_base.rstrip("/"), is_local=True)
    session = AiohttpSession(api=api, timeout=3600)
    bot = Bot(token=settings.bot_token, session=session)
    jobs: asyncio.Queue[Job] = asyncio.Queue()
    ctx = Ctx(
        settings=settings,
        catalog=catalog,
        drive=drive,
        http=http,
        genre=genre,
        bot=bot,
        mb=mb,
        jobs=jobs,
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(CtxMiddleware(ctx))

    @dp.errors()
    async def on_error(event: ErrorEvent, ctx: Ctx) -> None:
        exc = event.exception
        if isinstance(exc, asyncio.CancelledError):
            raise exc
        err = to_app_error(exc)
        user_id = None
        chat_id = None
        update = event.update
        if update.message and update.message.from_user:
            user_id = update.message.from_user.id
            chat_id = update.message.chat.id
        elif update.callback_query and update.callback_query.from_user:
            user_id = update.callback_query.from_user.id
            if update.callback_query.message:
                chat_id = update.callback_query.message.chat.id
        log_error(err.code, user_id=user_id, chat_id=chat_id, exc=exc)
        if user_id:
            await notify_user(ctx, user_id, err.user_message, chat_id=chat_id)

    dp.include_router(build_admin_router())
    dp.include_router(build_user_command_router())
    dp.include_router(build_hifi_router())
    dp.include_router(build_private_router(jobs))
    dp.include_router(build_review_command_router())
    dp.include_router(build_suggest_command_router())
    dp.include_router(build_edit_router())
    dp.include_router(build_reactions_router())
    dp.include_router(build_search_text_router())
    dp.include_router(build_router(jobs))
    dp.include_router(build_review_router())

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_cleanup, CronTrigger.from_crontab(settings.cleanup_cron, timezone="UTC"), args=[ctx])
    scheduler.add_job(run_expire_pending, "interval", minutes=15, args=[ctx])
    scheduler.start()

    worker_task = asyncio.create_task(worker("main", jobs, ctx), name="tagger-worker")
    index_task: asyncio.Task | None = None
    http_runner = None
    try:
        await wait_for_telegram(bot)
        http_runner = await start_http(ctx)
        await recover_interrupted(ctx, jobs)
        allowed = polling_allowed_updates(dp)
        log.info("polling allowed_updates=%s", allowed)
        index_task = asyncio.create_task(_warm_library_index(ctx), name="library-index")
        await dp.start_polling(bot, allowed_updates=allowed)
    finally:
        if http_runner:
            await http_runner.cleanup()
        if index_task:
            index_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await index_task
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        scheduler.shutdown(wait=False)
        await http.aclose()
        await bot.session.close()
        catalog.close()


if __name__ == "__main__":
    asyncio.run(main())
