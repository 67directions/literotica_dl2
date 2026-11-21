from __future__ import annotations

import html
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from literotica_dl2.utils import get_url_from_literotica, parse_story_url

# Literotica's CSS class names are auto-generated and change whenever the site is redeployed,
# so this module avoids depending on class names. A series page has the series title in its
# <h1> and its parts in a list (the "Table of Contents") of links to /s/<story>.
STORY_HREF = re.compile(r"(?:^|literotica\.com)/s/[^/?#]+")

# Series whose parts are already known, filled in by Author from the site's data feed:
#   series id -> {"title": "...", "stories": ["story-stub", ...]}
# A series listed here is not fetched from its page at all. (Series pages cannot always be
# reached by their numeric id, so using the parts the feed gives is more reliable.)
KNOWN_SERIES: dict = {}


def _soup(markup: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(markup, features="html5lib")
    except Exception:  # html5lib not installed
        return BeautifulSoup(markup, features="html.parser")


def _story_hrefs(element) -> list[str]:
    """Unique story links inside element, in document order."""
    found: list[str] = []
    for a in element.find_all("a", href=True):
        if STORY_HREF.search(a["href"]) and a["href"] not in found:
            found.append(a["href"])
    return found


class StorySeries:
    def __init__(self, series_id: str) -> None:
        self.series_id = series_id
        self._stories = None
        self._title = None
        self.url = f"https://www.literotica.com/series/se/{series_id}"
        self.page = None

    def parse(self) -> None:
        known = KNOWN_SERIES.get(str(self.series_id))
        if known:
            self._stories = list(known["stories"])
            self._title = known["title"] or str(self.series_id)
            return

        if self.page is None:
            self.page = _soup(get_url_from_literotica(self.url))

        # The parts: the list (ul/ol) that holds the most story links, which is the table of
        # contents. If there is no such list, fall back to every story link on the page.
        best: list[str] = []
        for lst in self.page.find_all(["ul", "ol"]):
            hrefs = _story_hrefs(lst)
            if len(hrefs) > len(best):
                best = hrefs
        if not best:
            best = _story_hrefs(self.page)

        self._stories = []
        for href in best:
            # an absolute link, the same form the author page gives
            stub = parse_story_url(urljoin(self.url, href))
            if stub is not None and stub not in self._stories:
                self._stories.append(stub)

        # Title: the page heading, else the og:title/<title> without the site suffix
        heading = self.page.find("h1")
        title = heading.get_text(strip=True) if heading is not None else ""
        if not title:
            meta = self.page.find("meta", attrs={"property": "og:title"})
            raw = meta.get("content") if meta is not None and meta.get("content") else None
            if raw is None:
                tag = self.page.find("title")
                raw = tag.get_text() if tag is not None else ""
            raw = html.unescape(raw)
            title = re.sub(r"\s*-\s*Story Series\s*-\s*Literotica\.com\s*$", "", raw, flags=re.I).strip()
        self._title = title or self.series_id

    @property
    def stories(self):
        if self._stories is None:
            self.parse()
        return self._stories

    @property
    def title(self) -> str:
        if self._title is None:
            self.parse()
        return self._title
