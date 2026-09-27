import pytest

from xscraper.parse import ParseError, normalize_date, parse_timeline_page, parse_tweet_result


def test_embed_tweet(tweet_results):
    t = parse_tweet_result(tweet_results["tweet"])
    assert t.id == "1834231234567890123"
    assert t.user.screen_name == "NASA" and t.user.verified
    assert t.created_at == "2024-09-12T14:05:33Z"
    assert t.like_count == 48213 and t.reply_count == 1204 and t.retweet_count is None
    # t.co links expanded, trailing media link removed
    assert t.text == ("We have liftoff! #Artemis heads for the Moon with @NASA_Orion aboard. "
                      "Watch: https://www.nasa.gov/live")
    assert t.hashtags == ["Artemis"] and t.mentions == ["NASA_Orion"]
    assert t.urls == ["https://www.nasa.gov/live"]
    assert t.in_reply_to_id == "1834231000000000000"
    assert t.quoted_tweet_id == "1834000000000000001"
    [video] = t.media
    assert video.type == "video" and video.width == 1920
    assert video.video_url.endswith("high.mp4")  # highest-bitrate MP4
    assert t.url == "https://x.com/NASA/status/1834231234567890123"


def test_html_entities_unescaped(tweet_results):
    assert "Go for launch & good luck!" in parse_tweet_result(tweet_results["parent"]).text


def test_tombstone_is_none(tweet_results):
    assert parse_tweet_result(tweet_results["tombstone"]) is None
    assert parse_tweet_result({}) is None


def test_timeline_page(timeline_html):
    tweets = parse_timeline_page(timeline_html)
    assert [t.id[-1] for t in tweets] == ["3", "2", "1"]  # notice entry skipped
    sci, rt, photo = tweets
    assert sci.text == "Science is amazing. Read more: https://science.nasa.gov/"
    assert sci.created_at == "2024-09-12T18:00:00Z"
    assert sci.user.followers_count == 88000000
    assert rt.is_retweet and rt.retweeted_tweet_id == "1834299999999999999"
    assert photo.text == "Photo of the day"
    assert photo.media[0].url == "https://pbs.twimg.com/media/pod.jpg"
    assert photo.like_count == 5000


def test_timeline_without_payload():
    with pytest.raises(ParseError):
        parse_timeline_page("<html>login wall</html>")


def test_normalize_date_passthrough():
    assert normalize_date("not a date") == "not a date"
    assert normalize_date(None) is None
