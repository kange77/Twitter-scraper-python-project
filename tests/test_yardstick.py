"""The Quest yardstick (quest/yardstick.md) as a test, so CI enforces it on every change.

Runs the real `xscraper tweet` CLI in a subprocess against quest/checks.py's
synthetic mock: 1,000 IDs, 3 of them malformed. Any change, from an agent or a
person, that loses a good tweet, writes a malformed payload as a tweet, hides a
failure, or changes a clean batch fails here instead of in review.
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("quest_checks", os.path.join(ROOT, "quest", "checks.py"))
checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checks)

ENGINES = ["sync"] + (["async"] if importlib.util.find_spec("aiohttp") else [])


@pytest.fixture(scope="module")
def mock():
    m = checks.Mock()
    yield m
    m.server.shutdown()


@pytest.mark.parametrize("http", ENGINES)
def test_yardstick_poisoned_batch(mock, tmp_path, http):
    r = checks.batch(mock, ROOT, http, 1000, str(tmp_path))
    good = r["ids"] - r["poison_ids"]
    assert r["tweets_written"] <= good, f"Q2: {r['tweets_written'] - good} malformed payloads were written as tweets"
    assert r["ids_lost"] == 0, f"Q1: a bad payload cost {r['ids_lost']} good tweets"
    assert r["failures_reported"] == r["poison_ids"], "Q2: not every bad ID was named on stderr"
    assert not r["traceback"] and r["exit"] == 1, "Q2: expected named failures and exit 1, no traceback"
    assert r["requests"] == r["ids"], "every ID is fetched exactly once"


@pytest.mark.parametrize("http", ENGINES)
def test_yardstick_clean_batch_unchanged(mock, tmp_path, http):
    r = checks.batch(mock, ROOT, http, 400, str(tmp_path))
    assert (r["tweets_written"], r["exit"], r["requests"]) == (400, 0, 400), "Q4: clean batch changed"


@pytest.mark.skipif(sys.platform == "win32", reason="subprocess timing")
def test_yardstick_reruns_converge(mock, tmp_path):
    first = checks.batch(mock, ROOT, "sync", 1000, str(tmp_path))
    again = checks.batch(mock, ROOT, "sync", 1000, str(tmp_path))
    keys = ("tweets_written", "failures_reported", "exit")
    assert [first[k] for k in keys] == [again[k] for k in keys], "Q3: a rerun gave a different result"
