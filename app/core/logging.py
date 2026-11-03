"""Logging setup with secret redaction.

python-telegram-bot sends requests to URLs of the form
`https://api.telegram.org/bot<TOKEN>/method`, and httpx logs request URLs at
INFO level. Without redaction the bot token would land in the logs. We both
quieten httpx and apply a redaction filter to every handler.
"""

from __future__ import annotations

import logging
import re
import sys

_TELEGRAM_TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_-]{30,}")
_URL_KEY_PARAM_RE = re.compile(r"([?&]key=)[^&\s]+", re.IGNORECASE)

REDACTED = "[REDACTED]"


def redact(text: str, secrets: list[str] | None = None) -> str:
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _TELEGRAM_TOKEN_RE.sub(REDACTED, text)
    text = _GOOGLE_KEY_RE.sub(REDACTED, text)
    text = _URL_KEY_PARAM_RE.sub(r"\1" + REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        cleaned = redact(message, self._secrets)
        if record.exc_info:
            # Render the traceback now so it can be redacted too.
            formatter = logging.Formatter()
            cleaned += "\n" + redact(formatter.formatException(record.exc_info), self._secrets)
            record.exc_info = None
            record.exc_text = None
        record.msg = cleaned
        record.args = None
        return True


def configure_logging(level: str, secrets: list[str]) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    handler.addFilter(RedactingFilter(secrets))
    root.addHandler(handler)

    # These libraries log full request URLs (incl. the bot token) at INFO.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Uncaught exceptions would otherwise be printed by Python's default hook,
    # bypassing redaction. Route them through the (redacting) logger instead.
    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("app").critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook
