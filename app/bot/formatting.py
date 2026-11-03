"""Telegram message formatting helpers (HTML parse mode)."""

from __future__ import annotations

import html

TELEGRAM_MAX_MESSAGE = 4096


def esc(value: object) -> str:
    """Escape any dynamic value before inserting it into an HTML message."""
    return html.escape(str(value), quote=False)


def chunk_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE - 96) -> list[str]:
    """Split long messages on line boundaries to respect Telegram's limit."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks
