"""Pluggable web search providers.

None is configured in this version: there is no free, terms-compliant web
search API that fits the "no paid services" rule, and scraping search
engines is fragile and against their terms. The Research Agent therefore
works from RSS feeds, watched pages and URLs you send it.

To add a provider later, implement SearchProvider and register it in
get_provider(). Results are *leads only*: the agent must fetch each hit
through the safe fetcher and verify facts against the page before storing
anything, exactly as it does for URLs you send by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str  # untrusted


class SearchProvider(Protocol):
    name: str
    available: bool

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]: ...


class NoSearchProvider:
    name = "none"
    available = False

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        return []


def get_provider() -> SearchProvider:
    return NoSearchProvider()
