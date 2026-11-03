"""The only way this system reads anything from the internet.

Guarantees, in order:
  1. Every URL (including every redirect hop) passes validate_public_url():
     http/https only, standard ports, no credentials, all resolved IPs public.
  2. After connecting, the peer address is checked again, which defeats DNS
     rebinding (a hostname that resolved public at check time but private at
     connect time). Skipped only when an explicit proxy does the connecting.
  3. Redirects are followed manually (max 5) so each hop is re-validated.
  4. Bodies are streamed and capped at FETCH_MAX_BYTES *after* decompression,
     so a small gzip bomb cannot exhaust memory.
  5. Only expected content types are accepted.
  6. Timeouts on connect/read; a generic, honest User-Agent.

Fetched content is untrusted data. Nothing here interprets it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app.core.config import Settings
from app.core.security import Resolver, UnsafeURLError, _default_resolver, is_public_ip, validate_public_url

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5
USER_AGENT = "personal-ai-ops/0.1 (+https://github.com/KoredeSec/personal-ai-ops)"
ALLOWED_TYPES = (
    "text/html", "application/xhtml+xml", "application/rss+xml", "application/atom+xml",
    "application/xml", "text/xml", "application/rdf+xml", "application/feed+json",
    "application/json", "text/plain",
)
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.1"


class FetchError(Exception):
    """Safe-to-display reason the fetch failed (no internal details)."""


@dataclass(frozen=True)
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content: bytes
    content_type: str
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def text(self) -> str:
        charset = "utf-8"
        if "charset=" in self.content_type:
            charset = self.content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def _proxied() -> bool:
    return any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"))


def _peer_ip(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    try:
        addr = stream.get_extra_info("server_addr")
    except Exception:
        return None
    return addr[0] if addr else None


def build_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    timeout = httpx.Timeout(settings.fetch_timeout_seconds, connect=min(10.0, settings.fetch_timeout_seconds))
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=transport,
                             headers={"User-Agent": USER_AGENT, "Accept": ACCEPT})


async def fetch(url: str, settings: Settings, *, etag: str | None = None, last_modified: str | None = None,
                client: httpx.AsyncClient | None = None, resolver: Resolver = _default_resolver) -> FetchResult:
    own_client = client is None
    client = client or build_client(settings)
    try:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                validate_public_url(current, resolver)
            except UnsafeURLError as exc:
                raise FetchError(f"Blocked URL: {exc}") from exc
            headers = {}
            if etag and current == url:
                headers["If-None-Match"] = etag
            if last_modified and current == url:
                headers["If-Modified-Since"] = last_modified
            try:
                async with client.stream("GET", current, headers=headers) as resp:
                    peer = _peer_ip(resp)
                    if peer and not _proxied() and not is_public_ip(peer):
                        raise FetchError("Blocked URL: connected to a non-public address")
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError(f"Redirect without location (HTTP {resp.status_code})")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code == 304:
                        return FetchResult(current, 304, b"", resp.headers.get("content-type", ""),
                                           resp.headers.get("etag"), resp.headers.get("last-modified"))
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").lower()
                    if ctype and not ctype.split(";")[0].strip().startswith(ALLOWED_TYPES):
                        raise FetchError(f"Unsupported content type: {ctype.split(';')[0][:60]}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > settings.fetch_max_bytes:
                        raise FetchError("Response too large")
                    chunks, total = [], 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > settings.fetch_max_bytes:
                            raise FetchError("Response too large")
                        chunks.append(chunk)
                    return FetchResult(current, resp.status_code, b"".join(chunks), ctype,
                                       resp.headers.get("etag"), resp.headers.get("last-modified"))
            except httpx.TimeoutException as exc:
                raise FetchError("Timed out") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"Network error: {type(exc).__name__}") from exc
        raise FetchError("Too many redirects")
    finally:
        if own_client:
            await client.aclose()
