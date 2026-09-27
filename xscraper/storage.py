"""Exporters (JSON, JSON Lines, CSV) and an incremental SQLite store."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import Tweet

FORMATS = ("json", "jsonl", "csv", "sqlite")
_EXTENSIONS = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".csv": "csv",
               ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite"}

CSV_FIELDS = ["id", "created_at", "screen_name", "name", "text", "lang", "like_count",
              "retweet_count", "reply_count", "quote_count", "hashtags", "mentions", "urls",
              "media_urls", "in_reply_to_id", "quoted_tweet_id", "retweeted_tweet_id", "url",
              "sentiment", "simhash"]


def detect_format(path: str | Path, fmt: Optional[str] = None) -> str:
    if fmt:
        if fmt not in FORMATS:
            raise ValueError(f"format must be one of {FORMATS}")
        return fmt
    ext = Path(path).suffix.lower()
    if ext not in _EXTENSIONS:
        raise ValueError(f"can't infer format from {ext or 'no extension'!r}; pass a format")
    return _EXTENSIONS[ext]


def csv_row(t: Tweet) -> dict:
    a = t.analysis or {}
    return {
        "id": t.id, "created_at": t.created_at, "screen_name": t.user.screen_name,
        "name": t.user.name, "text": t.text, "lang": t.lang, "like_count": t.like_count,
        "retweet_count": t.retweet_count, "reply_count": t.reply_count,
        "quote_count": t.quote_count, "hashtags": " ".join(t.hashtags),
        "mentions": " ".join(t.mentions), "urls": " ".join(t.urls),
        "media_urls": " ".join(m.video_url or m.url for m in t.media),
        "in_reply_to_id": t.in_reply_to_id, "quoted_tweet_id": t.quoted_tweet_id,
        "retweeted_tweet_id": t.retweeted_tweet_id, "url": t.url,
        "sentiment": a.get("sentiment"), "simhash": f"{a['simhash']:016x}" if "simhash" in a else None,
    }


def export(tweets: Iterable[Tweet], path: str | Path, fmt: Optional[str] = None) -> int:
    """Write tweets to ``path``; returns how many were written (or newly stored)."""
    fmt = detect_format(path, fmt)
    tweets = list(tweets)
    path = Path(path)
    if fmt == "sqlite":
        with TweetStore(path) as store:
            return store.upsert(tweets)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        if fmt == "json":
            json.dump([t.to_dict() for t in tweets], f, ensure_ascii=False, indent=2)
        elif fmt == "jsonl":
            for t in tweets:
                f.write(json.dumps(t.to_dict(), ensure_ascii=False) + "\n")
        else:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(csv_row(t) for t in tweets)
    return len(tweets)


def load(path: str | Path, fmt: Optional[str] = None) -> list[Tweet]:
    """Read tweets previously written as JSON, JSON Lines or SQLite."""
    fmt = detect_format(path, fmt)
    if fmt == "sqlite":
        with TweetStore(path) as store:
            return list(store)
    if fmt == "csv":
        raise ValueError("CSV is export-only; load from JSON, JSON Lines or SQLite")
    text = Path(path).read_text(encoding="utf-8")
    rows = json.loads(text) if fmt == "json" else [json.loads(l) for l in text.splitlines() if l.strip()]
    return [Tweet.from_dict(r) for r in rows]


class TweetStore:
    """SQLite store keyed by tweet ID, so repeated scrapes only add what's new."""

    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS tweets (
                   id TEXT PRIMARY KEY,
                   screen_name TEXT,
                   created_at TEXT,
                   text TEXT,
                   data TEXT NOT NULL,
                   first_seen TEXT NOT NULL,
                   last_seen TEXT NOT NULL)"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS tweets_user ON tweets(screen_name, created_at)")

    def __enter__(self) -> "TweetStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def upsert(self, tweets: Iterable[Tweet]) -> int:
        """Insert new tweets and refresh known ones; returns the number of new tweets."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = 0
        with self.conn:
            for t in tweets:
                exists = self.conn.execute("SELECT 1 FROM tweets WHERE id = ?", (t.id,)).fetchone()
                self.conn.execute(
                    """INSERT INTO tweets (id, screen_name, created_at, text, data, first_seen, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET data = excluded.data, text = excluded.text,
                           last_seen = excluded.last_seen""",
                    (t.id, t.user.screen_name, t.created_at, t.text,
                     json.dumps(t.to_dict(), ensure_ascii=False), now, now),
                )
                new += exists is None
        return new

    def known_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM tweets")}

    def __len__(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]

    def __iter__(self) -> Iterator[Tweet]:
        for (data,) in self.conn.execute("SELECT data FROM tweets ORDER BY created_at DESC"):
            yield Tweet.from_dict(json.loads(data))
