import csv

import pytest

from xscraper.analysis import Analyzer
from xscraper.parse import parse_timeline_page
from xscraper.storage import TweetStore, detect_format, export, load


@pytest.fixture
def tweets(timeline_html):
    return Analyzer("python").annotate(parse_timeline_page(timeline_html))


@pytest.mark.parametrize("name", ["out.json", "out.jsonl", "out.db"])
def test_roundtrip(tmp_path, tweets, name):
    path = tmp_path / name
    assert export(tweets, path) == 3
    loaded = load(path)
    assert sorted(t.to_dict()["id"] for t in loaded) == sorted(t.id for t in tweets)
    assert {t.id: t for t in loaded}[tweets[2].id].media[0].width == 4000


def test_csv(tmp_path, tweets):
    path = tmp_path / "out.csv"
    export(tweets, path)
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows[0]["screen_name"] == "NASA" and rows[0]["url"].startswith("https://x.com/NASA/status/")
    assert len(rows[0]["simhash"]) == 16
    with pytest.raises(ValueError):
        load(path)


def test_sqlite_counts_only_new(tmp_path, tweets):
    path = tmp_path / "store.db"
    assert export(tweets[:2], path) == 2
    assert export(tweets, path) == 1
    with TweetStore(path) as store:
        assert len(store) == 3 and len(store.known_ids()) == 3


def test_detect_format():
    assert detect_format("a.NDJSON") == "jsonl"
    assert detect_format("a.txt", "csv") == "csv"
    with pytest.raises(ValueError):
        detect_format("a.txt")


def test_load_errors_are_friendly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "missing.jsonl")
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()  # must not create an empty store

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id": "1"}\n{"broken\n')
    with pytest.raises(ValueError, match="line 1: not|line 2: invalid JSON|not an xscraper export"):
        load(bad)
    obj = tmp_path / "obj.json"
    obj.write_text('{"a": 1}')
    with pytest.raises(ValueError, match="JSON array"):
        load(obj)


def test_sqlite_not_a_database_and_nested_dir(tmp_path, tweets):
    junk = tmp_path / "junk.db"
    junk.write_text("hello")
    with pytest.raises(ValueError, match="not an xscraper SQLite store"):
        export(tweets, junk)
    assert export(tweets, tmp_path / "a" / "b" / "store.db") == 3
