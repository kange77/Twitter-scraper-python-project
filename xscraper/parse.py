"""Turn X's syndication payloads into ``Tweet`` objects.

Two payload shapes are handled:

* the embed endpoint (``cdn.syndication.twimg.com/tweet-result``), which uses
  ``mediaDetails``, ``conversation_count`` and ISO timestamps;
* the profile-timeline widget, whose tweets are embedded as v1.1-style JSON
  (``full_text``, ``extended_entities``, ``Wed Oct 10 20:19:24 +0000 2018``)
  inside the page's ``__NEXT_DATA__`` script.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .models import Media, Tweet, User

_NEXT_DATA = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL
)
_REPLY_PREFIX = re.compile(r"^(?:\s*@\w{1,15})+\s*$")
# Payloads for tweets that exist but can't be shown (deleted, protected, suspended).
_UNAVAILABLE_TYPES = frozenset({"TweetTombstone", "TweetUnavailable"})


class ParseError(ValueError):
    pass


class EmptyTimelineShell(ParseError):
    """X sent the profile widget's empty shell instead of a timeline.

    Seen live on 2026-10-07: to a request without cookies the widget page said
    the profile has tweets (``contextProvider.hasResults``) but listed no
    entries; the same request 25 minutes later got 20. It's intermittent, so
    callers treat it as a failed attempt that may be retried, not as an empty
    timeline.
    """


def _d(value: Any) -> dict:
    """``value`` if it's a dict, else {}; payload fields can change type without notice."""
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _s(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_date(value: Any) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value.replace("Z", "+0000"), fmt)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return value


def _user(u: dict) -> User:
    return User(
        id=_s(u.get("id_str")) or _s(u.get("id")) or "",
        screen_name=_s(u.get("screen_name")) or "",
        name=_s(u.get("name")) or "",
        verified=bool(u.get("verified") or u.get("is_blue_verified")),
        profile_image_url=_s(u.get("profile_image_url_https")),
        followers_count=_int(u.get("followers_count")),
    )


def _best_video(media: dict) -> Optional[str]:
    variants = _l(_d(media.get("video_info")).get("variants"))
    mp4 = [v for v in variants
           if isinstance(v, dict) and v.get("content_type") == "video/mp4" and _s(v.get("url"))]
    if not mp4:
        return None
    return max(mp4, key=lambda v: _int(v.get("bitrate")) or 0)["url"]


def _media(t: dict) -> list[Media]:
    items = (
        _l(t.get("mediaDetails"))
        or _l(_d(t.get("extended_entities")).get("media"))
        or _l(_d(t.get("entities")).get("media"))
    )
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        info = _d(m.get("original_info"))
        out.append(Media(
            type=_s(m.get("type")) or "photo",
            url=_s(m.get("media_url_https")) or _s(m.get("media_url")) or "",
            width=_int(info.get("width")),
            height=_int(info.get("height")),
            video_url=_best_video(m),
        ))
    return out


def _entity_values(entities: dict, kind: str, field: str) -> list[str]:
    return [v for e in _l(entities.get(kind)) if isinstance(e, dict) and (v := _s(e.get(field)))]


def _expand_text(t: dict) -> str:
    """Tweet text with t.co links expanded and the trailing media link removed."""
    text = _s(t.get("full_text")) or _s(t.get("text")) or ""
    # Replies start with the @mentions of the people being replied to; X hides
    # them by starting display_text_range after them.
    start = next(iter(_l(t.get("display_text_range"))), None)
    start = _int(start) if start is not None else None
    if start and 0 < start <= len(text) and _REPLY_PREFIX.match(text[:start]):
        text = text[start:]
    entities = _d(t.get("entities"))
    for u in _l(entities.get("urls")):
        short, full = _s(_d(u).get("url")), _s(_d(u).get("expanded_url"))
        if short and full:
            text = text.replace(short, full)
    media_entities = _l(_d(t.get("extended_entities")).get("media")) or _l(entities.get("media"))
    for m in media_entities:
        short = _s(_d(m).get("url"))
        if short:
            text = text.replace(short, "")
    return html.unescape(text).strip()


