import html
import logging
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from literotica_dl2.utils import get_url_from_literotica

log = logging.getLogger("literotica_dl.Poem")

# Literotica's CSS class names are auto-generated and change whenever the site is redeployed,
# so this module avoids depending on class names. It relies on page metadata and structure:
#   - title + category: the og:title <meta> tag ("Title - Category - Literotica.com")
#   - author:           the first link of the form /authors/NAME
#   - poem text:        the block holding the most text
AUTHOR_HREF = re.compile(r"/authors/([^/?#]+)/?$")
AUTHOR_ANY = re.compile(r"/authors/([^/?#]+)")


# Create shorthand method for conversion
def md(soup, **options):
    return MarkdownConverter(**options).convert_soup(soup)


def _soup(markup) -> BeautifulSoup:
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


def _text_element(soup):
    """The block that holds the poem: most text in its own <p> children; if no block has any,
    the block with the most text sitting directly inside it (lines separated by <br>)."""
    containers = soup.find_all(["div", "article", "section"])
    best, best_len = None, 0
    for el in containers:
        n = sum(len(p.get_text()) for p in el.find_all("p", recursive=False))
        if n > best_len:
            best, best_len = el, n
    if best is not None:
        return best
    for el in containers:
        n = sum(len(str(t).strip()) for t in el.find_all(string=True, recursive=False))
        if n > best_len:
            best, best_len = el, n
    return best


class Poem:
    def __init__(self, poem_id: str) -> None:
        self.url: str = f"https://www.literotica.com/p/{poem_id}"
        self.poem_id: str = poem_id
        self.first_page = None
        self._title: str = None
        self._text: str = None
        self._category: str = None
        self._md: str = None
        self._author: str = None

    def _fetch_and_parse(self) -> None:
        if self.first_page is not None:
            return
        log.info("Getting the initial page for %s", self.poem_id)
        self.first_page = _soup(get_url_from_literotica(self.url))
        soup = self.first_page

        # Title and category: "Title - Category - Literotica.com"
        full = _meta(soup, "og:title")
        if not full:
            tag = soup.find("title")
            full = html.unescape(tag.get_text()).strip() if tag is not None else None
        if not full:
            tag = soup.find("h1")
            full = tag.get_text(strip=True) if tag is not None else self.poem_id
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
            log.warning("Author not found for %s", self.poem_id)
            self._author = "Unknown"

        poem_div = _text_element(soup)
        if poem_div is None:
            log.warning("No poem text found for %s", self.poem_id)
            self._text, self._md = "", ""
        else:
            self._text = poem_div.get_text("\n")
            self._md = md(poem_div)

    @property
    def title(self) -> str:
        """Title of the poem"""
        if self._title is None:
            self._fetch_and_parse()
        return self._title

    @property
    def author(self) -> str:
        """Author of the poem"""
        if self._author is None:
            self._fetch_and_parse()
        return self._author

    @property
    def category(self) -> str:
        """Category of the poem"""
        if self._category is None:
            self._fetch_and_parse()
        return self._category

    @property
    def text(self) -> str:
        """The full poem"""
        if self._text is None:
            self._fetch_and_parse()
        return self._text

    @property
    def markdown(self) -> str:
        """The full poem in markdown"""
        if self._md is None:
            self._fetch_and_parse()
        return self._md
