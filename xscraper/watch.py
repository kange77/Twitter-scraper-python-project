"""Continuous monitoring: re-scrape targets on a schedule and emit change events.

A watch keeps its state in a SQLite file (which is also an ordinary tweet
store) so it can be stopped and restarted at any time:

* ``targets``: profiles and tweets to poll, each with its own interval and
  next due time.
* ``tracked``: the last known state of every tweet seen, used to tell new
  tweets from edits and engagement changes. Tweets can be re-checked by ID
  for a while after they first appear (``track_for``), which is the only
  way to notice a deletion: the profile widget shows a recent slice, so a
  tweet dropping off it proves nothing.
* ``snapshots``: an engagement time series (likes, retweets, replies,
  quotes) with a row whenever a count changes.
* ``events``: an outbox of ``new`` / ``edited`` / ``deleted`` / ``engagement``
  events. Sinks (a JSON Lines file, a webhook) read from it; webhook
  delivery is marked per event, so a receiver that is down gets the backlog
  when it comes back instead of losing it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional

from .http import HttpError, NotFound
from .metrics import anomalies
from .models import Tweet
from .parse import ParseError
from .storage import TweetStore

log = logging.getLogger(__name__)

EVENT_TYPES = ("new", "edited", "deleted", "restored", "engagement")
COUNTS = ("like_count", "retweet_count", "reply_count", "quote_count")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    every REAL NOT NULL,
    next_due REAL NOT NULL DEFAULT 0,
    last_run REAL,
    last_error TEXT,
    PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS tracked (
    tweet_id TEXT PRIMARY KEY,
    screen_name TEXT,
    first_seen REAL NOT NULL,
    track_until REAL NOT NULL,
    last_checked REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'live',
    text_hash TEXT,
    text TEXT,
    like_count INTEGER, retweet_count INTEGER, reply_count INTEGER, quote_count INTEGER,
    baseline TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
    tweet_id TEXT NOT NULL,
    ts REAL NOT NULL,
    like_count INTEGER, retweet_count INTEGER, reply_count INTEGER, quote_count INTEGER);
CREATE INDEX IF NOT EXISTS snapshots_tweet ON snapshots(tweet_id, ts);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    tweet_id TEXT,
    screen_name TEXT,
    data TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0);
"""

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)


