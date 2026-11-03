import logging
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import REDACTED, RedactingFilter, redact
from app.core.timeutils import format_local, local_day_bounds_utc, to_local, to_utc

TOKEN = "123456789:TEST_TOKEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
KEY = "AIzaTEST_KEY_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_user_ids_parse_comma_separated():
    s = Settings(telegram_allowed_user_ids="1, 2,3", _env_file=None)
    assert s.telegram_allowed_user_ids == [1, 2, 3]


def test_user_ids_reject_non_numeric():
    with pytest.raises(ValidationError):
        Settings(telegram_allowed_user_ids="1,abc", _env_file=None)


def test_invalid_timezone_rejected():
    with pytest.raises(ValidationError):
        Settings(timezone="Mars/Olympus", _env_file=None)


def test_secrets_not_in_repr():
    s = Settings(_env_file=None)
    assert TOKEN not in repr(s) and KEY not in repr(s)
    assert set(s.secret_values()) == {TOKEN, KEY}


def test_missing_for_bot():
    s = Settings(_env_file=None, telegram_bot_token="", telegram_allowed_user_ids="")
    assert s.missing_for_bot() == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS"]


def test_redact_token_in_telegram_url_and_key_param():
    line = f"POST https://api.telegram.org/bot{TOKEN}/getMe ?key={KEY}"
    out = redact(line)
    assert TOKEN not in out and KEY not in out and REDACTED in out


def test_redacting_filter_covers_args_and_tracebacks():
    record_out = []

    class Capture(logging.Handler):
        def emit(self, record):
            record_out.append(self.format(record))

    log = logging.getLogger("test.redact")
    handler = Capture()
    handler.addFilter(RedactingFilter([TOKEN, KEY]))
    log.addHandler(handler)
    try:
        log.error("url=%s", f"https://x/bot{TOKEN}/m")
        try:
            raise RuntimeError(f"boom {KEY}")
        except RuntimeError:
            log.exception("failed")
    finally:
        log.removeHandler(handler)
    joined = "\n".join(record_out)
    assert TOKEN not in joined and KEY not in joined
    assert "RuntimeError" in joined  # traceback still present, just redacted


def test_lagos_conversions():
    utc = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
    local = to_local(utc)
    assert (local.day, local.hour) == (4, 0)  # WAT = UTC+1, no DST
    naive_local = datetime(2026, 11, 4, 9, 0)
    assert to_utc(naive_local) == datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    assert format_local(utc) == "Wed 04 Nov 2026, 00:30 WAT"


def test_local_day_bounds():
    start, end = local_day_bounds_utc(date(2026, 11, 4))
    assert start == datetime(2026, 11, 3, 23, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 4, 23, 0, tzinfo=UTC)
