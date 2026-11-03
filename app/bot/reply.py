"""The single way handlers send replies.

* `reply_html` - caller supplies HTML whose dynamic parts were passed through
  `esc()`. Long messages are chunked to respect Telegram's 4096-char limit.
* `reply_text` - plain text; escaped here, so it is always safe.
* `typing`     - shows "typing..." while a slow operation (AI call) runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from app.bot.formatting import chunk_message, esc

logger = logging.getLogger(__name__)


async def reply_html(update: Update, html: str, **kwargs) -> None:
    msg = update.effective_message
    if msg is None:
        return
    parts = chunk_message(html)
    for i, part in enumerate(parts):
        extra = kwargs if i == len(parts) - 1 else {}  # buttons only on the last chunk
        await msg.reply_text(part, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **extra)


async def reply_text(update: Update, text: str, **kwargs) -> None:
    await reply_html(update, esc(text), **kwargs)


@contextlib.asynccontextmanager
async def typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> AsyncIterator[None]:
    """Telegram shows a chat action for ~5s, so refresh it every 4s."""
    chat = update.effective_chat
    if chat is None:
        yield
        return

    async def _loop() -> None:
        while True:
            try:
                await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
            except Exception:  # cosmetic only; never fail the request
                logger.debug("send_chat_action failed", exc_info=True)
            await asyncio.sleep(4)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
