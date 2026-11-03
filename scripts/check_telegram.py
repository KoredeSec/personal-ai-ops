"""Live Telegram connectivity check (run on a machine that can reach api.telegram.org).

    python -m scripts.check_telegram            # validates token (getMe)
    python -m scripts.check_telegram --send     # also DMs each allowed user

Note: you must have pressed Start in your bot's chat first, or Telegram will
refuse to let the bot message you.
"""

import argparse
import asyncio
import sys

from telegram import Bot
from telegram.error import TelegramError

from app.core.config import get_settings
from app.core.timeutils import format_local, utcnow


async def run(send: bool) -> int:
    s = get_settings()
    missing = s.missing_for_bot()
    if missing:
        print(f"Missing: {', '.join(missing)}")
        return 2
    bot = Bot(s.telegram_bot_token.get_secret_value())
    try:
        async with bot:
            me = await bot.get_me()
            print(f"OK: token valid, bot is @{me.username} (id {me.id})")
            if send:
                for uid in s.telegram_allowed_user_ids:
                    await bot.send_message(uid, f"Connectivity test from personal-ai-ops · {format_local(utcnow())}")
                    print(f"OK: test message sent to user {uid}")
    except TelegramError as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true")
    sys.exit(asyncio.run(run(p.parse_args().send)))
