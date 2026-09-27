"""Scrape public tweets through X's syndication (embed) endpoints.

These are the endpoints that power embedded tweets and profile widgets on
third-party sites. They serve public data without logging in, which makes
them far more stable than scraping x.com's JavaScript app, but coverage is
narrower: the profile widget returns a recent slice of a timeline, not the
full history, and X may restrict it at any time.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Iterator, Optional

from .http import HttpClient, HttpError, NotFound, loads
from .models import Tweet
from .parse import ParseError, parse_timeline_page, parse_tweet_result
from .token import syndication_token

log = logging.getLogger(__name__)

TWEET_ENDPOINT = "https://cdn.syndication.twimg.com/tweet-result"
TIMELINE_ENDPOINT = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{}"

_STATUS_URL = re.compile(r"(?:twitter\.com|x\.com)/(?:[^/]+/status(?:es)?|i/web/status)/(\d+)", re.I)
_PROFILE_URL = re.compile(r"(?:twitter\.com|x\.com)/@?([A-Za-z0-9_]{1,15})(?:[/?#]|$)", re.I)
_SCREEN_NAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def parse_tweet_id(value: str | int) -> str:
    """Accept a numeric ID or any x.com / twitter.com status URL."""
    s = str(value).strip()
    if s.isdigit():
        return s
    m = _STATUS_URL.search(s)
    if m:
        return m.group(1)
    raise ValueError(f"not a tweet ID or status URL: {value!r}")


def parse_screen_name(value: str) -> str:
    """Accept ``name``, ``@name`` or a profile URL."""
    s = value.strip()
    m = _PROFILE_URL.search(s)
    if m:
        s = m.group(1)
    s = s.lstrip("@")
    if not _SCREEN_NAME.match(s):
        raise ValueError(f"not a valid screen name: {value!r}")
    return s


def tweet_params(tweet_id: str, lang: str) -> dict:
    return {"id": tweet_id, "lang": lang, "token": syndication_token(tweet_id)}


def tweet_from_body(tweet_id: str, body: bytes) -> Optional[Tweet]:
    """Parse a tweet-result response body; None when the tweet is unavailable."""
    if not body.strip():
        return None
    try:
        data = loads(body)
    except ValueError as exc:
        raise ParseError(f"tweet {tweet_id}: response was not JSON "
                         "(request blocked or endpoint changed)") from exc
    return parse_tweet_result(data)


def timeline_from_page(page: str, include_retweets: bool) -> list[Tweet]:
    tweets = parse_timeline_page(page)
    if not include_retweets:
        tweets = [t for t in tweets if not t.is_retweet]
    return tweets


class Scraper:
    def __init__(self, client: Optional[HttpClient] = None, lang: str = "en", workers: int = 4):
        self.client = client or HttpClient()
        self.lang = lang
        self.workers = max(1, workers)

    def tweet(self, tweet: str | int) -> Optional[Tweet]:
        """Fetch one tweet by ID or URL. Returns None if it's deleted or private."""
        tweet_id = parse_tweet_id(tweet)
        try:
            resp = self.client.get(TWEET_ENDPOINT, params=tweet_params(tweet_id, self.lang))
        except NotFound:
            return None
        return tweet_from_body(tweet_id, resp.content)

    def tweets(self, tweets: Iterable[str | int], return_exceptions: bool = False) -> list:
        """Fetch many tweets concurrently (still within the client's rate limit).

        Results are in input order; missing tweets are None. With
        ``return_exceptions=True`` a tweet that fails to fetch or parse gets its
        exception in the result list instead of aborting the whole batch.
        """
        ids = [parse_tweet_id(t) for t in tweets]
        return list(self.iter_tweets(ids, return_exceptions))

    def iter_tweets(self, tweets: Iterable[str | int], return_exceptions: bool = False,
                    window: Optional[int] = None) -> Iterator:
        """Like ``tweets`` but yields results in input order as they arrive.

        At most ``window`` (default ``4 * workers``) tweets are in flight or
        waiting to be yielded, so memory stays flat however long ``tweets``
        is, and ``tweets`` can be a lazy iterable such as an open file.
        """
        def fetch(tweet_id: str):
            try:
                return self.tweet(tweet_id)
            except (HttpError, ParseError) as exc:
                if not return_exceptions:
                    raise
                return exc

        if self.workers == 1:
            for t in tweets:
                yield fetch(parse_tweet_id(t))
            return
        window = max(1, window or 4 * self.workers)
        pool = ThreadPoolExecutor(max_workers=self.workers)
        pending: deque = deque()
        try:
            for t in tweets:
                pending.append(pool.submit(fetch, parse_tweet_id(t)))
                if len(pending) >= window:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def thread(self, tweet: str | int, max_depth: int = 50) -> list[Tweet]:
        """The reply chain leading up to (and including) a tweet, oldest first."""
        chain: list[Tweet] = []
        seen: set[str] = set()
        next_id: Optional[str] = parse_tweet_id(tweet)
        while next_id and next_id not in seen and len(chain) < max_depth:
            seen.add(next_id)
            try:
                current = self.tweet(next_id)
            except (HttpError, ParseError) as exc:
                if not chain:
                    raise
                # Keep what we have rather than losing the whole thread.
                log.warning("stopping thread at %s: %s", next_id, exc)
                break
            if current is None:
                break
            chain.append(current)
            next_id = current.in_reply_to_id
        return list(reversed(chain))

    def user_timeline(self, screen_name: str, include_retweets: bool = True) -> list[Tweet]:
        """Recent tweets from a profile, newest first."""
        name = parse_screen_name(screen_name)
        resp = self.client.get(TIMELINE_ENDPOINT.format(name), params={"showReplies": "true"})
        return timeline_from_page(resp.text, include_retweets)

    def user_timelines(self, screen_names: Iterable[str], include_retweets: bool = True) -> list:
        """Timelines for several profiles fetched concurrently, in input order.

        Each entry is a list of tweets, or the exception that profile raised.
        """
        def fetch(name: str):
            try:
                return self.user_timeline(name, include_retweets)
            except (HttpError, ParseError, ValueError) as exc:
                return exc

        names = list(screen_names)
        if len(names) <= 1 or self.workers == 1:
            return [fetch(n) for n in names]
        with ThreadPoolExecutor(max_workers=min(self.workers, len(names))) as pool:
            return list(pool.map(fetch, names))
