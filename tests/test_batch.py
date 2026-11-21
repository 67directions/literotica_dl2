"""Offline tests for the resumable batch downloader (no network: the site is replaced by stand-ins)."""

import json

import pytest

from literotica_dl2.batch import (
    PROGRESS_FILE,
    Downloader,
    Options,
    SiteBlocked,
    looks_blocked,
    parse_author_names,
)

BODY = "word " * 100  # 500 characters, 100 words


class World:
    """A pretend Literotica: stories by address, with optional failures."""

    def __init__(self):
        self.stories = {}
        self.individual = []
        self.series = {}  # id -> (title, [stubs])
        self.details = {}
        self.fetches = []  # every story address fetched, in order

    def add(self, stub, title, text=BODY, fail_times=0, series=None, words=None, category="Romance"):
        self.stories[stub] = {"title": title, "text": text, "fail": fail_times, "category": category, "block": False}
        if series:
            sid, stitle = series
            self.series.setdefault(sid, (stitle, []))[1].append(stub)
        else:
            self.individual.append(stub)
        self.details[stub] = {"title": title, "words": words if words is not None else len(text.split()),
                              "category": category}


def make(tmp_path, world, **overrides):
    sleeps, lines = [], []

    class FakeAuthor:
        def __init__(self, slug):
            self.slug = slug
            self.author_name = "Test_Author"
            self.individual_stories = list(world.individual)
            self.series = list(world.series)
            self.work_details = world.details
            self.poems = []

    class FakeSeries:
        def __init__(self, sid):
            self.title, self.stories = world.series[sid][0], list(world.series[sid][1])

    class FakeStory:
        def __init__(self, stub):
            world.fetches.append(stub)
            record = world.stories[stub]
            if record["block"]:
                raise SiteBlocked(stub)
            if record["fail"] > 0:
                record["fail"] -= 1
                raise RuntimeError("timed out")
            self.title, self.text, self.markdown = record["title"], record["text"], "# " + record["text"]
            self.category, self.author = record["category"], "Test_Author"

    settings = {"output": str(tmp_path / "out"), "delay": 0, "author_pause": 0, "retries": 3}
    settings.update(overrides)
    options = Options(**settings)
    downloader = Downloader(options, author_factory=FakeAuthor, story_factory=FakeStory, series_factory=FakeSeries,
                            sleep=sleeps.append, out=lines.append)
    return downloader, sleeps, lines


def author_dir(tmp_path):
    return tmp_path / "out" / "Test_Author"


def test_downloads_everything_in_the_tools_layout(tmp_path):
    world = World()
    world.add("one", "First Story")
    world.add("two", "Part 1", series=("s1", "My Series"))
    world.add("three", "Part 2", series=("s1", "My Series"))
    downloader, _, _ = make(tmp_path, world)
    (result,) = downloader.run(["Test_Author"])
    assert (result["downloaded"], result["skipped"], result["failed"]) == (3, 0, 0)
    root = author_dir(tmp_path)
    assert (root / "Individual Stories" / "First Story.txt").read_text(encoding="utf-8") == BODY
    assert (root / "My Series" / "Part 1.txt").is_file()
    assert (root / "My Series" / "Part 2.txt").is_file()
    record = json.loads((root / PROGRESS_FILE).read_text(encoding="utf-8"))
    assert set(record["stories"]) == {"one", "two", "three"}
    assert record["stories"]["one"]["category"] == "Romance"


def test_second_run_skips_without_touching_the_site(tmp_path):
    world = World()
    world.add("one", "First Story")
    world.add("two", "Second Story")
    make(tmp_path, world)[0].run(["Test_Author"])
    world.fetches.clear()
    downloader, _, _ = make(tmp_path, world)
    (result,) = downloader.run(["Test_Author"])
    assert world.fetches == []
    assert (result["downloaded"], result["skipped"]) == (0, 2)


def test_resume_uses_the_record_when_the_list_has_no_titles(tmp_path):
    """Without titles from the site's list (page fallback) a finished story can only be recognised by the record."""
    world = World()
    world.add("one", "First Story")
    world.add("two", "Second Story")
    world.details = {}  # the list gives addresses only
    make(tmp_path, world)[0].run(["Test_Author"])
    world.fetches.clear()
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert world.fetches == []
    assert (result["downloaded"], result["skipped"]) == (0, 2)


