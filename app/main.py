"""Entrypoint: `python -m app.main`."""

from __future__ import annotations

import logging
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging

logger = logging.getLogger("app")


def main() -> int:
    try:
        settings = get_settings()
    except Exception as exc:  # pydantic ValidationError lists the bad variables
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.secret_values())

    missing = settings.missing_for_bot()
    if missing:
        logger.error("Missing required configuration: %s (see .env.example)", ", ".join(missing))
        return 2
    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY not set: AI features disabled, deterministic features still work")

    from app.database.migrate import schema_status

    current, head = schema_status()
    if current != head:
        logger.error("Database schema is at %s but code expects %s. Run: alembic upgrade head", current, head)
        return 3

    from telegram.error import InvalidToken, NetworkError

    from app.bot.application import ALLOWED_UPDATES, build_application
    from app.services.heartbeat import start_watchdog

    application = build_application(settings)
    start_watchdog(settings.heartbeat_file, settings.heartbeat_max_age_seconds)
    logger.info("Starting bot (timezone=%s, model=%s)", settings.timezone, settings.gemini_model)
    try:
        # bootstrap_retries: ride out short network blips at startup before giving up.
        application.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=False,
                                bootstrap_retries=5)
    except InvalidToken:
        logger.error("Telegram rejected TELEGRAM_BOT_TOKEN. Check the token from @BotFather.")
        return 2
    except NetworkError as exc:
        logger.error("Cannot reach Telegram (%s). Check network/proxy; will exit for restart.", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