def parse_duration(value: str) -> float:
    """``90``, ``90s``, ``15m``, ``2h``, ``1d`` → seconds."""
    m = _DURATION.match(str(value))
    if not m:
        raise ValueError(f"not a duration: {value!r} (use e.g. 90s, 15m, 2h, 1d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass
class Event:
    seq: int
    ts: float
    type: str
    tweet_id: Optional[str]
    screen_name: Optional[str]
    data: dict

    def to_dict(self) -> dict:
        d = {"seq": self.seq, "time": _iso(self.ts), "type": self.type, "tweet_id": self.tweet_id,
             "screen_name": self.screen_name}
        if self.tweet_id and self.screen_name:
            d["url"] = f"https://x.com/{self.screen_name}/status/{self.tweet_id}"
        d.update(self.data)
        return d


class WatchStore:
    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            raise ValueError(f"{path} is not an xscraper watch file ({exc})") from exc
        self.tweets = TweetStore(self.path, timeout=30.0)

    def close(self) -> None:
        self.tweets.close()
        self.conn.close()

    def __enter__(self) -> "WatchStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- targets ---------------------------------------------------------

    def add_target(self, kind: str, key: str, every: float) -> None:
        """Watch ``key`` every ``every`` seconds (updates the interval if already watched)."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO targets (kind, key, every) VALUES (?, ?, ?) "
                "ON CONFLICT (kind, key) DO UPDATE SET every = excluded.every", (kind, key, every))
            if kind == "tweet":
                # Explicitly watched tweets are tracked for as long as they are watched.
                self.conn.execute(
                    "INSERT INTO tracked (tweet_id, first_seen, track_until, last_checked) VALUES (?, ?, ?, 0) "
                    "ON CONFLICT (tweet_id) DO UPDATE SET track_until = excluded.track_until",
                    (key, self._clock(), float("inf")))

    def remove_target(self, kind: str, key: str) -> bool:
        with self.conn:
            gone = self.conn.execute("DELETE FROM targets WHERE kind = ? AND key = ?", (kind, key)).rowcount
            if kind == "tweet":
                self.conn.execute("UPDATE tracked SET track_until = ? WHERE tweet_id = ?", (self._clock(), key))
        return bool(gone)

    def targets(self) -> list[tuple[str, str, float, float, Optional[str]]]:
        return self.conn.execute(
            "SELECT kind, key, every, next_due, last_error FROM targets ORDER BY kind, key").fetchall()

    def due(self, now: Optional[float] = None) -> list[tuple[str, str, float]]:
        now = self._clock() if now is None else now
        return self.conn.execute("SELECT kind, key, every FROM targets WHERE next_due <= ? ORDER BY next_due",
                                 (now,)).fetchall()

    def mark_run(self, kind: str, key: str, every: float, error: Optional[str] = None) -> None:
        now = self._clock()
        with self.conn:
            self.conn.execute("UPDATE targets SET last_run = ?, next_due = ?, last_error = ? "
                              "WHERE kind = ? AND key = ?", (now, now + every, error, kind, key))

    def next_due(self) -> Optional[float]:
        return self.conn.execute("SELECT MIN(next_due) FROM targets").fetchone()[0]

    # -- tracked tweets --------------------------------------------------

    def recheck_due(self, recheck: float, limit: int = 1000) -> list[str]:
        """Tweets still being tracked that haven't been checked for ``recheck`` seconds.

        Explicitly watched tweets are polled through their own target, not here.
        """
        now = self._clock()
        return [r[0] for r in self.conn.execute(
            "SELECT tweet_id FROM tracked WHERE state = 'live' AND track_until > ? AND last_checked <= ? "
            "AND tweet_id NOT IN (SELECT key FROM targets WHERE kind = 'tweet') "
            "ORDER BY last_checked LIMIT ?", (now, now - recheck, limit))]

    def observe(self, tweets: Iterable[Tweet], track_for: float = 0.0,
                engagement_change: Optional[float] = None) -> list[Event]:
        """Compare fetched tweets with what's known; store them and return the events."""
        tweets = list(tweets)
        if not tweets:
            return []
        self.tweets.upsert(tweets)
        now = self._clock()
        events: list[tuple] = []
        with self.conn:
            for t in tweets:
                row = self.conn.execute(
                    "SELECT text_hash, text, state, like_count, retweet_count, reply_count, quote_count, "
                    "first_seen, track_until, baseline FROM tracked WHERE tweet_id = ?", (t.id,)).fetchone()
                counts = [getattr(t, c) for c in COUNTS]
                digest = _text_hash(t.text)
                name = t.user.screen_name
                if row is None or row[0] is None:
                    # First sighting (an explicitly watched tweet has a row but no content yet).
                    events.append((now, "new", t.id, name, {"tweet": t.to_dict()}))
                    track_until = now + track_for if row is None else row[8]
                    self.conn.execute(
                        "INSERT OR REPLACE INTO tracked (tweet_id, screen_name, first_seen, track_until, "
                        "last_checked, state, text_hash, text, like_count, retweet_count, reply_count, "
                        "quote_count, baseline) VALUES (?, ?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, ?, ?)",
                        (t.id, name, now if row is None else row[7], track_until, now, digest, t.text, *counts,
                         json.dumps(counts)))
                    self.conn.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)", (t.id, now, *counts))
                    continue
                old_hash, old_text, state = row[0], row[1], row[2]
                old_counts = list(row[3:7])
                text = t.text
                if not text and old_text:
                    # A payload without text (the parser degrades it to "") isn't an
                    # edit to an empty tweet; keep the last text, like missing counts.
                    text, digest = old_text, old_hash
                if digest != old_hash:
                    events.append((now, "edited", t.id, name, {"old_text": old_text, "text": text}))
                if state == "deleted":
                    events.append((now, "restored", t.id, name, {}))
                # Counts X didn't report this time (None) keep their last known value.
                merged = [new if new is not None else old for new, old in zip(counts, old_counts)]
                baseline = json.loads(row[9]) if row[9] else old_counts
                if merged != old_counts:
                    self.conn.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)", (t.id, now, *merged))
                    if engagement_change is not None:
                        # Measured from the last reported level, so slow steady growth
                        # still produces an event once it adds up.
                        change = _engagement_change(baseline, merged, engagement_change)
                        if change:
                            events.append((now, "engagement", t.id, name, {"change": change}))
                            baseline = merged
                self.conn.execute(
                    "UPDATE tracked SET last_checked = ?, state = 'live', text_hash = ?, text = ?, like_count = ?, "
                    "retweet_count = ?, reply_count = ?, quote_count = ?, screen_name = ?, baseline = ? "
                    "WHERE tweet_id = ?", (now, digest, text, *merged, name, json.dumps(baseline), t.id))
            return self._record(events)

    def observe_missing(self, tweet_ids: Iterable[str]) -> list[Event]:
        """Tweets that came back deleted/withheld when re-checked by ID."""
        now = self._clock()
        events = []
        with self.conn:
            for tid in tweet_ids:
                row = self.conn.execute("SELECT state, screen_name, text FROM tracked WHERE tweet_id = ?",
                                        (tid,)).fetchone()
                if row is None or row[0] == "deleted":
                    continue
                self.conn.execute("UPDATE tracked SET state = 'deleted', last_checked = ? WHERE tweet_id = ?",
                                  (now, tid))
                if row[2] is not None:  # only a tweet we had seen can be reported deleted
                    events.append((now, "deleted", tid, row[1], {"last_text": row[2]}))
            return self._record(events)

    def touch(self, tweet_ids: Iterable[str]) -> None:
        """Mark tweets as checked (e.g. after a failed re-check) so they rotate to the back."""
        with self.conn:
            self.conn.executemany("UPDATE tracked SET last_checked = ? WHERE tweet_id = ?",
                                  [(self._clock(), t) for t in tweet_ids])

    def _record(self, events: list[tuple]) -> list[Event]:
        out = []
        for ts, type_, tid, name, data in events:
            cur = self.conn.execute(
                "INSERT INTO events (ts, type, tweet_id, screen_name, data) VALUES (?, ?, ?, ?, ?)",
                (ts, type_, tid, name, json.dumps(data, ensure_ascii=False)))
            out.append(Event(cur.lastrowid, ts, type_, tid, name, data))
        return out

    # -- reading ---------------------------------------------------------

    def events(self, after: int = 0, undelivered: bool = False, limit: int = 1000) -> list[Event]:
        q = "SELECT seq, ts, type, tweet_id, screen_name, data FROM events WHERE seq > ?"
        if undelivered:
            q += " AND delivered = 0"
        rows = self.conn.execute(q + " ORDER BY seq LIMIT ?", (after, limit)).fetchall()
        return [Event(r[0], r[1], r[2], r[3], r[4], json.loads(r[5])) for r in rows]

    def mark_delivered(self, seqs: Iterable[int]) -> None:
        with self.conn:
            self.conn.executemany("UPDATE events SET delivered = 1 WHERE seq = ?", [(s,) for s in seqs])

    def history(self, tweet_id: str) -> list[tuple]:
        return self.conn.execute(
            "SELECT ts, like_count, retweet_count, reply_count, quote_count FROM snapshots "
            "WHERE tweet_id = ? ORDER BY ts", (tweet_id,)).fetchall()

    def summary(self) -> dict:
        tracked = dict(self.conn.execute("SELECT state, COUNT(*) FROM tracked GROUP BY state"))
        events = dict(self.conn.execute("SELECT type, COUNT(*) FROM events GROUP BY type"))
        pending = self.conn.execute("SELECT COUNT(*) FROM events WHERE delivered = 0").fetchone()[0]
        return {"targets": len(self.targets()), "tracked": tracked, "events": events,
                "undelivered": pending, "snapshots":
                    self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]}


