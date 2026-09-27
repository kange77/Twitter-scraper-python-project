import json

import pytest

from tests.conftest import FakeClient, FakeResponse
from xscraper.http import NotFound
from xscraper.scraper import Scraper, parse_screen_name, parse_tweet_id
from xscraper.token import syndication_token


@pytest.mark.parametrize("value,expected", [
    ("1834231234567890123", "1834231234567890123"),
    (1834231234567890123, "1834231234567890123"),
    ("https://x.com/NASA/status/123?s=20", "123"),
    ("https://twitter.com/i/web/status/456", "456"),
    ("https://mobile.twitter.com/a/statuses/789/photo/1", "789"),
])
def test_parse_tweet_id(value, expected):
    assert parse_tweet_id(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("NASA", "NASA"), ("@NASA", "NASA"), ("https://x.com/NASA", "NASA"),
    ("https://twitter.com/NASA/media", "NASA"),
])
def test_parse_screen_name(value, expected):
    assert parse_screen_name(value) == expected


@pytest.mark.parametrize("bad", ["not a url", "a b", "x" * 16])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_screen_name(bad)


def embed_client(tweet_results):
    by_id = {tweet_results["tweet"]["id_str"]: tweet_results["tweet"],
             tweet_results["parent"]["id_str"]: tweet_results["parent"],
             "666": tweet_results["tombstone"]}

    def handler(url, params):
        assert params["token"] == syndication_token(params["id"])
        if params["id"] not in by_id:
            raise NotFound("404")
        return FakeResponse(200, json.dumps(by_id[params["id"]]))

    return FakeClient(handler)


def test_tweet_and_missing(tweet_results):
    s = Scraper(embed_client(tweet_results))
    assert s.tweet("https://x.com/NASA/status/1834231234567890123").like_count == 48213
    assert s.tweet("666") is None
    assert s.tweet("999") is None


def test_tweets_keeps_order_concurrently(tweet_results):
    s = Scraper(embed_client(tweet_results), workers=4)
    ids = ["1834231000000000000", "999", "1834231234567890123", "666"]
    assert [t and t.id for t in s.tweets(ids)] == ["1834231000000000000", None, "1834231234567890123", None]


def test_thread_is_oldest_first(tweet_results):
    s = Scraper(embed_client(tweet_results))
    assert [t.id for t in s.thread("1834231234567890123")] == ["1834231000000000000", "1834231234567890123"]
    assert len(s.thread("1834231234567890123", max_depth=1)) == 1


def test_user_timeline(timeline_html):
    client = FakeClient(lambda url, params: FakeResponse(200, timeline_html))
    s = Scraper(client)
    assert len(s.user_timeline("@NASA")) == 3
    assert all(not t.is_retweet for t in s.user_timeline("NASA", include_retweets=False))
    assert client.calls[0][0].endswith("/screen-name/NASA")
