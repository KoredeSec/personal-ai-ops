"""RSS/Atom parsing. feedparser is only ever given bytes we fetched ourselves,
never a URL, so it cannot bypass the SSRF-safe fetcher."""

from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from app.integrations.html_text import html_to_text

MAX_ENTRIES = 50
TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|cmpid|ocid|_hsenc|_hsmi)$", re.I)


@dataclass(frozen=True)
class FeedEntry:
    title: str
    url: str
    published_at: datetime | None
    summary: str  # plain text, untrusted


@dataclass(frozen=True)
class ParsedFeed:
    entries: list[FeedEntry]
    title: str
    is_feed: bool  # False if the bytes weren't a recognisable feed


def normalize_url(url: str) -> str:
    """Canonical form for de-duplication: no fragment, no tracking params."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not TRACKING_PARAMS.match(k)))
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host + port, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def title_hash(title: str) -> str:
    norm = " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())
    return hashlib.sha256(norm.encode()).hexdigest()


def _is_http(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")


def parse_feed(content: bytes) -> ParsedFeed:
    d = feedparser.parse(content)
    if not d.version and not d.entries:
        return ParsedFeed([], "", False)
    out = []
    for e in d.entries[:MAX_ENTRIES]:
        link = (e.get("link") or "").strip()
        title = " ".join((e.get("title") or "").split())[:1000]
        if not link or not title or not _is_http(link):  # drops javascript:, data:, mailto: ...
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime.fromtimestamp(calendar.timegm(ts), UTC) if ts else None
        summary = html_to_text(e.get("summary") or e.get("description") or "")[:4000]
        out.append(FeedEntry(title=title, url=link, published_at=published, summary=summary))
    return ParsedFeed(out, " ".join((d.feed.get("title") or "").split())[:200], True)