def _engagement_change(old: list, new: list, threshold: float) -> Optional[dict]:
    """Counts that moved by at least ``threshold`` (a fraction, e.g. 0.1) from ``old``."""
    change = {}
    for name, a, b in zip(COUNTS, old, new):
        if a is None or b is None or a == b:
            continue
        if (a == 0 and b > 0) or (a and abs(b - a) / a >= threshold):
            change[name] = {"from": a, "to": b}
    return change or None


# -- sinks -----------------------------------------------------------------

class JsonlSink:
    """Appends events to a JSON Lines file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def send(self, events: list[Event]) -> None:
        if events:
            with self.path.open("a", encoding="utf-8") as f:
                f.write("".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in events))


def _post_json(url: str, payload: dict, timeout: float) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "xscraper-watch"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


class WebhookSink:
    """POSTs ``{"events": [...]}`` batches; the store's outbox tracks what was delivered."""

    outbox = True

    def __init__(self, url: str, timeout: float = 10.0, batch: int = 100,
                 post: Callable[[str, dict, float], int] = _post_json):
        self.url = url
        self.timeout = timeout
        self.batch = batch
        self._post = post

    async def deliver(self, store: WatchStore) -> int:
        """Send undelivered events in order; stops at the first failure. Returns events sent."""
        sent = 0
        while True:
            events = store.events(undelivered=True, limit=self.batch)
            if not events:
                return sent
            try:
                status = await asyncio.to_thread(self._post, self.url,
                                                 {"events": [e.to_dict() for e in events]}, self.timeout)
            except Exception as exc:  # network errors, HTTP errors: try again next cycle
                log.warning("webhook %s failed (%s); %d events will be retried", self.url, exc, len(events))
                return sent
            if not 200 <= status < 300:
                log.warning("webhook %s answered %s; %d events will be retried", self.url, status, len(events))
                return sent
            store.mark_delivered(e.seq for e in events)
            sent += len(events)


