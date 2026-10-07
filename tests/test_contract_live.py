"""Contract tests: real tweet-result responses saved on 2026-10-07 (tests/fixtures/live).

The other fixtures are modelled on the payload format; these are X's actual
bytes, so a failure here means the parser and today's payloads disagree.
"""
from pathlib import Path

import pytest

from xscraper.scraper import tweet_from_body

LIVE = Path(__file__).parent / "fixtures" / "live"


def body(tweet_id: str) -> bytes:
    return (LIVE / f"tweet-result-{tweet_id}.json").read_bytes()


# id: (screen_name, created_at, text start, media types, in_reply_to_id, quoted_tweet_id)
EXPECTED = {
    "1745294979403014244": ("elonmusk", "2024-01-11T04:02:03Z", "Cis is a heterophobic word.",
                            [], None, None),
    "1874918027982172626": ("SenSanders", "2025-01-02T20:37:46Z", "Elon Musk is wrong.",
                            ["photo", "photo", "photo"], None, None),
    "1812256998588662068": ("elonmusk", "2024-07-13T22:45:13Z", "I fully endorse President Trump",
                            ["video"], None, None),
    "1273770669214490626": ("realDonaldTrump", "2020-06-19T00:12:47Z", "",
                            ["video"], None, None),
    "1594131768298315777": ("elonmusk", "2022-11-20T00:53:25Z", "The people have spoken.",
                            [], None, "1593767953706921985"),
    "1603190155107794944": ("elonmusk", "2022-12-15T00:48:13Z", "Last night, car carrying lil X",
                            [], "1603181423787380737", None),
}


def test_every_live_fixture_is_covered():
    ids = {p.stem.removeprefix("tweet-result-") for p in LIVE.glob("tweet-result-*.json")}
    assert ids == set(EXPECTED) | {"88618213008621568", "1347684877634838528"}


@pytest.mark.parametrize("tweet_id", sorted(EXPECTED))
def test_live_tweet_parses(tweet_id):
    screen_name, created_at, text, media, reply_to, quoted = EXPECTED[tweet_id]
    t = tweet_from_body(tweet_id, body(tweet_id))
    assert t is not None and t.id == tweet_id and t.source == "syndication-tweet"
    assert (t.user.screen_name, t.created_at) == (screen_name, created_at)
    assert t.user.id.isdigit() and t.user.name
    assert t.text.startswith(text)
    # t.co links are expanded or, for media, removed; entities are decoded.
    assert "https://t.co/" not in t.text and "&amp;" not in t.text
    assert [m.type for m in t.media] == media
    assert all(m.url.startswith("https://pbs.twimg.com/") for m in t.media)
    assert (t.in_reply_to_id, t.quoted_tweet_id, t.retweeted_tweet_id) == (reply_to, quoted, None)
    # Counts change; only their type is part of the contract. The embed
    # endpoint has no retweet or quote counts at the top level.
    assert isinstance(t.like_count, int) and isinstance(t.reply_count, int)
    assert t.retweet_count is None and t.quote_count is None
    assert isinstance(t.lang, str)


def test_live_video_picks_the_best_mp4():
    [video] = tweet_from_body("1812256998588662068", body("1812256998588662068")).media
    assert video.video_url.endswith(".mp4?tag=16") and "/1280x720/" in video.video_url
    assert (video.width, video.height) == (1280, 720)


def test_live_media_only_tweet_has_no_text():
    # The tweet is only a video link (display_text_range [0, 0], lang zxx), so
    # after the media link is removed there is no text left; that's correct.
    t = tweet_from_body("1273770669214490626", body("1273770669214490626"))
    assert t.text == "" and t.lang == "zxx" and len(t.media) == 1


def test_live_reply_text_unescapes_entities():
    t = tweet_from_body("1603190155107794944", body("1603190155107794944"))
    assert "moving & climbed" in t.text and "Sweeney & organizations" in t.text


@pytest.mark.parametrize("tweet_id", ["88618213008621568", "1347684877634838528"])
def test_live_tombstones_are_unavailable(tweet_id):
    # X answers deleted and unavailable posts with HTTP 200 and a TweetTombstone.
    assert tweet_from_body(tweet_id, body(tweet_id)) is None
