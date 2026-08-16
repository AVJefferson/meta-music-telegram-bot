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
from aiogram.types import Message, TelegramObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.catalog import Catalog
from app.cleanup import run_cleanup, run_expire_pending
from app.config import Settings
from app.drive import DriveClient
from app.edit_ui import build_edit_router
from app.genre import GenreMapper
from app.identify import MBClient
from app.models import Ctx, Job
from app.private_ui import build_private_router
from app.queue import recover_interrupted, worker
from app.reactions import build_reactions_router
from app.review_cmd import build_review_command_router
from app.review_ui import build_review_router
from app.util import html_esc

log = logging.getLogger(__name__)

# Telegram's default getUpdates list omits message_reaction (and chat_member,
# message_reaction_count) to save bandwidth. An empty allowed_updates is that
# default — reaction events never arrive unless we name them.
REQUIRED_ALLOWED_UPDATES = ("message", "message_reaction")

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
        return await handler(event, data)


def polling_allowed_updates(dp: Dispatcher) -> list[str]:
    """Update types sent to getUpdates.

    Starts from handlers actually registered (message, callback_query, …) and
    always includes message + message_reaction. Passing only those two would
    drop callback_query and break review/edit buttons.
    """
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
        if message.chat.id != ctx.settings.allowed_chat_id:
            return
        created = message.forum_topic_created
        if created and message.message_thread_id:
            ctx.catalog.upsert_topic(message.message_thread_id, created.name)

    @router.message(F.forum_topic_edited)
    async def topic_edited(message: Message, ctx: Ctx) -> None:
        if message.chat.id != ctx.settings.allowed_chat_id:
            return
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
        status_id = 0
        try:
            status = await message.reply(f"Queued <code>{html_esc(file_name)}</code>…", parse_mode="HTML")
            status_id = status.message_id
        except Exception as exc:
            log.warning("queue reply failed: %s", exc)
        # Recorded before enqueueing so a restart re-drives the job instead of
        # leaving a permanently stale "Queued…" message.
        pending_id = ctx.catalog.insert_pending_review(
            phase="intake",
            status="queued",
            local_path="",
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
            thread_id=thread_id,
            status_message_id=status_id,
            topic_name=topic_name,
            file_name=file_name,
            telegram_file_id=file_id,
            expires_at=intake_expires_at(),
        )
        await jobs.put(
            Job(
                chat_id=message.chat.id,
                thread_id=thread_id,
                topic_name=topic_name,
                file_id=file_id,
                file_name=file_name,
                status_message_id=status_id,
                source_pending_id=pending_id,
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
    settings = Settings()
    setup_logging(settings.log_level)
    for path in (
        settings.library_root,
        settings.review_root,
        settings.pending_root,
        settings.tmp_root,
        settings.covers_root,
        settings.state_db.parent,
    ):
        path.mkdir(parents=True, exist_ok=True)

    catalog = Catalog(settings.state_db)
    drive = DriveClient.from_settings(settings)
    drive.assert_folders(
        {
            "GDRIVE_FOLDER_ID": settings.gdrive_folder_id,
            "GDRIVE_REVIEW_FOLDER_ID": settings.gdrive_review_folder_id,
        }
    )
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
    dp.include_router(build_private_router(jobs))
    dp.include_router(build_review_command_router())
    dp.include_router(build_edit_router())
    dp.include_router(build_reactions_router())
    dp.include_router(build_router(jobs))
    dp.include_router(build_review_router())

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(run_cleanup, CronTrigger.from_crontab(settings.cleanup_cron, timezone="UTC"), args=[ctx])
    scheduler.add_job(run_expire_pending, "interval", minutes=15, args=[ctx])
    scheduler.start()

    worker_task = asyncio.create_task(worker("main", jobs, ctx), name="tagger-worker")
    try:
        await wait_for_telegram(bot)
        await recover_interrupted(ctx, jobs)
        allowed = polling_allowed_updates(dp)
        log.info("polling allowed_updates=%s allowed_chat_id=%s", allowed, settings.allowed_chat_id)
        await dp.start_polling(bot, allowed_updates=allowed)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        scheduler.shutdown(wait=False)
        await http.aclose()
        await bot.session.close()
        catalog.close()


if __name__ == "__main__":
    asyncio.run(main())