# -- the loop ----------------------------------------------------------------

class Watcher:
    def __init__(self, store: WatchStore, fetch_tweet: Callable[[str], Awaitable[Optional[Tweet]]],
                 fetch_user: Callable[[str], Awaitable[list[Tweet]]], *, sinks=(),
                 track_for: float = 0.0, recheck: Optional[float] = None,
                 engagement_change: Optional[float] = None, concurrency: int = 8, observer=None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.store = store
        self.fetch_tweet = fetch_tweet
        self.fetch_user = fetch_user
        self.sinks = list(sinks)
        self.track_for = track_for
        self.recheck = recheck
        self.engagement_change = engagement_change
        self.concurrency = max(1, concurrency)
        self.observer = observer
        self._sleep = sleep
        self._stopping = False
        self.cycles = 0

    def stop(self) -> None:
        self._stopping = True

    async def _bounded(self, coros):
        slots = asyncio.Semaphore(self.concurrency)

        async def run(c):
            async with slots:
                try:
                    return await c
                except (HttpError, ParseError, ValueError) as exc:  # NotFound included
                    return exc
                except Exception as exc:
                    # One target's unexpected error must not abort the cycle for
                    # every other target (see Crawler._work); it is recorded as
                    # that target's error and the traceback is logged.
                    log.warning("unexpected error while polling", exc_info=True)
                    return exc
        return await asyncio.gather(*(run(c) for c in coros))

    async def cycle(self) -> list[Event]:
        """Poll every due target (and due re-checks) once; returns the new events."""
        store = self.store
        events: list[Event] = []
        due = store.due()
        results = await self._bounded(
            [self.fetch_user(key) if kind == "user" else self.fetch_tweet(key) for kind, key, _ in due])
        missing: list[str] = []
        for (kind, key, every), result in zip(due, results):
            if isinstance(result, NotFound) or (kind == "tweet" and result is None):
                if kind == "tweet":
                    missing.append(key)
                store.mark_run(kind, key, every, None if kind == "tweet" else "not found")
                continue
            if isinstance(result, Exception):
                log.warning("%s %s: %s", kind, key, result)
                store.mark_run(kind, key, every, f"{type(result).__name__}: {result}")
                continue
            tweets = result if kind == "user" else [result]
            self._check_drift(tweets)
            events += store.observe(tweets, self.track_for, self.engagement_change)
            store.mark_run(kind, key, every)
        if self.recheck is not None:
            ids = store.recheck_due(self.recheck)
            results = await self._bounded([self.fetch_tweet(t) for t in ids])
            found, failed = [], []
            for tid, result in zip(ids, results):
                if result is None or isinstance(result, NotFound):
                    missing.append(tid)
                elif isinstance(result, Exception):
                    failed.append(tid)
                else:
                    found.append(result)
            self._check_drift(found)
            events += store.observe(found, self.track_for, self.engagement_change)
            store.touch(failed)
        events += store.observe_missing(missing)
        self.cycles += 1
        await self._emit(events)
        return events

    def _check_drift(self, tweets: list[Tweet]) -> None:
        drift = getattr(self.observer, "drift", None)
        if drift is not None:
            for t in tweets:
                drift.observe(anomalies(t))

    async def _emit(self, events: list[Event]) -> None:
        for sink in self.sinks:
            if getattr(sink, "outbox", False):
                await sink.deliver(self.store)
            else:
                await sink.send(events)
        if self.observer is not None and hasattr(self.observer, "watch_events"):
            self.observer.watch_events(events)

    async def run(self, once: bool = False, on_cycle: Optional[Callable[[list[Event]], None]] = None) -> None:
        while not self._stopping:
            events = await self.cycle()
            if on_cycle is not None:
                on_cycle(events)
            if once:
                return
            next_due = self.store.next_due()
            waits = [] if next_due is None else [next_due - time.time()]
            if self.recheck is not None:
                waits.append(self.recheck)
            wait = max(1.0, min(waits)) if waits else 60.0
            # Sleep in short steps so stop() takes effect promptly.
            end = time.monotonic() + wait
            while not self._stopping and (left := end - time.monotonic()) > 0:
                await self._sleep(min(left, 1.0))