def test_deleted_file_is_downloaded_again(tmp_path):
    world = World()
    world.add("one", "First Story")
    world.add("two", "Second Story")
    make(tmp_path, world)[0].run(["Test_Author"])
    (author_dir(tmp_path) / "Individual Stories" / "Second Story.txt").unlink()
    world.fetches.clear()
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert world.fetches == ["two"]
    assert (result["downloaded"], result["skipped"]) == (1, 1)


def test_failure_is_retried_with_growing_waits(tmp_path):
    world = World()
    world.add("one", "Flaky", fail_times=2)
    downloader, sleeps, _ = make(tmp_path, world)
    (result,) = downloader.run(["Test_Author"])
    assert result["downloaded"] == 1 and result["failed"] == 0
    assert world.fetches == ["one", "one", "one"]
    assert sleeps == [10.0, 20.0]


def test_a_story_that_keeps_failing_does_not_stop_the_rest(tmp_path):
    world = World()
    world.add("bad", "Broken", fail_times=99)
    world.add("good", "Fine")
    downloader, _, _ = make(tmp_path, world)
    (result,) = downloader.run(["Test_Author"])
    assert (result["downloaded"], result["failed"]) == (1, 1)
    assert (author_dir(tmp_path) / "Individual Stories" / "Fine.txt").is_file()
    failed = (author_dir(tmp_path) / "failed.txt").read_text(encoding="utf-8")
    assert "https://www.literotica.com/s/bad" in failed and "timed out" in failed

    # the next run retries it, and failed.txt goes away once it works
    world.stories["bad"]["fail"] = 0
    (again,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert (again["downloaded"], again["skipped"], again["failed"]) == (1, 1, 0)
    assert not (author_dir(tmp_path) / "failed.txt").exists()


def test_empty_page_is_a_failure_not_an_empty_file(tmp_path):
    world = World()
    world.add("blocked", "Blocked", text="")
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert result["failed"] == 1
    assert not list((author_dir(tmp_path) / "Individual Stories").glob("*.txt"))


def test_cut_off_story_is_a_failure(tmp_path):
    world = World()
    world.add("short", "Cut Off", text="word " * 100, words=1000)  # site says 1000 words, only 100 arrived
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert result["failed"] == 1
    # with the check switched off it is saved
    world2 = World()
    world2.add("short", "Cut Off", text="word " * 100, words=1000)
    (result2,) = make(tmp_path / "again", world2, min_ratio=0)[0].run(["Test_Author"])
    assert result2["downloaded"] == 1


def test_files_from_the_plain_downloader_are_adopted(tmp_path):
    world = World()
    world.add("one", "Already Here")
    existing = author_dir(tmp_path) / "Individual Stories"
    existing.mkdir(parents=True)
    (existing / "Already Here.txt").write_text("saved by the plain tool", encoding="utf-8")
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert world.fetches == []
    assert (result["downloaded"], result["skipped"]) == (0, 1)
    assert (existing / "Already Here.txt").read_text(encoding="utf-8") == "saved by the plain tool"


def test_two_stories_with_the_same_title_do_not_overwrite_each_other(tmp_path):
    world = World()
    world.add("a", "Untitled", text="alpha " * 100)
    world.add("b", "Untitled", text="bravo " * 100)
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert result["downloaded"] == 2
    texts = sorted(p.read_text(encoding="utf-8")[:5] for p in (author_dir(tmp_path) / "Individual Stories").glob("*.txt"))
    assert texts == ["alpha", "bravo"]


def test_unreadable_progress_file_is_set_aside(tmp_path):
    world = World()
    world.add("one", "First Story")
    root = author_dir(tmp_path)
    root.mkdir(parents=True)
    (root / PROGRESS_FILE).write_text("{ not json", encoding="utf-8")
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert result["downloaded"] == 1
    assert (root / (PROGRESS_FILE + ".corrupt")).is_file()


def test_unicode_text_and_markdown_format(tmp_path):
    world = World()
    world.add("one", "Café ❤", text=("café — \U0001f600 " * 60))
    (result,) = make(tmp_path, world, fmt="md")[0].run(["Test_Author"])
    assert result["downloaded"] == 1
    saved = next((author_dir(tmp_path) / "Individual Stories").glob("*.md"))
    assert saved.read_text(encoding="utf-8").startswith("# café")


def test_interrupted_run_keeps_what_was_saved(tmp_path):
    world = World()
    for n in range(4):
        world.add(f"s{n}", f"Story {n}")
    downloader, _, _ = make(tmp_path, world)
    original = downloader.story_factory

    def interrupting(stub):
        if stub == "s2":
            raise KeyboardInterrupt
        return original(stub)

    downloader.story_factory = interrupting
    downloader.run(["Test_Author"])
    assert downloader.interrupted
    world.fetches.clear()
    (result,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert world.fetches == ["s2", "s3"]
    assert (result["downloaded"], result["skipped"]) == (2, 2)


def test_no_works_is_reported(tmp_path):
    downloader, _, _ = make(tmp_path, World())
    (result,) = downloader.run(["Nobody"])
    assert result["problem"] and result["downloaded"] == 0


def test_parse_author_names():
    said = []
    names = parse_author_names(
        [
            "In_Lust_We_Trust",
            "# a comment",
            "",
            "https://www.literotica.com/authors/Irish%20Moss/works/stories",
            "in_lust_we_trust",
            "uid:4033141",
            "https://www.literotica.com/stories/memberpage.php?uid=1032891&page=submissions",
            "  CaffeineFetish  ",
        ],
        out=said.append,
    )
    assert names == ["In_Lust_We_Trust", "Irish Moss", "CaffeineFetish"]
    assert len(said) == 2


@pytest.mark.parametrize("bad", ["", "   ", "# only a comment"])
def test_blank_lines_are_ignored(bad):
    assert parse_author_names([bad]) == []


CHALLENGE_PAGE = '<!DOCTYPE html><html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt = {};</script>'


def test_looks_blocked():
    assert looks_blocked(CHALLENGE_PAGE)
    assert looks_blocked(CHALLENGE_PAGE.encode("utf-8"))
    assert not looks_blocked("<html><head><title>A Story - Romance - Literotica.com</title></head><body>x</body></html>")
    assert not looks_blocked("")
    assert not looks_blocked(b"")


def test_a_bot_check_page_stops_the_whole_run_without_retries(tmp_path):
    world = World()
    world.add("one", "First Story")
    world.add("two", "Second Story")
    world.add("three", "Third Story")
    world.stories["two"]["block"] = True
    downloader, sleeps, lines = make(tmp_path, world)
    results = downloader.run(["Test_Author", "Second_Author"])
    assert downloader.blocked
    assert len(results) == 1  # the second author was never started
    assert world.fetches == ["one", "two"]  # no retry of the blocked story, nothing after it
    assert sleeps == []  # no retry waits and no pause before another author
    assert any("bot-check" in line for line in lines)
    record = json.loads((author_dir(tmp_path) / PROGRESS_FILE).read_text(encoding="utf-8"))
    assert set(record["stories"]) == {"one"} and record["failed"] == {}  # not marked failed
    assert not (author_dir(tmp_path) / "failed.txt").exists()

    # later, when the site lets us in, the same command finishes the job
    world.stories["two"]["block"] = False
    world.fetches.clear()
    (again,) = make(tmp_path, world)[0].run(["Test_Author"])
    assert world.fetches == ["two", "three"]
    assert (again["downloaded"], again["skipped"], again["failed"]) == (2, 1, 0)


def test_an_author_is_put_aside_after_failures_in_a_row(tmp_path):
    world = World()
    for n in range(8):
        world.add(f"s{n}", f"Story {n}", fail_times=99)
    downloader, _, _ = make(tmp_path, world, retries=0, max_failures=3)
    (result,) = downloader.run(["Test_Author"])
    assert world.fetches == ["s0", "s1", "s2"]
    assert result["failed"] == 3 and "3 failures in a row" in result["problem"]

    world.fetches.clear()
    (everything,) = make(tmp_path / "again", world, retries=0, max_failures=0)[0].run(["Test_Author"])
    assert len(world.fetches) == 8 and everything["failed"] == 8


def test_a_success_resets_the_failure_streak(tmp_path):
    world = World()
    world.add("a", "A", fail_times=99)
    world.add("b", "B", fail_times=99)
    world.add("c", "C")
    world.add("d", "D", fail_times=99)
    world.add("e", "E", fail_times=99)
    downloader, _, _ = make(tmp_path, world, retries=0, max_failures=3)
    (result,) = downloader.run(["Test_Author"])
    assert world.fetches == ["a", "b", "c", "d", "e"]
    assert result["problem"] is None and (result["downloaded"], result["failed"]) == (1, 4)
