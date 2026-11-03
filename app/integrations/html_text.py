"""Main-text extraction from HTML using only the standard library.

Deliberately simple (trafilatura was deferred, see ENGINEERING_NOTES.md):
  * drops script/style/nav/header/footer/aside/form and similar chrome
  * prefers <article> or <main> when present
  * keeps paragraph/heading/list structure as line breaks
  * collects the page title, meta description and outbound links

Output is still untrusted text; callers must fence it with wrap_untrusted().
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe",
        "template", "button", "select", "canvas", "object", "embed"}
BLOCK = {"p", "div", "section", "article", "main", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5",
         "h6", "tr", "table", "blockquote", "pre", "dd", "dt"}
VOID = {"br", "img", "hr", "meta", "link", "input", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
MAX_INPUT_CHARS = 3_000_000
MAX_LINKS = 60


@dataclass
class Extracted:
    title: str = ""
    description: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute url)


class _Parser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.skip_depth = 0
        self.focus_depth = 0  # inside <article>/<main>
        self.saw_focus = False
        self.all_parts: list[str] = []
        self.focus_parts: list[str] = []
        self.in_title = False
        self.title = ""
        self.description = ""
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP:
            if tag not in VOID:
                self.skip_depth += 1
            return
        if tag in ("article", "main"):
            self.focus_depth += 1
            self.saw_focus = True
        if tag == "title":
            self.in_title = True
        if tag == "meta" and (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
            self.description = self.description or (a.get("content") or "").strip()
        if tag == "a" and a.get("href") and self.skip_depth == 0:
            self._href, self._anchor = a["href"], []
        if tag in BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in ("article", "main"):
            self.focus_depth = max(0, self.focus_depth - 1)
        if tag == "title":
            self.in_title = False
        if tag == "a" and self._href is not None:
            url = urljoin(self.base, self._href.strip())
            if urlsplit(url).scheme in ("http", "https") and len(self.links) < MAX_LINKS:
                self.links.append((" ".join("".join(self._anchor).split())[:120], url))
            self._href = None
        if tag in BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.skip_depth:
            return
        if self._href is not None:
            self._anchor.append(data)
        self._emit(data)

    def _emit(self, s: str):
        self.all_parts.append(s)
        if self.focus_depth:
            self.focus_parts.append(s)


def _clean(text: str) -> str:
    text = html.unescape(text).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if not line:
            if not blank and out:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    return "\n".join(out).strip()


def extract(markup: str, base_url: str = "") -> Extracted:
    p = _Parser(base_url)
    try:
        p.feed(markup[:MAX_INPUT_CHARS])
        p.close()
    except Exception:  # malformed markup: keep whatever was parsed
        logger.debug("HTML parse stopped early", exc_info=True)
    focus = _clean("".join(p.focus_parts)) if p.saw_focus else ""
    body = focus if len(focus) >= 200 else _clean("".join(p.all_parts))
    return Extracted(title=" ".join(p.title.split())[:300], description=p.description[:500], text=body,
                     links=p.links)


def html_to_text(fragment: str) -> str:
    """For short HTML snippets such as RSS summaries."""
    return _clean(re.sub(r"<[^>]+>", " ", fragment or ""))
