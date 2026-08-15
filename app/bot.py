from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.catalog import Catalog
from app.cleanup import run_cleanup
from app.config import Settings
from app.drive import DriveClient
from app.genre import GenreMapper
from app.identify import MBClient
from app.models import Ctx, Job
from app.queue import worker
from app.util import html_esc

log = logging.getLogger(__name__)


class CtxMiddleware(BaseMiddleware):
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx

    async def __call__(self, handler, event: TelegramObject, data: dict):
        data["ctx"] = self.ctx
        return await handler(event, data)


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

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.reply("Send FLAC files in the configured forum group. I tag, upload to Drive, and file them.")

    @router.message(Command("chatid"))
    async def chatid(message: Message) -> None:
        await message.reply(
            f"chat_id=<code>{message.chat.id}</code>\n"
            f"thread_id=<code>{message.message_thread_id}</code>",
            parse_mode="HTML",
        )

    @router.message(F.forum_topic_created)
    async def topic_created(message: Message, ctx: Ctx) -> None:
        created = message.forum_topic_created
        if created and message.message_thread_id:
            ctx.catalog.upsert_topic(message.message_thread_id, created.name)

    @router.message(F.forum_topic_edited)
    async def topic_edited(message: Message, ctx: Ctx) -> None:
        edited = message.forum_topic_edited
        if edited and edited.name and message.message_thread_id:
            ctx.catalog.upsert_topic(message.message_thread_id, edited.name)

    @router.message(F.document | F.audio)
    async def on_media(message: Message, ctx: Ctx) -> None:
        if not is_flac_message(message):
            return
        if message.chat.id != ctx.settings.allowed_chat_id:
            log.info("ignored FLAC from chat_id=%s", message.chat.id)
            return
        file_id, file_name = file_info(message)
        thread_id, topic_name = resolve_topic(message, ctx)
        status = await message.reply(f"Queued <code>{html_esc(file_name)}</code>…", parse_mode="HTML")
        await jobs.put(
            Job(
                chat_id=message.chat.id,
                thread_id=thread_id,
                topic_name=topic_name,
                file_id=file_id,
                file_name=file_name,
                status_message_id=status.message_id,
            )
        )

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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    for path in (
        settings.library_root,
        settings.review_root,
        settings.tmp_root,
        settings.state_db.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)

    catalog = Catalog(settings.state_db)
    drive = DriveClient(settings.google_service_account_json)
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
    ctx = Ctx(settings=settings, catalog=catalog, drive=drive, http=http, genre=genre, bot=bot, mb=mb)

    dp = Dispatcher()
    dp.update.middleware(CtxMiddleware(ctx))
    dp.include_router(build_router(jobs))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_cleanup, CronTrigger.from_crontab(settings.cleanup_cron, timezone="UTC"), args=[ctx])
    scheduler.start()

    worker_task = asyncio.create_task(worker("main", jobs, ctx), name="tagger-worker")
    try:
        await wait_for_telegram(bot)
        log.info("polling allowed_chat_id=%s", settings.allowed_chat_id)
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        scheduler.shutdown(wait=False)
        await http.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
