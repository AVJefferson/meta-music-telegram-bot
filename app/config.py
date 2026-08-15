from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    allowed_chat_id: int
    alert_thread_id: int = 1
    telegram_api_base: str = "http://telegram-bot-api:8081"

    acoustid_api_key: str
    acoustid_min_score: float = 0.8
    musicbrainz_user_agent: str = "telegram-music-bot/1.0 (unknown@example.com)"
    lastfm_api_key: str = ""

    google_service_account_json: str
    gdrive_folder_id: str
    gdrive_review_folder_id: str

    library_root: Path = Path("/data/library")
    review_root: Path = Path("/data/review")
    state_db: Path = Path("/data/state.sqlite")
    tmp_root: Path = Path("/data/tmp")
    cleanup_cron: str = "0 3 * * 0"
    genre_map_path: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent / "genre_map.yaml")
