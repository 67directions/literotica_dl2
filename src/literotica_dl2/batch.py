"""Resumable batch downloader for Literotica authors.

Run it from the folder where the "output" folder should be:

    python -m literotica_dl2.batch AUTHOR [AUTHOR ...]
    python -m literotica_dl2.batch -f authors.txt

An author can be given as a name (In_Lust_We_Trust) or as a profile link
(https://www.literotica.com/authors/In_Lust_We_Trust/works/stories).

It saves files in the same layout as the "author" command:

    output/<author>/Individual Stories/<title>.txt
    output/<author>/<series title>/<title>.txt

and adds what the plain command lacks:

* Resume. Each author folder gets a small ".progress.json" record. Stories already saved are
  skipped without any request to the site, so an interrupted run (Ctrl+C, a crash, a closed
  window) carries on by running the same command again. A story whose file was deleted is
  downloaded again. Files saved by the plain "author" command are recognised and skipped too.
* Pauses. At least --delay seconds pass between requests to the site.
* Retries. A failed story is retried with longer and longer waits. If it still fails it is
  noted in the author's "failed.txt" and the run moves on to the next story instead of stopping.
* Checks. A page with no story text, or far fewer words than the site says the story has, is
  treated as a failure instead of being saved as an empty or cut-off file.
* Files are written as UTF-8 and only appear under their final name once complete.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import literotica_dl2.author as _author_module
import literotica_dl2.poem as _poem_module
import literotica_dl2.story as _story_module
import literotica_dl2.story_series as _series_module
from literotica_dl2.author import Author
from literotica_dl2.poem import Poem
from literotica_dl2.story import Story
from literotica_dl2.story_series import StorySeries
from literotica_dl2.utils import get_sane_filename

PROGRESS_FILE = ".progress.json"
INDIVIDUAL = "Individual Stories"
MAX_NAME = 150  # keeps Windows paths under the 260-character limit
MIN_CHARS = 30  # less text than this is not a story


class DownloadError(Exception):
    """A download that failed even after all retries."""


@dataclass
class Options:
    output: str = "output"
    fmt: str = "txt"  # "txt" or "md"
    delay: float = 3.0  # least seconds between requests to the site
    retries: int = 3  # extra attempts per story after the first
    author_pause: float = 20.0  # seconds between authors
    poems: bool = False
    min_ratio: float = 0.5  # fewer words than this share of the site's count = failed download


@dataclass
class Item:
    stub: str
    kind: str  # "story" or "poem"
    folder: str
    details: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://www.literotica.com/{'p' if self.kind == 'poem' else 's'}/{self.stub}"


def _say(message: str = "") -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:  # a console that cannot show some character in a title
        print(message.encode("ascii", "replace").decode("ascii"), flush=True)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Progress:
    """What has been saved for one author, kept in <author folder>/.progress.json."""

    def __init__(self, path: Path, author: str, out) -> None:
        self.path = path
        self.data: dict = {"version": 1, "author": author, "stories": {}, "failed": {}}
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(loaded.get("stories"), dict):
                raise ValueError("unexpected layout")
            self.data.update(loaded)
            if not isinstance(self.data.get("failed"), dict):
                self.data["failed"] = {}
        except (ValueError, OSError) as error:
            backup = path.with_name(path.name + ".corrupt")
            out(f"  The progress record could not be read ({error}); keeping it as {backup.name} and starting fresh.")
            try:
                os.replace(path, backup)
            except OSError:
                pass

    def record(self, stub: str):
        return self.data["stories"].get(stub)

    def owner_of(self, relative: str):
        for stub, record in self.data["stories"].items():
            if record.get("file") == relative:
                return stub
        return None

    def mark_done(self, stub: str, relative: str, **info) -> None:
        self.data["stories"][stub] = {"file": relative, "saved": _now(), **info}
        self.data["failed"].pop(stub, None)
        self.save()

    def mark_failed(self, stub: str, kind: str, error: str) -> None:
        self.data["failed"][stub] = {"kind": kind, "error": error, "when": _now()}
        self.save()

    def save(self) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)


class Downloader:
    def __init__(
        self,
        options: Options,
        *,
        author_factory=Author,
        story_factory=Story,
        series_factory=StorySeries,
        poem_factory=Poem,
        sleep=time.sleep,
        out=_say,
    ) -> None:
        self.opts = options
        self.author_factory = author_factory
        self.story_factory = story_factory
        self.series_factory = series_factory
        self.poem_factory = poem_factory
        self.sleep = sleep
        self.out = out
        self.interrupted = False

    # ----- helpers -------------------------------------------------------------------

    @staticmethod
    def _clean(name) -> str:
        return get_sane_filename(str(name or ""))[:MAX_NAME].strip()

    def _backoff(self, attempt: int) -> float:
        return max(10.0, self.opts.delay) * (2**attempt)

    def _retry(self, label: str, action):
        last = "unknown error"
        for attempt in range(self.opts.retries + 1):
            try:
                return action()
            except Exception as error:  # noqa: BLE001 - any failure is retried, then reported
                last = f"{type(error).__name__}: {error}"
                if attempt < self.opts.retries:
                    wait = self._backoff(attempt)
                    self.out(f"    {label} failed ({last}); trying again in {wait:.0f}s")
                    self.sleep(wait)
        raise DownloadError(last)

    def _author_dir(self, name: str, slug: str) -> Path:
        root = Path(self.opts.output)
        candidates = [self._clean(name), self._clean(slug)]
        for candidate in candidates:
            if candidate and (root / candidate).is_dir():
                return root / candidate
        return root / (candidates[0] or candidates[1] or "unknown_author")

    def _target(self, author_dir: Path, item: Item, title: str, extension: str, progress: Progress) -> Path:
        base = self._clean(title) or self._clean(item.stub) or "untitled"
        path = author_dir / item.folder / (base + extension)
        owner = progress.owner_of(path.relative_to(author_dir).as_posix())
        if owner not in (None, item.stub):  # another story already uses this name
            tail = f" ({self._clean(item.stub.replace('-', '_'))})"
            path = author_dir / item.folder / (base[: MAX_NAME - len(tail)] + tail + extension)
        return path

    @staticmethod
    def _usable(path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    # ----- one author ----------------------------------------------------------------

    def _plan(self, slug: str):
        def build():
            author = self.author_factory(slug)
            individual = list(author.individual_stories)
            series_ids = list(author.series)
            details = getattr(author, "work_details", None) or {}
            items: list[Item] = []
            seen: set = set()

            def add(stub, kind, folder):
                if stub and stub not in seen:
                    seen.add(stub)
                    items.append(Item(stub, kind, folder, details.get(stub) or {}))

            for stub in individual:
                add(stub, "story", INDIVIDUAL)
            for series_id in series_ids:
                series = self.series_factory(series_id)
                folder = self._clean(series.title) or str(series_id)
                for stub in series.stories:
                    add(stub, "story", folder)
            if self.opts.poems:
                for stub in author.poems:
                    add(stub, "poem", "Poems")
            return author.author_name or slug, items

        return self._retry("getting the author's list", build)

    def _fetch_work(self, item: Item):
        poem = item.kind == "poem"

        def attempt():
            work = (self.poem_factory if poem else self.story_factory)(item.stub)
            text = work.text or ""
            if len(text.strip()) < MIN_CHARS:
                raise ValueError("the page had no story text (blocked, removed or redirected?)")
            expected = item.details.get("words") or 0
            if self.opts.min_ratio and expected >= 200:
                got = len(text.split())
                if got < expected * self.opts.min_ratio:
                    raise ValueError(f"only {got} of about {expected} words came through")
            if self.opts.fmt == "md":
                work.markdown  # noqa: B018 - make sure the Markdown version exists
            return work

        return self._retry("download", attempt)

    def process_author(self, slug: str) -> dict:
        result = {"author": slug, "downloaded": 0, "skipped": 0, "failed": 0, "words": 0, "problem": None}
        self.out(f"\n=== {slug} ===")
        try:
            name, items = self._plan(slug)
        except DownloadError as error:
            result["problem"] = f"could not get the list of works: {error}"
            self.out(f"  {result['problem']}")
            return result
        if not items:
            result["problem"] = "no works found (wrong name, no stories, or the site refused the request)"
            self.out(f"  {result['problem']}")
            return result

        author_dir = self._author_dir(name, slug)
        author_dir.mkdir(parents=True, exist_ok=True)
        progress = Progress(author_dir / PROGRESS_FILE, name, self.out)
        extension = ".md" if self.opts.fmt == "md" else ".txt"
        self.out(f"  {len(items)} works to check; saving into {author_dir}")

        try:
            for number, item in enumerate(items, 1):
                self._handle_item(number, len(items), item, author_dir, extension, progress, result)
        finally:
            self._write_failed_list(author_dir, progress)

        self.out(
            f"  {name}: {result['downloaded']} downloaded, {result['skipped']} already had, {result['failed']} failed"
        )
        return result

    def _handle_item(self, number, total, item, author_dir, extension, progress, result) -> None:
        details = item.details
        hint = details.get("title")
        record = progress.record(item.stub)

        if record and self._usable(author_dir / record.get("file", "")):
            result["skipped"] += 1
            return
        if hint:
            path = self._target(author_dir, item, hint, extension, progress)
            if self._usable(path):  # saved earlier by the plain downloader: adopt it
                relative = path.relative_to(author_dir).as_posix()
                progress.mark_done(item.stub, relative, title=hint, kind=item.kind, category=details.get("category", ""),
                                   words=details.get("words", 0), adopted=True)
                result["skipped"] += 1
                return

        shown = hint or item.stub
        self.out(f"  [{number}/{total}] {shown}")
        try:
            work = self._fetch_work(item)
        except DownloadError as error:
            progress.mark_failed(item.stub, item.kind, str(error))
            result["failed"] += 1
            self.out(f"    FAILED: {error}")
            return

        title = work.title or hint or item.stub
        path = self._target(author_dir, item, title, extension, progress)
        content = work.markdown if self.opts.fmt == "md" else work.text
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".part")
            partial.write_text(content, encoding="utf-8")
            os.replace(partial, path)
        except OSError as error:
            progress.mark_failed(item.stub, item.kind, f"could not save the file: {error}")
            result["failed"] += 1
            self.out(f"    FAILED: could not save the file: {error}")
            return

        words = len(work.text.split())
        category = details.get("category") or getattr(work, "category", "") or ""
        progress.mark_done(item.stub, path.relative_to(author_dir).as_posix(), title=title, kind=item.kind,
                           category=category, words=words)
        result["downloaded"] += 1
        result["words"] += words

    @staticmethod
    def _write_failed_list(author_dir: Path, progress: Progress) -> None:
        path = author_dir / "failed.txt"
        failed = progress.data.get("failed") or {}
        if not failed:
            if path.exists():
                path.unlink()
            return
        lines = []
        for stub, info in failed.items():
            kind = "p" if info.get("kind") == "poem" else "s"
            lines.append(f"https://www.literotica.com/{kind}/{stub}\t{info.get('error', '')}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ----- everything ----------------------------------------------------------------

    def run(self, slugs: list) -> list:
        results = []
        try:
            for index, slug in enumerate(slugs):
                result = self.process_author(slug)
                results.append(result)
                if (result["downloaded"] + result["failed"]) and index < len(slugs) - 1:
                    self.sleep(self.opts.author_pause)
        except KeyboardInterrupt:
            self.interrupted = True
            self.out("\nStopped. Run the same command again to carry on where it left off.")
        self._summary(results)
        return results

    def _summary(self, results: list) -> None:
        if not results:
            return
        self.out("\n=== Summary ===")
        for result in results:
            line = (f"{result['author']}: {result['downloaded']} downloaded, {result['skipped']} already had, "
                    f"{result['failed']} failed, {result['words']:,} new words")
            if result["problem"]:
                line += f"  ** {result['problem']}"
            self.out(line)
        if any(r["failed"] for r in results):
            self.out("Failed stories are listed in each author's failed.txt; run the same command again to retry them.")


def install_pacing(delay: float, sleep=time.sleep, clock=time.monotonic) -> None:
    """Make every request to the site wait until at least `delay` seconds after the last one."""
    state = {"last": -1e9}

    def wrap(function):
        if getattr(function, "_paced", False):
            return function

        def paced(url):
            gap = state["last"] + delay - clock()
            if gap > 0:
                sleep(gap + random.uniform(0, 0.5))
            try:
                return function(url)
            finally:
                state["last"] = clock()

        paced._paced = True
        return paced

    for module in (_author_module, _story_module, _series_module, _poem_module):
        module.get_url_from_literotica = wrap(module.get_url_from_literotica)


def parse_author_names(values, out=_say) -> list:
    """Names from a list of lines or arguments. Profile links are reduced to the name."""
    names: list = []
    seen: set = set()
    for value in values:
        value = value.strip()
        if not value or value.startswith("#"):
            continue
        match = re.search(r"/authors/([^/?#\s]+)", value)
        name = unquote(match.group(1)) if match else value
        if re.fullmatch(r"(uid:)?\d+", name, re.I) or "memberpage.php" in value:
            out(f"Skipping '{value}': numeric profile ids are not supported; use the author's name instead.")
            continue
        if name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m literotica_dl2.batch",
        description="Download all stories of Literotica authors, resumably.",
    )
    parser.add_argument("authors", nargs="*", help="author names or profile links")
    parser.add_argument("-f", "--file", help="text file with one author per line (# starts a comment)")
    parser.add_argument("-o", "--output", default="output", help="folder to save into (default: output)")
    parser.add_argument("--format", choices=["txt", "md"], default="txt", help="file format (default: txt)")
    parser.add_argument("--delay", type=float, default=3.0, help="least seconds between requests (default: 3)")
    parser.add_argument("--retries", type=int, default=3, help="extra attempts for a failed story (default: 3)")
    parser.add_argument("--author-pause", type=float, default=20.0, help="seconds between authors (default: 20)")
    parser.add_argument("--min-ratio", type=float, default=0.5,
                        help="a story with fewer than this share of the site's word count is treated as a failed "
                             "download; 0 turns the check off (default: 0.5)")
    parser.add_argument("--poems", action="store_true", help="also download the author's poems")
    parser.add_argument("--clear-cache", action="store_true",
                        help="forget saved pages first (use to pick up an author's new stories)")
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    values = list(args.authors)
    if args.file:
        try:
            values += Path(args.file).read_text(encoding="utf-8-sig").splitlines()
        except OSError as error:
            parser.error(f"cannot read {args.file}: {error}")
    names = parse_author_names(values)
    if not names:
        parser.error("give at least one author name, or a file with -f authors.txt")

    logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
    if args.clear_cache:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(".literotica_cache.sqlite" + suffix).unlink()
            except OSError:
                pass

    options = Options(output=args.output, fmt=args.format, delay=args.delay, retries=args.retries,
                      author_pause=args.author_pause, poems=args.poems, min_ratio=args.min_ratio)
    install_pacing(options.delay)
    downloader = Downloader(options)
    results = downloader.run(names)
    failed = downloader.interrupted or any(r["failed"] or r["problem"] for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