def parse_tweet(t: Any, source: str = "") -> Tweet:
    """Parse one tweet object from either syndication payload shape."""
    tweet_id = (_s(t.get("id_str")) or _s(t.get("id"))) if isinstance(t, dict) else None
    if not tweet_id:
        raise ParseError("not a tweet object")
    entities = _d(t.get("entities"))
    text = _expand_text(t)
    hashtags = _entity_values(entities, "hashtags", "text")
    mentions = _entity_values(entities, "user_mentions", "screen_name")
    urls = _entity_values(entities, "urls", "expanded_url")
    retweeted = _d(t.get("retweeted_status"))
    if _s(retweeted.get("id_str")) and (_s(retweeted.get("full_text")) or _s(retweeted.get("text"))):
        # A retweet's own text is "RT @user: " plus the original cut to 140
        # characters, so take the text and entities from the original.
        rt_entities = _d(retweeted.get("entities"))
        author = _s(_d(retweeted.get("user")).get("screen_name"))
        text = f"RT @{author}: {_expand_text(retweeted)}" if author else _expand_text(retweeted)
        hashtags = _entity_values(rt_entities, "hashtags", "text")
        orig_mentions = _entity_values(rt_entities, "user_mentions", "screen_name")
        mentions = ([author] if author and author not in orig_mentions else []) + orig_mentions
        urls = _entity_values(rt_entities, "urls", "expanded_url")
    return Tweet(
        id=tweet_id,
        text=text,
        created_at=normalize_date(t.get("created_at")),
        user=_user(_d(t.get("user"))),
        lang=_s(t.get("lang")),
        like_count=_int(t.get("favorite_count")),
        retweet_count=_int(t.get("retweet_count")),
        reply_count=_int(t.get("reply_count", t.get("conversation_count"))),
        quote_count=_int(t.get("quote_count")),
        hashtags=hashtags,
        mentions=mentions,
        urls=urls,
        media=_media(t),
        in_reply_to_id=_s(t.get("in_reply_to_status_id_str")) or _s(_d(t.get("parent")).get("id_str")),
        quoted_tweet_id=_s(t.get("quoted_status_id_str")) or _s(_d(t.get("quoted_tweet")).get("id_str")),
        retweeted_tweet_id=_s(_d(t.get("retweeted_status")).get("id_str")),
        source=source,
    )


def parse_tweet_result(data: Any) -> Optional[Tweet]:
    """Parse the embed endpoint's JSON. Returns None for deleted/withheld/protected tweets.

    Only an explicit unavailable type or a tombstone means "unavailable". An
    empty payload (``{}``, ``[]``, ``null``) says nothing about the tweet, so
    it is an error that may be retried, never evidence of a deletion.
    """
    if not data:
        raise ParseError(f"empty tweet-result payload: {data!r}")
    if not isinstance(data, dict):
        raise ParseError(f"unexpected tweet-result payload: {type(data).__name__}")
    typename = data.get("__typename")
    if typename is not None and not isinstance(typename, str):
        # Seen as a list or dict in fuzzed payloads. We can't tell whether this
        # is a tweet or a tombstone, so fail it rather than store a blank tweet.
        raise ParseError(f"unexpected __typename in tweet-result payload: {type(typename).__name__}")
    if typename in _UNAVAILABLE_TYPES or "tombstone" in data:
        return None
    return parse_tweet(data, source="syndication-tweet")


def extract_next_data(page: str) -> dict:
    m = _NEXT_DATA.search(page)
    if not m:
        raise ParseError("page has no __NEXT_DATA__ payload (layout changed or request blocked)")
    try:
        return json.loads(m.group("json"))
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid __NEXT_DATA__ JSON: {exc}") from exc


def parse_timeline_page(page: str, logged_in: bool = False) -> list[Tweet]:
    """Parse the profile-timeline widget HTML into tweets (newest first).

    The widget lists a pinned tweet first whatever its age, so entries are
    re-sorted by ID, which X assigns in time order. Raises ``EmptyTimelineShell``
    when a request sent without cookies (``logged_in=False``) got the empty
    shell, so callers don't report it as an empty timeline.
    """
    data = extract_next_data(page)
    props = _d(_d(_d(data).get("props")).get("pageProps"))
    entries = _l(_d(props.get("timeline")).get("entries"))
    if (not entries and not logged_in
            and _d(props.get("contextProvider")).get("hasResults") is True):
        raise EmptyTimelineShell(
            "X returned an empty timeline page, which it does intermittently for logged-out "
            "clients; retry later, or pass your own session cookies with --cookies or "
            "$XSCRAPER_COOKIES for reliable results")
    tweets = []
    for entry in entries:
        if _d(entry).get("type") != "tweet":
            continue
        raw = _d(entry.get("content")).get("tweet")
        try:
            tweets.append(parse_tweet(raw, source="syndication-timeline"))
        except ParseError:
            continue
    tweets.sort(key=lambda t: int(t.id) if t.id.isdigit() else 0, reverse=True)
    return tweets
