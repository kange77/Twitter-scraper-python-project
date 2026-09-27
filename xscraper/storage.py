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


class TweetWriter:
    """Incremental writer: call ``write`` with batches as they arrive.

    JSON Lines, CSV and SQLite rows reach disk batch by batch, so a long
    scrape keeps what it has fetched if it is interrupted and never holds the
    whole result set in memory. JSON is written as one array that is closed
    by ``close``; the output is byte-for-byte what ``json.dump(indent=2)``
    would produce.
    """

    def __init__(self, path: str | Path, fmt: Optional[str] = None):
        self.fmt = detect_format(path, fmt)
        self.path = Path(path)
        self.written = 0  # tweets written (for SQLite: newly stored)
        self.seen = 0
        self._store: Optional[TweetStore] = None
        self._file = None
        if self.fmt == "sqlite":
            self._store = TweetStore(self.path)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8", newline="")
        if self.fmt == "csv":
            self._csv = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
            self._csv.writeheader()
        elif self.fmt == "json":
            self._file.write("[")

    def write(self, tweets: Iterable[Tweet]) -> None:
        tweets = list(tweets)
        self.seen += len(tweets)
        if self._store is not None:
            self.written += self._store.upsert(tweets)
            return
        f = self._file
        if self.fmt == "jsonl":
            f.write("".join(json.dumps(t.to_dict(), ensure_ascii=False) + "\n" for t in tweets))
        elif self.fmt == "csv":
            self._csv.writerows(csv_row(t) for t in tweets)
        else:
            for t in tweets:
                item = json.dumps(t.to_dict(), ensure_ascii=False, indent=2).replace("\n", "\n  ")
                f.write(("," if self.written else "") + "\n  " + item)
                self.written += 1
            f.flush()
            return
        self.written += len(tweets)
        f.flush()

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None
        elif self._file is not None:
            if self.fmt == "json":
                self._file.write("\n]" if self.written else "]")
            self._file.close()
            self._file = None

    def __enter__(self) -> "TweetWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def export(tweets: Iterable[Tweet], path: str | Path, fmt: Optional[str] = None) -> int:
    """Write tweets to ``path``; returns how many were written (or newly stored)."""
    with TweetWriter(path, fmt) as writer:
        writer.write(tweets)
    return writer.written


def load(path: str | Path, fmt: Optional[str] = None) -> list[Tweet]:
    """Read tweets previously written as JSON, JSON Lines or SQLite."""
    fmt = detect_format(path, fmt)
    if fmt == "csv":
        raise ValueError("CSV is export-only; load from JSON, JSON Lines or SQLite")
    if not Path(path).is_file():
        raise FileNotFoundError(f"no such file: {path}")
    if fmt == "sqlite":
        with TweetStore(path) as store:
            return list(store)
    text = Path(path).read_text(encoding="utf-8")
    try:
        if fmt == "json":
            rows = json.loads(text)
        else:
            rows = []
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"{path} line {lineno}: invalid JSON ({exc})") from exc
        if not isinstance(rows, list):
            raise ValueError(f"{path}: expected a JSON array of tweets")
        return [Tweet.from_dict(r) for r in rows]
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc})") from exc
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{path}: not an xscraper export ({type(exc).__name__}: {exc})") from exc


class TweetStore:
    """SQLite store keyed by tweet ID, so repeated scrapes only add what's new."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        try:
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
        except sqlite3.DatabaseError as exc:
            self.conn.close()
            raise ValueError(f"{path} is not an xscraper SQLite store ({exc})") from exc

    def __enter__(self) -> "TweetStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def upsert(self, tweets: Iterable[Tweet]) -> int:
        """Insert new tweets and refresh known ones; returns the number of new tweets."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [(t.id, t.user.screen_name, t.created_at, t.text,
                 json.dumps(t.to_dict(), ensure_ascii=False), now, now) for t in tweets]
        if not rows:
            return 0
        ids = list(dict.fromkeys(r[0] for r in rows))
        existing: set[str] = set()
        for i in range(0, len(ids), 500):  # stay under SQLite's bound-parameter limit
            chunk = ids[i:i + 500]
            existing.update(r[0] for r in self.conn.execute(
                f"SELECT id FROM tweets WHERE id IN ({','.join('?' * len(chunk))})", chunk))
        with self.conn:
            self.conn.executemany(
                """INSERT INTO tweets (id, screen_name, created_at, text, data, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET data = excluded.data, text = excluded.text,
                       last_seen = excluded.last_seen""",
                rows,
            )
        return len(ids) - len(existing)

    def known_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM tweets")}

    def __len__(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]

    def __iter__(self) -> Iterator[Tweet]:
        for (data,) in self.conn.execute("SELECT data FROM tweets ORDER BY created_at DESC"):
            yield Tweet.from_dict(json.loads(data))
