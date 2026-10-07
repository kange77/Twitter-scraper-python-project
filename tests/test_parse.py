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


def test_unavailable_tweet_is_none():
    assert parse_tweet_result({"__typename": "TweetUnavailable", "reason": "Protected"}) is None


def test_reply_mentions_hidden_by_display_text_range(tweet_results):
    reply = dict(tweet_results["tweet"], text="@NASA @alice Go Artemis! 🚀", display_text_range=[13, 26],
                 entities={"user_mentions": [{"screen_name": "NASA"}, {"screen_name": "alice"}]})
    t = parse_tweet_result(reply)
    assert t.text == "Go Artemis! 🚀"
    assert t.mentions == ["NASA", "alice"]  # still recorded as entities


def test_display_text_range_only_strips_mentions(tweet_results):
    # A range that would cut real words (e.g. a quote tweet's leading text) is ignored.
    t = parse_tweet_result(dict(tweet_results["parent"], text="Hello world", display_text_range=[6, 11]))
    assert t.text == "Hello world"


def test_retweet_uses_original_text():
    original = ("Webb just captured the most detailed image yet of the Pillars of Creation, showing newly "
                "formed stars glowing inside clouds of gas and dust. More: https://t.co/w")
    rt = {"id_str": "2", "full_text": "RT @NASAWebb: " + original[:125] + "…",
          "user": {"screen_name": "NASA"},
          "entities": {"user_mentions": [{"screen_name": "NASAWebb"}]},
          "retweeted_status": {"id_str": "1", "full_text": original, "user": {"screen_name": "NASAWebb"},
                               "entities": {"hashtags": [{"text": "JWST"}],
                                            "urls": [{"url": "https://t.co/w", "expanded_url": "https://webb.nasa.gov"}]}}}
    [t] = parse_timeline_page(_page([rt]))
    assert t.is_retweet and t.retweeted_tweet_id == "1"
    assert t.text == "RT @NASAWebb: " + original.replace("https://t.co/w", "https://webb.nasa.gov")
    assert t.hashtags == ["JWST"] and t.urls == ["https://webb.nasa.gov"]
    assert t.mentions == ["NASAWebb"]


def test_timeline_sorted_newest_first_despite_pinned_tweet():
    pinned = {"id_str": "100", "full_text": "pinned, old"}
    newer = [{"id_str": "300", "full_text": "newest"}, {"id_str": "200", "full_text": "older"}]
    assert [t.id for t in parse_timeline_page(_page([pinned, *newer]))] == ["300", "200", "100"]


def _page(tweets):
    import json as _json
    entries = [{"type": "tweet", "content": {"tweet": t}} for t in tweets]
    return ('<script id="__NEXT_DATA__" type="application/json">'
            + _json.dumps({"props": {"pageProps": {"timeline": {"entries": entries}}}}) + "</script>")


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


@pytest.mark.parametrize("field,value", [
    ("entities", "oops"), ("entities", {"urls": "x", "hashtags": [1, None, {"text": 5}]}),
    ("user", ["not", "a", "dict"]), ("created_at", True), ("favorite_count", "12"),
    ("mediaDetails", [None, {"video_info": {"variants": [{"url": 1}, "x"]}}]),
    ("text", {"nested": 1}), ("parent", "123"), ("quoted_tweet", 7),
    ("display_text_range", "0,5"), ("display_text_range", [True, None]),
    ("display_text_range", [9999]),
    ("retweeted_status", {"text": 5, "user": "x"}), ("retweeted_status", {"full_text": "hi", "entities": []}),
])
def test_unexpected_field_types_do_not_crash(tweet_results, field, value):
    """X changes payloads without notice; wrong types degrade, never crash."""
    data = dict(tweet_results["tweet"], **{field: value})
    t = parse_tweet_result(data)
    assert t.id == "1834231234567890123"


def test_non_object_payload_is_parse_error():
    for bad in ("str", 5):
        with pytest.raises(ParseError):
            parse_tweet_result(bad)


def test_timeline_with_malformed_entries(timeline_html):
    import json as _json
    page = ('<script id="__NEXT_DATA__" type="application/json">'
            + _json.dumps({"props": {"pageProps": {"timeline": {"entries": [
                None, "x", {"type": "tweet", "content": "nope"},
                {"type": "tweet", "content": {"tweet": {"id_str": "9", "full_text": "ok"}}}]}}}})
            + "</script>")
    assert [t.id for t in parse_timeline_page(page)] == ["9"]


@pytest.mark.parametrize("typename", [["Tweet"], {"name": "Tweet"}, ["TweetTombstone"]])
def test_non_string_typename_is_parse_error(typename):
    # The shape the QA fuzzer found: it used to raise TypeError (unhashable)
    # and abort whole batches. It must not be stored as a blank tweet either.
    with pytest.raises(ParseError, match="__typename"):
        parse_tweet_result({"__typename": typename, "id_str": "900"})
