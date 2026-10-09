"""Run statistics, Prometheus/JSON export, adaptive concurrency and drift alarms.

``Metrics`` is the observer both HTTP clients and the crawler report to.
It counts responses by status, retries and time spent waiting on rate
limits, keeps a request-latency histogram, and tallies crawl outcomes. A
snapshot is available as a dict (``snapshot``), as Prometheus text
(``prometheus``) and over HTTP (``serve``: ``/metrics``, ``/stats``,
``/healthz``).

``AdaptiveLimit`` is an AIMD concurrency limit in the spirit of Scrapy's
AutoThrottle and Crawlee's autoscaled pool: it grows while responses are
healthy and halves when the server signals trouble (429, 5xx, timeouts),
so a struggling server sees less load, not more retries.

``DriftMonitor`` watches parsed tweets for signs that X changed a payload
shape (dates, authors or text suddenly missing, parse errors climbing)
and warns once per symptom, since the scraper otherwise keeps "succeeding"
while storing empty fields.
"""
from __future__ import annotations

import bisect
import json
import logging
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterable, Optional

from .models import Tweet

log = logging.getLogger(__name__)

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
# Statuses that mean "the server is overloaded or limiting us": back off.
CONGESTION_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class AdaptiveLimit:
    """Additive-increase / multiplicative-decrease limit on requests in flight.

    Starts at ``initial`` and, until the first congestion signal, grows by
    one per success (doubling every round trip, like TCP slow start); after
    that by one per ``limit`` successes. A congestion signal multiplies the
    limit by ``decrease``, at most once per ``cooldown`` seconds so one
    burst of 429s counts once.
    """

    def __init__(self, maximum: int, initial: Optional[int] = None, minimum: int = 1,
                 decrease: float = 0.5, cooldown: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.maximum = max(1, maximum)
        self.minimum = max(1, min(minimum, self.maximum))
        self.limit = float(min(self.maximum, max(self.minimum, initial or min(self.maximum, 8))))
        self.decrease = decrease
        self.cooldown = cooldown
        self.slow_start = True
        self.decreases = 0
        self._clock = clock
        self._last_decrease = float("-inf")
        self._lock = threading.Lock()

    def on_success(self) -> None:
        with self._lock:
            step = 1.0 if self.slow_start else 1.0 / self.limit
            self.limit = min(float(self.maximum), self.limit + step)

    def on_congestion(self) -> None:
        with self._lock:
            now = self._clock()
            if now - self._last_decrease < self.cooldown:
                return
            self._last_decrease = now
            self.slow_start = False
            self.decreases += 1
            self.limit = max(float(self.minimum), self.limit * self.decrease)

    def on_response(self, status: Optional[int], seconds: float, error: Optional[str] = None) -> None:
        if status is None or status in CONGESTION_STATUSES:
            self.on_congestion()
        elif status < 400:
            self.on_success()


def anomalies(tweet: Tweet) -> list[str]:
    """Symptoms of a payload the parser no longer understands."""
    out = []
    if not tweet.created_at:
        out.append("no_date")
    elif not tweet.created_at.endswith("Z"):
        out.append("unparsed_date")
    if not tweet.user.screen_name:
        out.append("no_author")
    if not tweet.text and not tweet.media:
        out.append("empty_text")
    if tweet.like_count is None:
        out.append("no_like_count")
    return out


class DriftMonitor:
    """Warns when a symptom shows up in more than ``threshold`` of recent items."""

    def __init__(self, window: int = 500, threshold: float = 0.2, min_samples: int = 50):
        self.window = window
        self.threshold = threshold
        self.min_samples = min_samples
        self._recent: deque[tuple[str, ...]] = deque(maxlen=window)
        self.alerts: set[str] = set()

    def observe(self, symptoms: Iterable[str]) -> list[str]:
        """Record one item; returns symptoms that just crossed the threshold."""
        self._recent.append(tuple(symptoms))
        if len(self._recent) < self.min_samples:
            return []
        counts = Counter(s for item in self._recent for s in item)
        n = len(self._recent)
        raised = []
        for symptom in set(counts) | self.alerts:
            share = counts.get(symptom, 0) / n
            if share > self.threshold and symptom not in self.alerts:
                self.alerts.add(symptom)
                raised.append(symptom)
                log.warning("payload drift? %.0f%% of the last %d items show %r; X may have changed "
                            "a payload shape (see xscraper/parse.py)", share * 100, n, symptom)
            elif share <= self.threshold / 2 and symptom in self.alerts:
                self.alerts.discard(symptom)
                log.warning("payload drift: %r back to %.0f%% of recent items", symptom, share * 100)
        return raised


class Metrics:
    """Thread-safe run statistics; usable as an HTTP-client and crawler observer."""

    def __init__(self, clock: Callable[[], float] = time.time, rate_window: float = 30.0):
        self._clock = clock
        self._lock = threading.Lock()
        self.started = clock()
        self.responses: Counter = Counter()   # status code (or error class) -> count
        self.retries = 0
        self.waits: Counter = Counter()       # "rate_limit" / "server" -> seconds
        self.items: Counter = Counter()       # done / missing / retry / failed
        self.stored = 0
        self.queued = 0
        self.anomalies: Counter = Counter()
        self.events: Counter = Counter()      # watch events by type
        self._buckets = [0] * (len(LATENCY_BUCKETS) + 1)
        self._latency_sum = 0.0
        self._latency_count = 0
        self._rate_window = rate_window
        self._marks: deque[tuple[float, int]] = deque()  # (time, items finished so far)
        self.drift = DriftMonitor()
        self.limit: Optional[AdaptiveLimit] = None
        self.frontier: Optional[dict] = None  # latest job counts, set by the crawler
        self.last_progress = self.started

    # -- HTTP client hooks -----------------------------------------------

    def on_response(self, status: Optional[int], seconds: float, error: Optional[str] = None) -> None:
        with self._lock:
            self.responses[str(status) if status is not None else (error or "error")] += 1
            self._buckets[bisect.bisect_left(LATENCY_BUCKETS, seconds)] += 1
            self._latency_sum += seconds
            self._latency_count += 1
        if self.limit is not None:
            self.limit.on_response(status, seconds, error)

    def on_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def on_wait(self, kind: str, seconds: float) -> None:
        with self._lock:
            self.waits[kind] += seconds

    # -- crawler hooks ---------------------------------------------------

    def outcomes(self, outcomes: list, counts: dict) -> None:
        with self._lock:
            for key in ("done", "missing", "retry", "failed"):
                self.items[key] += counts.get(key, 0)
            self.stored += counts.get("stored", 0)
            self.queued += counts.get("queued", 0)
            now = self._clock()
            self.last_progress = now
            self._marks.append((now, sum(self.items.values())))
            while len(self._marks) > 2 and self._marks[0][0] < now - self._rate_window:
                self._marks.popleft()
        for o in outcomes:
            symptoms = [s for t in o.tweets for s in anomalies(t)]
            if o.error and o.error.startswith("ParseError"):
                symptoms.append("parse_error")
            with self._lock:
                self.anomalies.update(symptoms)
            self.drift.observe(set(symptoms))

    def watch_events(self, events: list) -> None:
        with self._lock:
            self.events.update(e.type for e in events)
            self.last_progress = self._clock()

    # -- reading ---------------------------------------------------------

    def percentile(self, q: float) -> Optional[float]:
        """Latency percentile, interpolated within its histogram bucket."""
        with self._lock:
            total = self._latency_count
            if not total:
                return None
            target, seen, lower = q * total, 0, 0.0
            for bound, n in zip(LATENCY_BUCKETS + (float("inf"),), self._buckets):
                if n and seen + n >= target:
                    if bound == float("inf"):
                        return lower
                    return round(lower + (bound - lower) * (target - seen) / n, 4)
                seen += n
                lower = bound
        return None

    def rate(self) -> float:
        """Items finished per second over the recent window."""
        with self._lock:
            if len(self._marks) < 2:
                elapsed = self._clock() - self.started
                return sum(self.items.values()) / elapsed if elapsed > 0 else 0.0
            (t0, n0), (t1, n1) = self._marks[0], self._marks[-1]
            return (n1 - n0) / (t1 - t0) if t1 > t0 else 0.0

    def snapshot(self) -> dict:
        p50, p95 = self.percentile(0.5), self.percentile(0.95)
        rate = self.rate()
        with self._lock:
            return {
                "uptime_s": round(self._clock() - self.started, 1),
                "items": dict(self.items),
                "items_per_s": round(rate, 1),
                "tweets_stored": self.stored,
                "links_queued": self.queued,
                "responses": dict(self.responses),
                "retries": self.retries,
                # Summed over requests: 100 requests paused together for 2 s count 200 s.
                "request_wait_s": {k: round(v, 1) for k, v in self.waits.items()},
                "latency_s": {"p50": p50, "p95": p95,
                              "mean": round(self._latency_sum / self._latency_count, 4)
                              if self._latency_count else None},
                "concurrency_limit": round(self.limit.limit, 1) if self.limit else None,
                "anomalies": dict(self.anomalies),
                "events": dict(self.events),
                "drift_alerts": sorted(self.drift.alerts),
                "frontier": self.frontier,
            }

    def progress_line(self) -> str:
        s = self.snapshot()
        items = s["items"]
        parts = [f"{items.get('done', 0):,} done", f"{items.get('missing', 0):,} unavailable",
                 f"{items.get('failed', 0):,} failed", f"{s['items_per_s']:,.0f} items/s"]
        if s["frontier"]:
            f = s["frontier"]
            parts.append(f"{f.get('pending', 0) + f.get('leased', 0):,} remaining")
        if s["latency_s"]["p95"] is not None:
            parts.append(f"p95 {s['latency_s']['p95'] * 1000:.0f} ms")
        limited = s["responses"].get("429", 0)
        if limited:
            parts.append(f"{limited} × 429")
        if s["concurrency_limit"] is not None:
            parts.append(f"concurrency {s['concurrency_limit']:.0f}")
        if s["drift_alerts"]:
            parts.append("DRIFT: " + ",".join(s["drift_alerts"]))
        return "progress: " + ", ".join(parts)

    def prometheus(self) -> str:
        """The Prometheus text exposition format (version 0.0.4)."""
        s = self.snapshot()
        out: list[str] = []

        def metric(name, kind, help_, samples):
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                lbl = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
                out.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")

        metric("xscraper_responses_total", "counter", "HTTP responses by status (or error type).",
               [({"status": k}, v) for k, v in sorted(s["responses"].items())])
        metric("xscraper_retries_total", "counter", "Requests retried.", [({}, s["retries"])])
        metric("xscraper_wait_seconds_total", "counter",
               "Time requests spent waiting on rate limits, summed over requests.",
               [({"reason": k}, v) for k, v in sorted(s["request_wait_s"].items())])
        metric("xscraper_items_total", "counter", "Crawl items finished, by outcome.",
               [({"outcome": k}, v) for k, v in sorted(s["items"].items())])
        metric("xscraper_tweets_stored_total", "counter", "New tweets stored.", [({}, s["tweets_stored"])])
        metric("xscraper_links_queued_total", "counter", "Links discovered and queued.",
               [({}, s["links_queued"])])
        metric("xscraper_watch_events_total", "counter", "Watch events emitted, by type.",
               [({"type": k}, v) for k, v in sorted(s["events"].items())])
        metric("xscraper_payload_anomalies_total", "counter", "Parsed items showing signs of payload drift.",
               [({"symptom": k}, v) for k, v in sorted(s["anomalies"].items())])
        metric("xscraper_drift_alert", "gauge", "1 while a drift symptom is above its threshold.",
               [({"symptom": k}, 1) for k in s["drift_alerts"]])
        if s["concurrency_limit"] is not None:
            metric("xscraper_concurrency_limit", "gauge", "Adaptive limit on requests in flight.",
                   [({}, s["concurrency_limit"])])
        if s["frontier"]:
            metric("xscraper_frontier_items", "gauge", "Job items by state.",
                   [({"state": k}, v) for k, v in sorted(s["frontier"].items())])
        with self._lock:
            buckets, total, count = list(self._buckets), self._latency_sum, self._latency_count
        out.append("# HELP xscraper_request_duration_seconds Request latency.")
        out.append("# TYPE xscraper_request_duration_seconds histogram")
        cumulative = 0
        for bound, n in zip(LATENCY_BUCKETS + (float("inf"),), buckets):
            cumulative += n
            le = "+Inf" if bound == float("inf") else repr(bound)
            out.append(f'xscraper_request_duration_seconds_bucket{{le="{le}"}} {cumulative}')
        out.append(f"xscraper_request_duration_seconds_sum {total}")
        out.append(f"xscraper_request_duration_seconds_count {count}")
        return "\n".join(out) + "\n"

    def healthy(self, stall_after: float = 300.0) -> tuple[bool, str]:
        """Unhealthy when work is queued but nothing has finished for ``stall_after`` seconds."""
        idle = self._clock() - self.last_progress
        queued = (self.frontier or {}).get("pending", 0) + (self.frontier or {}).get("leased", 0)
        if queued and idle > stall_after:
            return False, f"no progress for {idle:.0f}s with {queued} items queued"
        if self.drift.alerts:
            return True, "ok (drift: " + ",".join(sorted(self.drift.alerts)) + ")"
        return True, "ok"

    def serve(self, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
        """Serve /metrics, /stats and /healthz from a daemon thread; returns the server."""
        metrics = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/metrics":
                    code, ctype, body = 200, "text/plain; version=0.0.4", metrics.prometheus()
                elif path == "/stats":
                    code, ctype, body = 200, "application/json", json.dumps(metrics.snapshot())
                elif path == "/healthz":
                    ok, why = metrics.healthy()
                    code, ctype, body = (200 if ok else 503), "text/plain", why + "\n"
                else:
                    code, ctype, body = 404, "text/plain", "not found\n"
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="xscraper-metrics", daemon=True).start()
        return server


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
