"""Persistent crawl jobs: a SQLite frontier that survives crashes and restarts.

A job file is an ordinary xscraper SQLite store (so ``xscraper analyze
job.db`` and ``storage.load`` work on it) with a few extra tables:

* ``frontier``: every tweet or profile the job has seen, keyed by
  (kind, key), with its state (pending, leased, done, missing, failed),
  attempts, last error and how it was discovered. Nothing is queued, or
  fetched, twice. An item's depth is the shortest path found to it so far,
  whatever order paths arrive in: when a shorter one turns up for an item
  already fetched, its stored links are expanded again without refetching,
  so retries and concurrency don't change what a depth-limited crawl
  collects.
* ``workers``: one row per running crawler with a heartbeat. Items are
  *leased* to a worker for a limited time; if the worker dies, its leases
  expire (or are released as soon as its heartbeat goes stale) and another
  worker picks the items up. That is what lets a crashed run resume, and
  several processes share one job.
* ``meta``: the job's settings (link types to follow, depth, attempts).

Everything here is synchronous and short; callers batch their updates.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .models import Tweet
from .storage import TweetStore

TWEET, USER = "tweet", "user"
FOLLOW_TYPES = ("parents", "quotes", "retweets")
STATES = ("pending", "leased", "done", "missing", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0,
    parent TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    last_error TEXT,
    updated REAL,
    PRIMARY KEY (kind, key));
CREATE INDEX IF NOT EXISTS frontier_queue ON frontier(state, depth);
CREATE INDEX IF NOT EXISTS frontier_owner ON frontier(lease_owner) WHERE state = 'leased';
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    host TEXT,
    pid INTEGER,
    started REAL,
    heartbeat REAL,
    stats TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class Item:
    kind: str
    key: str
    depth: int = 0


@dataclass
class Outcome:
    """What happened to one leased item.

    ``tweets`` is the fetched tweet (a one-element list) or a profile's
    timeline; ``missing`` means the tweet is deleted, private or withheld;
    ``error`` means the fetch failed and may be retried.
    """
    item: Item
    tweets: list[Tweet] = field(default_factory=list)
    missing: bool = False
    error: Optional[str] = None


@dataclass
class JobConfig:
    follow: tuple[str, ...] = ()
    max_depth: int = 0
    max_attempts: int = 3
    rate: float = 1.0  # requests/second for the whole job, however many processes run it

    def to_meta(self) -> dict:
        return {"follow": ",".join(self.follow), "max_depth": str(self.max_depth),
                "max_attempts": str(self.max_attempts), "rate": repr(self.rate)}

    @classmethod
    def from_meta(cls, meta: dict) -> "JobConfig":
        follow = tuple(f for f in meta.get("follow", "").split(",") if f)
        return cls(follow, int(meta.get("max_depth", 0)), int(meta.get("max_attempts", 3)),
                   float(meta.get("rate", 1.0)))


def parse_follow(value: str) -> tuple[str, ...]:
    """``parents,quotes`` / ``all`` / ``none`` → a tuple of link types."""
    value = value.strip().lower()
    if value in ("", "none"):
        return ()
    if value == "all":
        return FOLLOW_TYPES
    parts = tuple(dict.fromkeys(p.strip() for p in value.split(",") if p.strip()))
    bad = [p for p in parts if p not in FOLLOW_TYPES]
    if bad:
        raise ValueError(f"unknown link type {bad[0]!r}; choose from {', '.join(FOLLOW_TYPES)}, all, none")
    return parts


def links(tweet: Tweet, follow: Iterable[str]) -> list[str]:
    """IDs of the tweets ``tweet`` points at, for the chosen link types."""
    out = []
    for kind in follow:
        ref = {"parents": tweet.in_reply_to_id, "quotes": tweet.quoted_tweet_id,
               "retweets": tweet.retweeted_tweet_id}[kind]
        if ref and ref != tweet.id:
            out.append(ref)
    return out


class JobStore:
    """The frontier, worker registry and tweet store of one crawl job."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE),
        # so a claim can't race another process between SELECT and UPDATE.
        self.conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            raise ValueError(f"{path} is not an xscraper job ({exc})") from exc
        self.tweets = TweetStore(self.path, timeout=30.0)
        self.config = JobConfig.from_meta(self.meta())
        self.expanded = 0  # links queued by the last configure() that widened the crawl

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self.tweets.close()
        self.conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _tx(self):
        return _Transaction(self.conn)

    # -- settings --------------------------------------------------------

    def meta(self) -> dict:
        return dict(self.conn.execute("SELECT key, value FROM meta"))

    def configure(self, follow: Optional[tuple[str, ...]] = None, max_depth: Optional[int] = None,
                  max_attempts: Optional[int] = None, rate: Optional[float] = None) -> JobConfig:
        """Set whichever settings are given; the rest keep their stored values."""
        old = cfg = self.config
        if rate is not None and rate <= 0:
            raise ValueError("rate must be positive")
        cfg = JobConfig(cfg.follow if follow is None else follow,
                        cfg.max_depth if max_depth is None else max_depth,
                        cfg.max_attempts if max_attempts is None else max(1, max_attempts),
                        cfg.rate if rate is None else rate)
        self.expanded = 0
        with self._tx():
            self.conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                  cfg.to_meta().items())
            self.config = cfg
            if cfg.max_depth > old.max_depth or set(cfg.follow) - set(old.follow):
                # Links are computed when an item completes, so items finished
                # under the narrower settings must be expanded again now, or a
                # larger --depth (or a new link type) would silently do nothing.
                now = self._clock()
                done = self.conn.execute("SELECT key, depth FROM frontier WHERE kind = ? AND state = 'done' "
                                         "AND depth < ?", (TWEET, cfg.max_depth)).fetchall()
                self.expanded = self._queue([r for key, depth in done for r in self._links_of(key, depth, now)])
        return cfg

    # -- frontier --------------------------------------------------------

    def add(self, items: Iterable[Item], parent: Optional[str] = None) -> int:
        """Queue items that aren't in the job yet; returns how many were new."""
        now = self._clock()
        rows = [(i.kind, i.key, i.depth, parent, now) for i in items]
        with self._tx():
            return self._queue(rows)

    def _queue(self, rows: list[tuple]) -> int:
        """Insert (kind, key, depth, parent, updated) rows inside a transaction; returns how many were new.

        A row for a known item lowers its depth if the new path is shorter.
        If that item is already done, its links are re-expanded from the
        stored tweet at the new depth (and so on, transitively).
        """
        new = 0
        while rows:
            more = []
            for kind, key, depth, parent, now in rows:
                old = self.conn.execute("SELECT state, depth FROM frontier WHERE kind = ? AND key = ?",
                                        (kind, key)).fetchone()
                if old is None:
                    self.conn.execute("INSERT INTO frontier (kind, key, depth, parent, updated) "
                                      "VALUES (?, ?, ?, ?, ?)", (kind, key, depth, parent, now))
                    new += 1
                elif depth < old[1]:
                    self.conn.execute("UPDATE frontier SET depth = ?, parent = ? WHERE kind = ? AND key = ?",
                                      (depth, parent, kind, key))
                    if old[0] == "done" and kind == TWEET:
                        more += self._links_of(key, depth, now)
            rows = more
        return new

    def _links_of(self, key: str, depth: int, now: float) -> list[tuple]:
        """Frontier rows for the links of stored tweet ``key`` sitting at ``depth``."""
        cfg = self.config
        if depth + 1 > cfg.max_depth or not cfg.follow:
            return []
        row = self.conn.execute("SELECT data FROM tweets WHERE id = ?", (key,)).fetchone()
        if row is None:
            return []
        tweet = Tweet.from_dict(json.loads(row[0]))
        return [(TWEET, ref, depth + 1, key, now) for ref in links(tweet, cfg.follow)]

    def claim(self, owner: str, limit: int, lease: float = 300.0) -> list[Item]:
        """Lease up to ``limit`` ready items to ``owner``, shallowest first."""
        if limit <= 0:
            return []
        now = self._clock()
        with self._tx():
            # Expired leases first (their worker died), then the shallowest queued
            # items. Both queries walk the frontier_queue (state, depth) index instead of sorting
            # the whole frontier, which matters once it holds millions of rows.
            rows = self.conn.execute(
                "SELECT rowid, kind, key, depth FROM frontier WHERE state = 'leased' AND lease_until < ? "
                "LIMIT ?", (now, limit)).fetchall()
            if len(rows) < limit:
                rows += self.conn.execute(
                    "SELECT rowid, kind, key, depth FROM frontier WHERE state = 'pending' AND not_before <= ? "
                    "ORDER BY depth LIMIT ?", (now, limit - len(rows))).fetchall()
            self.conn.executemany(
                "UPDATE frontier SET state = 'leased', lease_owner = ?, lease_until = ?, updated = ? "
                "WHERE rowid = ?", [(owner, now + lease, now, r[0]) for r in rows])
        return [Item(kind, key, depth) for _, kind, key, depth in rows]

    def renew(self, owner: str, lease: float = 300.0) -> None:
        """Extend every lease ``owner`` holds (and its heartbeat)."""
        now = self._clock()
        with self._tx():
            self.conn.execute("UPDATE frontier SET lease_until = ? WHERE state = 'leased' AND lease_owner = ?",
                              (now + lease, owner))
            self.conn.execute("UPDATE workers SET heartbeat = ? WHERE id = ?", (now, owner))

    def release(self, owner: str, items: Optional[Iterable[Item]] = None) -> int:
        """Hand leased items back to the queue (all of ``owner``'s by default)."""
        now = self._clock()
        with self._tx():
            before = self.conn.total_changes
            if items is None:
                self.conn.execute(
                    "UPDATE frontier SET state = 'pending', lease_owner = NULL, lease_until = NULL, updated = ? "
                    "WHERE state = 'leased' AND lease_owner = ?", (now, owner))
            else:
                self.conn.executemany(
                    "UPDATE frontier SET state = 'pending', lease_owner = NULL, lease_until = NULL, updated = ? "
                    "WHERE kind = ? AND key = ? AND state = 'leased' AND lease_owner = ?",
                    [(now, i.kind, i.key, owner) for i in items])
            return self.conn.total_changes - before

    def complete(self, outcomes: Iterable[Outcome]) -> dict:
        """Record a batch of outcomes: store tweets, queue their links, park failures.

        Returns counts: ``done``, ``missing``, ``retry``, ``failed``,
        ``stored`` (new tweets) and ``queued`` (newly discovered items).
        Outcomes for items whose lease was lost to another worker are still
        applied: the work is done, and storing it is idempotent.
        """
        outcomes = list(outcomes)
        cfg = self.config
        now = self._clock()
        counts = dict.fromkeys(("done", "missing", "retry", "failed", "stored", "queued"), 0)
        tweets = [t for o in outcomes for t in o.tweets]
        if tweets:
            counts["stored"] = self.tweets.upsert(tweets)
        state_rows, retry_rows, new_rows, timeline_rows = [], [], [], []
        for o in outcomes:
            it = o.item
            if o.error is not None:
                attempts = self._attempts(it) + 1
                if attempts >= cfg.max_attempts:
                    state_rows.append(("failed", attempts, o.error, now, it.kind, it.key))
                    counts["failed"] += 1
                else:
                    delay = min(300.0, 5.0 * 2 ** (attempts - 1))
                    retry_rows.append((attempts, o.error, now + delay, now, it.kind, it.key))
                    counts["retry"] += 1
                continue
            if o.missing:
                state_rows.append(("missing", None, None, now, it.kind, it.key))
                counts["missing"] += 1
                continue
            state_rows.append(("done", None, None, now, it.kind, it.key))
            counts["done"] += 1
            link_depth = it.depth + 1
            for t in o.tweets:
                if it.kind == USER:
                    # Timeline tweets are stored already and sit at the
                    # profile's depth; mark them done so a link to one of
                    # them doesn't fetch it again.
                    timeline_rows.append((TWEET, t.id, it.depth, it.key, now))
                if link_depth <= cfg.max_depth:
                    for ref in links(t, cfg.follow):
                        new_rows.append((TWEET, ref, link_depth, t.id, now))
        with self._tx():
            self.conn.executemany(
                "UPDATE frontier SET state = ?, attempts = COALESCE(?, attempts), last_error = ?, "
                "lease_owner = NULL, lease_until = NULL, updated = ? WHERE kind = ? AND key = ?", state_rows)
            self.conn.executemany(
                "UPDATE frontier SET state = 'pending', attempts = ?, last_error = ?, not_before = ?, "
                "lease_owner = NULL, lease_until = NULL, updated = ? WHERE kind = ? AND key = ?", retry_rows)
            self.conn.executemany(
                "INSERT INTO frontier (kind, key, depth, parent, state, updated) VALUES (?, ?, ?, ?, 'done', ?) "
                "ON CONFLICT (kind, key) DO UPDATE SET state = 'done', updated = excluded.updated, "
                "depth = MIN(depth, excluded.depth) WHERE state = 'pending'", timeline_rows)
            counts["queued"] = self._queue(new_rows)
        return counts

    def _attempts(self, item: Item) -> int:
        row = self.conn.execute("SELECT attempts FROM frontier WHERE kind = ? AND key = ?",
                                (item.kind, item.key)).fetchone()
        return row[0] if row else 0

    def retry_failed(self) -> int:
        """Put every failed item back in the queue with a fresh attempt count."""
        with self._tx():
            before = self.conn.total_changes
            self.conn.execute("UPDATE frontier SET state = 'pending', attempts = 0, not_before = 0, updated = ? "
                              "WHERE state = 'failed'", (self._clock(),))
            return self.conn.total_changes - before

    def counts(self) -> dict:
        out = dict.fromkeys(STATES, 0)
        out.update(self.conn.execute("SELECT state, COUNT(*) FROM frontier GROUP BY state"))
        return out

    def next_ready_in(self) -> Optional[float]:
        """Seconds until the next queued item becomes claimable; None if nothing is queued.

        Items leased by live workers don't count: they are that worker's to finish.
        """
        now = self._clock()
        row = self.conn.execute(
            "SELECT MIN(not_before) FROM frontier WHERE state = 'pending'").fetchone()
        waits = [] if row[0] is None else [max(0.0, row[0] - now)]
        row = self.conn.execute(
            "SELECT MIN(lease_until) FROM frontier WHERE state = 'leased'").fetchone()
        if row[0] is not None and row[0] < now:
            waits.append(0.0)
        return min(waits) if waits else None

    def failures(self, limit: int = 20) -> list[tuple[str, str, int, str]]:
        return self.conn.execute(
            "SELECT kind, key, attempts, last_error FROM frontier WHERE state = 'failed' "
            "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()

    # -- workers ---------------------------------------------------------

    def register(self, owner: str) -> None:
        now = self._clock()
        host, pid = owner.split(":")[0], os.getpid()
        with self._tx():
            self.conn.execute("INSERT OR REPLACE INTO workers (id, host, pid, started, heartbeat) "
                              "VALUES (?, ?, ?, ?, ?)", (owner, host, pid, now, now))

    def heartbeat(self, owner: str, stats: Optional[dict] = None) -> None:
        with self._tx():
            self.conn.execute("UPDATE workers SET heartbeat = ?, stats = COALESCE(?, stats) WHERE id = ?",
                              (self._clock(), json.dumps(stats) if stats is not None else None, owner))

    def unregister(self, owner: str) -> None:
        self.release(owner)
        with self._tx():
            self.conn.execute("DELETE FROM workers WHERE id = ?", (owner,))

    def leased_elsewhere(self, owner: str) -> int:
        """Items currently leased to workers other than ``owner``."""
        return self.conn.execute("SELECT COUNT(*) FROM frontier WHERE state = 'leased' AND lease_owner != ?",
                                 (owner,)).fetchone()[0]

    def reap(self, stale_after: float = 60.0) -> int:
        """Forget workers that are gone and requeue what they held.

        A worker is gone when its heartbeat is older than ``stale_after``, or,
        on this host, as soon as its process no longer exists (so a resume
        right after ``kill -9`` doesn't wait for the heartbeat to go stale).
        """
        cutoff = self._clock() - stale_after
        host = socket.gethostname()
        dead = []
        for wid, whost, pid, beat in self.conn.execute("SELECT id, host, pid, heartbeat FROM workers"):
            if beat < cutoff or (whost == host and pid != os.getpid() and not _pid_alive(pid)):
                dead.append(wid)
        released = 0
        for owner in dead:
            released += self.release(owner)
            with self._tx():
                self.conn.execute("DELETE FROM workers WHERE id = ?", (owner,))
        return released

    def workers(self) -> list[dict]:
        rows = self.conn.execute("SELECT id, host, pid, started, heartbeat, stats FROM workers "
                                 "ORDER BY started").fetchall()
        return [{"id": r[0], "host": r[1], "pid": r[2], "started": r[3], "heartbeat": r[4],
                 "stats": json.loads(r[5]) if r[5] else None} for r in rows]


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return True  # unknown: rely on the heartbeat
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


class _Transaction:
    """``BEGIN IMMEDIATE`` … ``COMMIT``: takes the write lock up front."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *exc) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
