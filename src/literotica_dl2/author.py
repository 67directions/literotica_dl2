from __future__ import annotations

import json
import re
import time
from logging import getLogger
from urllib.parse import quote

from bs4 import BeautifulSoup

from literotica_dl2.story_series import KNOWN_SERIES
from literotica_dl2.utils import (
    get_url_from_literotica,
    parse_story_series,
    parse_story_url,
)

log = getLogger("literotica.author")

# Literotica's CSS class names are auto-generated (e.g. "_header_title_15t0y_78") and change
# whenever the site is redeployed, so this module avoids depending on exact class names.
#
# The author's page only shows the first 50 works; the rest are loaded by its "Show more"
# button from a data feed. This module reads that feed (JSON, 50 works per page, with every
# series' parts included). If the feed cannot be read it falls back to parsing the page,
# where a series group holds a /series/se/ link followed by its parts, and a standalone story
# group holds a single /s/ link.
API_URL = "https://literotica.com/api/3/users/{slug}/series_and_works?params={params}"
API_PAGE_SIZE = 50
MAX_API_PAGES = 200  # safety stop
API_PAUSE = 1.0      # seconds between feed pages

STORY_HREF = re.compile(r"(?:^|literotica\.com)/s/[^/?#]+")
SERIES_HREF = re.compile(r"(?:^|literotica\.com)/series/se/[^/?#]+")
POEM_HREF = re.compile(r"(?:^|literotica\.com)/p/[^/?#]+")


def _soup(markup) -> BeautifulSoup:
    try:
        return BeautifulSoup(markup, "html5lib")
    except Exception:  # html5lib not installed
        return BeautifulSoup(markup, "html.parser")


def _class_has(tag, fragment: str) -> bool:
    return any(fragment in c for c in (tag.get("class") or []))


def _hrefs(element) -> list[str]:
    """Unique link targets inside element, in document order."""
    seen: list[str] = []
    for a in element.find_all("a", href=True):
        if a["href"] not in seen:
            seen.append(a["href"])
    return seen


def _stub(href: str, marker: str) -> str:
    return href.split(marker, 1)[1].split("?")[0].split("#")[0].strip("/")


def _work_container(soup, patterns):
    """The role="list" element that holds the work links, or None if there isn't one."""
    for el in soup.find_all(attrs={"role": "list"}):
        for href in _hrefs(el):
            if any(p.search(href) for p in patterns):
                return el
    return None


def _work_details(work: dict, series_title) -> dict:
    """The facts the feed gives about one story, kept for stats and organising."""
    category = work.get("category_info") or {}
    return {
        "title": work.get("title") or "",
        "category": category.get("name") or "",  # standalone stories carry no name; filled in later
        "category_id": work.get("category"),
        "category_page": category.get("pageUrl") or "",
        "words": work.get("words_count") or 0,
        "date": work.get("date_approve") or "",
        "rating": work.get("rate_all"),
        "views": work.get("view_count"),
        "favorites": work.get("favorite_count"),
        "tags": [t.get("tag") for t in (work.get("tags") or []) if isinstance(t, dict) and t.get("tag")],
        "description": work.get("description") or "",
        "series": series_title,
    }


