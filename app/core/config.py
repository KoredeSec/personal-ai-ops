"""Application configuration.

All settings come from environment variables (optionally a local `.env` file).
Secrets are typed as `SecretStr` so they never appear in reprs, logs or
tracebacks by accident; call `.get_secret_value()` only at the point of use.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime ---------------------------------------------------------
    app_env: str = Field(default="development", description="development | production | test")
    log_level: str = "INFO"
    timezone: str = "Africa/Lagos"

    # --- Database --------------------------------------------------------
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'app.db'}"
    backup_dir: Path = PROJECT_ROOT / "data" / "backups"
    backup_retention_count: int = Field(default=14, ge=1)

    backup_time_local: str = Field(default="02:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    # --- Telegram --------------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    rate_limit_per_minute: int = Field(default=20, ge=1)
    # Updates processed concurrently, so a slow AI reply never blocks /tasks.
    concurrent_updates: int = Field(default=8, ge=1, le=64)

    # --- Delivery & health ------------------------------------------------
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    # A claim older than this is treated as a crashed send and reclaimed.
    delivery_lease_seconds: int = Field(default=300, ge=30)
    heartbeat_file: Path = PROJECT_ROOT / "data" / "heartbeat"
    heartbeat_interval_seconds: int = Field(default=60, ge=10)
    heartbeat_max_age_seconds: int = Field(default=300, ge=30)

    # --- Gemini ----------------------------------------------------------
    gemini_api_key: SecretStr | None = None
    # Primary model for reasoning, briefings and summaries.
    gemini_model: str = "gemini-2.5-flash"
    # Cheaper/faster model for classification and intent routing.
    gemini_model_light: str = "gemini-2.5-flash-lite"
    gemini_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    gemini_max_retries: int = Field(default=4, ge=0, le=10)
    # Max total seconds (incl. retries) a user waits on an interactive reply.
    gemini_interactive_deadline_seconds: float = Field(default=20.0, gt=0, le=300)
    # Includes "thinking" tokens on thinking models, so keep generous.
    gemini_max_output_tokens: int = Field(default=4096, ge=64)
    # Optional thinking budget for models that support it (0 disables
    # thinking on 2.5 Flash; unset = model default).
    gemini_thinking_budget: int | None = Field(default=None, ge=0)
    # Hard cap on Gemini calls per (Lagos) day, protects free-tier quota.
    gemini_daily_request_budget: int = Field(default=200, ge=1)
    # Hard cap on characters sent in a single prompt (history + content).
    gemini_max_prompt_chars: int = Field(default=30_000, ge=1_000)
    llm_cache_ttl_hours: int = Field(default=24, ge=0)

    # --- External content fetching (Milestone 3) -------------------------
    fetch_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    fetch_max_bytes: int = Field(default=2_000_000, ge=10_000)

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def _parse_user_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
            try:
                return [int(p) for p in parts]
            except ValueError as exc:
                raise ValueError(
                    "TELEGRAM_ALLOWED_USER_IDS must be comma-separated numeric Telegram user IDs"
                ) from exc
        if isinstance(value, list | tuple):
            return [int(v) for v in value]
        raise ValueError("Invalid TELEGRAM_ALLOWED_USER_IDS")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid LOG_LEVEL: {value}")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def secret_values(self) -> list[str]:
        """Secret strings that must be redacted from all log output."""
        out = []
        for secret in (self.telegram_bot_token, self.gemini_api_key):
            if secret is not None and secret.get_secret_value():
                out.append(secret.get_secret_value())
        return out

    def missing_for_bot(self) -> list[str]:
        """Names of settings required to run the Telegram bot that are unset."""
        missing = []
        if not self.telegram_bot_token or not self.telegram_bot_token.get_secret_value():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_allowed_user_ids:
            missing.append("TELEGRAM_ALLOWED_USER_IDS")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
