import html
import logging
import re
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from literotica_dl2.utils import get_url_from_literotica

log = logging.getLogger("literotica_dl.Story")

# Literotica's CSS class names are auto-generated and change whenever the site is redeployed,
# so this module avoids depending on class names. It relies on page structure and metadata:
#   - title + category: the og:title <meta> tag ("Title - Category - Literotica.com")
#   - description:      the description <meta> tag
#   - author:           the first link of the form /authors/NAME
#   - story text:       the block holding the most paragraph text
#   - further pages:    links to <story url>?page=N
AUTHOR_HREF = re.compile(r"/authors/([^/?#]+)/?$")
AUTHOR_ANY = re.compile(r"/authors/([^/?#]+)")
PAGE_NUM = re.compile(r"[?&]page=(\d+)")
MAX_PAGES = 200  # safety stop


# Create shorthand method for conversion
def md(soup, **options):
    return MarkdownConverter(**options).convert_soup(soup)


def _soup(markup: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(markup, features="html5lib")
    except Exception:  # html5lib not installed
        return BeautifulSoup(markup, features="html.parser")


def _meta(soup, *names):
    """Content of the first <meta> whose name or property matches one of names."""
    for name in names:
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag is not None and tag.get("content"):
            return html.unescape(tag["content"]).strip()
    return None


def _story_element(soup):
    """The block that holds the story text: the one with the most text in its own <p> children."""
    best, best_len = None, 0
    for el in soup.find_all(["div", "article", "section"]):
        n = sum(len(p.get_text()) for p in el.find_all("p", recursive=False))
        if n > best_len:
            best, best_len = el, n
    return best


class Story:
    def __init__(self, story_id: str) -> None:
        self.url: str = f"https://www.literotica.com/s/{story_id}"
        self.story_id: str = story_id
        self.first_page = None
        self._author: str = None
        self._category: str = None
        self._description: str = None
        self._pages: int = None
        self._text = None
        self._title = None
        self._md = None

    def _fetch_and_parse(self) -> None:
        if self.first_page is not None:
            return
        log.info("Getting the initial page for %s", self.story_id)

        page = get_url_from_literotica(self.url)
        self.first_page = _soup(page)
        soup = self.first_page

        # Title and category: "Title - Category - Literotica.com"
        full = _meta(soup, "og:title")
        if not full:
            tag = soup.find("title")
            full = html.unescape(tag.get_text()).strip() if tag is not None else None
        if not full:
            tag = soup.find("h1")
            full = tag.get_text(strip=True) if tag is not None else self.story_id
        full = re.sub(r"\s*-\s*Literotica\.com\s*$", "", full, flags=re.I).strip()
        if " - " in full:
            self._title, self._category = full.rsplit(" - ", 1)
        else:
            self._title, self._category = full, ""

        # Author: the first link that points at /authors/NAME (not /authors/NAME/works...)
        self._author = None
        for a in soup.find_all("a", href=True):
            m = AUTHOR_HREF.search(a["href"])
            if m:
                self._author = a.get_text(strip=True) or unquote(m.group(1))
                break
        if not self._author:
            for a in soup.find_all("a", href=True):
                m = AUTHOR_ANY.search(a["href"])
                if m:
                    self._author = unquote(m.group(1))
                    break
        if not self._author:
            log.warning("Author not found for %s", self.story_id)
            self._author = "Unknown"

        self._description = _meta(soup, "description", "og:description") or ""

        # Number of pages as shown by the page links (updated to the real number when the
        # full story is downloaded, which follows the "next page" links).
        self._pages = self._count_pages(soup)

    def _count_pages(self, soup) -> int:
        numbers = [1]
        for a in soup.find_all("a", href=True):
            m = PAGE_NUM.search(a["href"])
            if m and self.story_id in a["href"]:
                numbers.append(int(m.group(1)))
        return max(numbers)

    def _next_page(self, soup, current: int):
        """(number, url) of the link to the page after `current`, or None on the last page."""
        wanted = current + 1
        for a in soup.find_all("a", href=True):
            m = PAGE_NUM.search(a["href"])
            if m and int(m.group(1)) == wanted and self.story_id in a["href"]:
                return wanted, urljoin(self.url, a["href"])
        return None

    @property
    def title(self) -> str:
        """Title of the story"""
        if self._title is None:
            self._fetch_and_parse()
        return self._title

    @property
    def author(self) -> str:
        """Author of the story"""
        if self._author is None:
            self._fetch_and_parse()
        return self._author

    @property
    def category(self) -> str:
        """Category of the story"""
        if self._category is None:
            self._fetch_and_parse()
        return self._category

    @property
    def description(self) -> str:
        """Description of the story"""
        if self._description is None:
            self._fetch_and_parse()
        return self._description

    @property
    def pages(self) -> int:
        """Pages of the story"""
        if self._pages is None:
            self._fetch_and_parse()
        return self._pages

    def populate_full_story(self) -> None:
        if self._pages is None:
            self._fetch_and_parse()
        # We already have the first page. Follow the "page N+1" links until there are none,
        # e.g. https://www.literotica.com/s/hot-neighbor-noelle?page=3
        page_soups = [self.first_page]
        current, soup = 1, self.first_page
        while current < MAX_PAGES:
            nxt = self._next_page(soup, current)
            if nxt is None:
                break
            current, page_url = nxt
            soup = _soup(get_url_from_literotica(page_url))
            page_soups.append(soup)
        self._pages = len(page_soups)

        texts, markdowns = [], []
        for number, page_soup in enumerate(page_soups, 1):
            element = _story_element(page_soup)
            if element is None:
                log.warning("No story text found on page %d of %s", number, self.story_id)
                continue
            texts.append(element.get_text("\n\n"))
            markdowns.append(md(element))

        self._text = "\n".join(texts)
        self._md = "\n".join(markdowns)

    @property
    def text(self) -> str:
        """The full story"""
        if self._text is None:
            self.populate_full_story()
        return self._text

    @property
    def markdown(self) -> str:
        """The full story in markdown"""
        if self._md is None:
            self.populate_full_story()
        return self._md
