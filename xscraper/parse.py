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


class ParseError(ValueError):
    pass


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
    return Tweet(
        id=tweet_id,
        text=_expand_text(t),
        created_at=normalize_date(t.get("created_at")),
        user=_user(_d(t.get("user"))),
        lang=_s(t.get("lang")),
        like_count=_int(t.get("favorite_count")),
        retweet_count=_int(t.get("retweet_count")),
        reply_count=_int(t.get("reply_count", t.get("conversation_count"))),
        quote_count=_int(t.get("quote_count")),
        hashtags=_entity_values(entities, "hashtags", "text"),
        mentions=_entity_values(entities, "user_mentions", "screen_name"),
        urls=_entity_values(entities, "urls", "expanded_url"),
        media=_media(t),
        in_reply_to_id=_s(t.get("in_reply_to_status_id_str")) or _s(_d(t.get("parent")).get("id_str")),
        quoted_tweet_id=_s(t.get("quoted_status_id_str")) or _s(_d(t.get("quoted_tweet")).get("id_str")),
        retweeted_tweet_id=_s(_d(t.get("retweeted_status")).get("id_str")),
        source=source,
    )


def parse_tweet_result(data: Any) -> Optional[Tweet]:
    """Parse the embed endpoint's JSON. Returns None for deleted/withheld tweets."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise ParseError(f"unexpected tweet-result payload: {type(data).__name__}")
    if data.get("__typename") == "TweetTombstone" or "tombstone" in data:
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


def parse_timeline_page(page: str) -> list[Tweet]:
    """Parse the profile-timeline widget HTML into tweets (newest first)."""
    data = extract_next_data(page)
    timeline = _d(_d(_d(data).get("props")).get("pageProps")).get("timeline")
    tweets = []
    for entry in _l(_d(timeline).get("entries")):
        if _d(entry).get("type") != "tweet":
            continue
        raw = _d(entry.get("content")).get("tweet")
        try:
            tweets.append(parse_tweet(raw, source="syndication-timeline"))
        except ParseError:
            continue
    return tweets
