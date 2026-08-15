from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    allowed_chat_id: int
    alert_thread_id: int = 1
    telegram_api_base: str = "http://telegram-bot-api:8081"

    log_level: str = "info"
    enable_log_per_music_file: bool = False

    acoustid_api_key: str
    acoustid_min_score: float = 0.8
    musicbrainz_user_agent: str = "telegram-music-bot/1.0 (unknown@example.com)"
    lastfm_api_key: str = ""

    # Personal Drive (OAuth). Files use YOUR quota. Service accounts are not
    # supported: they have 0 My Drive quota.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    gdrive_folder_id: str
    gdrive_review_folder_id: str

    library_root: Path = Path("/data/library")
    review_root: Path = Path("/data/review")
    pending_root: Path = Path("/data/pending")
    state_db: Path = Path("/data/state.sqlite")
    tmp_root: Path = Path("/data/tmp")
    covers_root: Path = Path("/data/covers")
    cleanup_cron: str = "0 3 * * 0"
    genre_map_path: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent / "genre_map.yaml")

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> str:
        name = str(value or "info").strip().lower()
        if name not in {"debug", "info", "error"}:
            return "info"
        return name
