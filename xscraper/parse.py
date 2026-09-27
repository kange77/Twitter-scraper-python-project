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


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_date(value: Optional[str]) -> Optional[str]:
    if not value:
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
        id=str(u.get("id_str") or u.get("id") or ""),
        screen_name=u.get("screen_name", ""),
        name=u.get("name", ""),
        verified=bool(u.get("verified") or u.get("is_blue_verified")),
        profile_image_url=u.get("profile_image_url_https"),
        followers_count=_int(u.get("followers_count")),
    )


def _best_video(media: dict) -> Optional[str]:
    variants = (media.get("video_info") or {}).get("variants") or []
    mp4 = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
    if not mp4:
        return None
    return max(mp4, key=lambda v: v.get("bitrate") or 0)["url"]


def _media(t: dict) -> list[Media]:
    items = (
        t.get("mediaDetails")
        or (t.get("extended_entities") or {}).get("media")
        or (t.get("entities") or {}).get("media")
        or []
    )
    out = []
    for m in items:
        info = m.get("original_info") or {}
        out.append(Media(
            type=m.get("type", "photo"),
            url=m.get("media_url_https") or m.get("media_url") or "",
            width=_int(info.get("width")),
            height=_int(info.get("height")),
            video_url=_best_video(m),
        ))
    return out


def _expand_text(t: dict) -> str:
    """Tweet text with t.co links expanded and the trailing media link removed."""
    text = t.get("full_text") or t.get("text") or ""
    entities = t.get("entities") or {}
    for u in entities.get("urls") or []:
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    media_entities = (t.get("extended_entities") or {}).get("media") or entities.get("media") or []
    for m in media_entities:
        if m.get("url"):
            text = text.replace(m["url"], "")
    return html.unescape(text).strip()


def parse_tweet(t: dict, source: str = "") -> Tweet:
    """Parse one tweet object from either syndication payload shape."""
    if not isinstance(t, dict) or not (t.get("id_str") or t.get("id")):
        raise ParseError("not a tweet object")
    entities = t.get("entities") or {}
    retweeted = t.get("retweeted_status")
    quoted_id = t.get("quoted_status_id_str") or (t.get("quoted_tweet") or {}).get("id_str")
    return Tweet(
        id=str(t.get("id_str") or t["id"]),
        text=_expand_text(t),
        created_at=normalize_date(t.get("created_at")),
        user=_user(t.get("user") or {}),
        lang=t.get("lang"),
        like_count=_int(t.get("favorite_count")),
        retweet_count=_int(t.get("retweet_count")),
        reply_count=_int(t.get("reply_count", t.get("conversation_count"))),
        quote_count=_int(t.get("quote_count")),
        hashtags=[h["text"] for h in entities.get("hashtags") or [] if h.get("text")],
        mentions=[m["screen_name"] for m in entities.get("user_mentions") or [] if m.get("screen_name")],
        urls=[u["expanded_url"] for u in entities.get("urls") or [] if u.get("expanded_url")],
        media=_media(t),
        in_reply_to_id=t.get("in_reply_to_status_id_str") or (t.get("parent") or {}).get("id_str"),
        quoted_tweet_id=quoted_id,
        retweeted_tweet_id=(retweeted or {}).get("id_str"),
        source=source,
    )


def parse_tweet_result(data: dict) -> Optional[Tweet]:
    """Parse the embed endpoint's JSON. Returns None for deleted/withheld tweets."""
    if not data or data.get("__typename") == "TweetTombstone" or "tombstone" in data:
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
    timeline = ((data.get("props") or {}).get("pageProps") or {}).get("timeline") or {}
    tweets = []
    for entry in timeline.get("entries") or []:
        if entry.get("type") != "tweet":
            continue
        raw = (entry.get("content") or {}).get("tweet")
        try:
            tweets.append(parse_tweet(raw, source="syndication-timeline"))
        except ParseError:
            continue
    return tweets