class Author:
    def __init__(self, author_slug: str):
        self.slug: str = author_slug
        self.stories_page = None
        self.poems_page = None
        self.stories_url: str = f"https://www.literotica.com/authors/{author_slug}/works/stories/all"
        # Some authors' /all address redirects here, and the downloader refuses redirected pages,
        # so this plain address is tried when /all gives nothing.
        self.stories_url_plain: str = f"https://www.literotica.com/authors/{author_slug}/works/stories"
        self.poems_url: str = f"https://www.literotica.com/authors/{author_slug}/works/poetry"
        self._authorname: str = None
        self._series: list[str] = None
        self._individual_works: list[str] = None
        self._poems: list[str] = None
        self._parsed: bool = False
        self.total_stories: int | None = None  # how many stories the site says this author has
        # filled in when the feed is read: story stub -> details, series id -> {"title", "stories"}
        self.works: dict = {}
        self.series_info: dict = {}

    # ----- poems -------------------------------------------------------------------------

    def _fetch_and_parse_alternates(self) -> None:
        if self.poems_page is not None:
            return
        try:
            self.poems_page = _soup(get_url_from_literotica(self.poems_url))
        except Exception:
            log.exception("Could not fetch the poems page: %s", self.poems_url)
            self.poems_page = _soup("")
        container = _work_container(self.poems_page, (POEM_HREF,))
        self._poems = []
        if container is not None:
            for href in _hrefs(container):
                if POEM_HREF.search(href):
                    stub = _stub(href, "/p/")
                    if stub and stub not in self._poems:
                        self._poems.append(stub)

    # ----- stories: the site's data feed --------------------------------------------------

    def _feed_page(self, number: int):
        params = json.dumps(
            {"page": number, "pageSize": API_PAGE_SIZE, "sort": "date", "type": "story", "listType": "expanded"},
            separators=(",", ":"),
        )
        url = API_URL.format(slug=quote(self.slug, safe=""), params=quote(params, safe=""))
        raw = get_url_from_literotica(url)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) and isinstance(data.get("data"), list) else None

    def _fetch_from_feed(self) -> bool:
        """Read every page of the feed. False if the feed could not be used at all."""
        items: list[dict] = []
        number, last, total = 1, 1, None
        while number <= min(last, MAX_API_PAGES):
            data = self._feed_page(number)
            if data is None:
                if number == 1:
                    return False
                log.warning("Page %d of %d of the author's list could not be read; the list is incomplete.", number, last)
                break
            last = int(data.get("last_page") or 1)
            total = data.get("total")
            items.extend(data["data"])
            number += 1
            if number <= last:
                time.sleep(API_PAUSE)

        self._series, self._individual_works = [], []
        self.works, self.series_info = {}, {}
        for item in items:
            if isinstance(item.get("parts"), list):  # a series, with all its parts
                sid = str(item.get("id"))
                stubs = []
                for part in item["parts"]:
                    stub = part.get("url")
                    if stub and stub not in self.works:
                        stubs.append(stub)
                        self.works[stub] = _work_details(part, item.get("title"))
                info = {"title": item.get("title") or sid, "stories": stubs}
                self.series_info[sid] = info
                KNOWN_SERIES[sid] = info  # lets StorySeries use these parts without fetching a page
                if sid not in self._series:
                    self._series.append(sid)
                log.info("Series: %s (%d parts)", info["title"], len(stubs))
            else:  # a standalone story
                stub = item.get("url")
                if stub and stub not in self.works:
                    self._individual_works.append(stub)
                    self.works[stub] = _work_details(item, None)
                    log.info("Individual story: %s", stub)
                elif not stub:
                    log.warning("A story with no address was skipped: %s", item.get("title"))

        # Only series parts carry the category's name. Learn id -> name from them and apply it to
        # the standalone stories; where no name is known, the category's address fragment is kept
        # (the real name is read from the story page itself when the story is downloaded).
        names = {}
        for details in self.works.values():
            if details["category"] and details["category_id"] is not None:
                names[details["category_id"]] = details["category"]
        for details in self.works.values():
            if not details["category"]:
                details["category"] = names.get(details["category_id"]) or details["category_page"]

        for work in list(items):
            name = work.get("authorname")
            if not name and work.get("parts"):
                name = (work["parts"][0] or {}).get("authorname")
            if name:
                self._authorname = name
                break
        if not self._authorname:
            self._authorname = self.slug

        self.total_stories = len(self.works)
        if isinstance(total, int) and len(items) < total:
            log.warning(
                "INCOMPLETE: only %d of the author's %d works were read from the list.", len(items), total
            )
        return True

    # ----- stories: the page itself (fallback) --------------------------------------------

    def _fetch_from_page(self) -> None:
        markup = ""
        for url in (self.stories_url, self.stories_url_plain):
            markup = get_url_from_literotica(url)
            if markup:
                break
            log.info("No usable page at %s; trying the next address", url)
        raw = markup.decode("utf-8", errors="replace") if isinstance(markup, bytes) else (markup or "")
        # the page's own data says how many stories this author has in total
        m = re.search(r'stories_count:(\d+),.{0,600}?username:"%s"' % re.escape(self.slug), raw, re.S | re.I)
        self.total_stories = int(m.group(1)) if m else None
        self.stories_page = _soup(raw)

        # author name: the <p> whose class contains "_header_title_"
        self._authorname = self.slug
        for p in self.stories_page.find_all("p"):
            if _class_has(p, "_header_title_"):
                self._authorname = p.get_text(strip=True) or self.slug
                break
        else:
            log.warning("Author name not found on the page; using %s", self.slug)

        self._series = []
        self._individual_works = []

        container = _work_container(self.stories_page, (STORY_HREF, SERIES_HREF))
        if container is not None:
            groups = [g for g in container.find_all(recursive=False)]
        else:
            log.warning(
                "Story list not found; scanning all links instead (series parts may be downloaded twice)."
            )
            groups = None

        if groups is None:
            hrefs = _hrefs(self.stories_page)
            self._add_series([h for h in hrefs if SERIES_HREF.search(h)])
            self._add_stories([h for h in hrefs if STORY_HREF.search(h)])
        else:
            for group in groups:
                hrefs = _hrefs(group)
                series_hrefs = [h for h in hrefs if SERIES_HREF.search(h)]
                story_hrefs = [h for h in hrefs if STORY_HREF.search(h)]
                if series_hrefs:
                    # a series group: download the series once, not its parts separately
                    log.info("Series: %s", series_hrefs[0])
                    self._add_series(series_hrefs[:1])
                else:
                    for h in story_hrefs:
                        log.info("Individual story: %s", h)
                    self._add_stories(story_hrefs)

        scope = container if container is not None else self.stories_page
        listed = len([h for h in _hrefs(scope) if STORY_HREF.search(h)])
        if self.total_stories and listed < self.total_stories:
            log.warning(
                "INCOMPLETE: the page lists only %d of this author's %d stories. The rest are loaded "
                "by the site's 'Show more' button, which this tool cannot press, so they will NOT be downloaded.",
                listed,
                self.total_stories,
            )

    def _fetch_and_parse(self) -> None:
        if self._parsed:
            return
        self._parsed = True
        if self._fetch_from_feed():
            return
        log.warning("The site's list feed could not be read; reading the author's page instead.")
        self._fetch_from_page()

    def _add_series(self, hrefs: list[str]) -> None:
        for href in hrefs:
            try:
                stub = parse_story_series(href)
            except Exception:
                log.exception("Could not parse series link: %s", href)
                continue
            if stub is not None and stub not in self._series:
                self._series.append(stub)

    def _add_stories(self, hrefs: list[str]) -> None:
        for href in hrefs:
            try:
                stub = parse_story_url(href)
            except Exception:
                log.exception("Could not parse story link: %s", href)
                continue
            if stub is not None and stub not in self._individual_works:
                self._individual_works.append(stub)

    # ----- results ------------------------------------------------------------------------

    @property
    def author_name(self) -> str:
        if self._authorname is None:
            self._fetch_and_parse()
        return self._authorname

    @property
    def series(self) -> list[str | None]:
        if self._series is None:
            self._fetch_and_parse()
        return self._series

    @property
    def individual_stories(self) -> list[str | None]:
        if self._individual_works is None:
            self._fetch_and_parse()
        return self._individual_works

    @property
    def work_details(self) -> dict:
        """story stub -> title, category, words, date, rating, views, tags, series (feed only)."""
        if not self._parsed:
            self._fetch_and_parse()
        return self.works

    @property
    def poems(self) -> list[str | None]:
        if self._poems is None:
            self._fetch_and_parse_alternates()
        return self._poems
