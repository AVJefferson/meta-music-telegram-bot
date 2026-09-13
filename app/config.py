from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    admin_telegram_user_id: int
    telegram_api_base: str = "http://telegram-bot-api:8081"
    telegram_api_id: int = 0
    telegram_api_hash: str = ""

    log_level: str = "info"
    enable_log_per_music_file: bool = False
    cover_choice_hold_seconds: float = Field(default=2.0, ge=0)
    authenticity_check: bool = True
    authenticity_sample_seconds: float = 15.0
    authenticity_flag_hires: bool = True

    acoustid_api_key: str
    acoustid_min_score: float = 0.8
    musicbrainz_user_agent: str = "telegram-music-bot/1.0 (unknown@example.com)"
    lastfm_api_key: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""

    public_base_url: str = ""
    oauth_http_port: int = 8080
    user_inactive_months: int = 3
    dm_requires_known_chat: bool = True
    hifi_bot_username: str = "HiFiAudioBot"
    hifi_session_path: Path = Path("/data/hifi.session")
    hifi_timeout_seconds: int = 90
    hifi_queue_max: int = 8
    rate_search_per_minute: int = 8
    rate_upload_per_minute: int = 12
    rate_api_per_minute: int = 60
    oauth_ticket_ttl_seconds: int = 600
    initdata_max_age_seconds: int = 300

    library_root: Path = Path("/data/library")
    review_root: Path = Path("/data/review")
    pending_root: Path = Path("/data/pending")
    state_db: Path = Path("/data/state.sqlite")
    tmp_root: Path = Path("/data/tmp")
    covers_root: Path = Path("/data/covers")
    cache_root: Path = Path("/data/cache")
    cleanup_cron: str = "0 3 * * *"
    genre_map_path: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent / "genre_map.yaml")

    # Optional leftovers for tests / local helpers. Not required at boot.
    allowed_chat_id: int = 0
    alert_thread_id: int = 1
    google_refresh_token: str = ""
    gdrive_folder_id: str = ""
    gdrive_review_folder_id: str = ""

    @field_validator("authenticity_sample_seconds", mode="before")
    @classmethod
    def _normalize_auth_sample_seconds(cls, value: object) -> float:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return 15.0
        if seconds == -1:
            return -1.0
        if seconds >= 1:
            return seconds
        return 15.0

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> str:
        name = str(value or "info").strip().lower()
        if name not in {"debug", "info", "error"}:
            return "info"
        return name

    @field_validator("public_base_url", mode="before")
    @classmethod
    def _strip_base_url(cls, value: object) -> str:
        return str(value or "").strip().rstrip("/")
