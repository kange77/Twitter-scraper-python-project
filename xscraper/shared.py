"""Rate limits shared by every crawler process working on one job.

Several processes on one job would each run their own token bucket and
their own view of X's rate-limit window, so together they would send N
times ``--rate`` and keep drawing 429s after one of them had been told to
stop. These classes keep that state in the job's SQLite file instead:

* ``SharedRateLimiter`` is a GCRA ("virtual scheduling") limiter: the job
  stores the theoretical arrival time of the next request, and each process
  reserves a small block of evenly spaced send slots in one transaction, so
  the job as a whole sends at most ``rate`` requests per second.
* ``SharedRateGate`` is ``http.RateGate`` with its state in the job: the
  server's window budget (``x-rate-limit-remaining`` until
  ``x-rate-limit-reset``) is one job-wide counter that every process draws
  from, and when one process gets a 429 with Retry-After it publishes
  "hold until T" and every process waits for T.

This shares one budget; it doesn't multiply it. Adding processes adds CPU
for parsing and storing, not requests per second.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .http import MAX_RESET_HORIZON, RateGate

log = logging.getLogger(__name__)

_SCHEMA = """CREATE TABLE IF NOT EXISTS rate_state (
    name TEXT PRIMARY KEY,
    tat REAL NOT NULL DEFAULT 0,
    hold_until REAL NOT NULL DEFAULT 0,
    win_reset REAL,
    win_budget INTEGER)"""
# Columns added after the first release; job files created before get them on open.
_ADDED_COLUMNS = (("win_reset", "REAL"), ("win_budget", "INTEGER"))


class _SharedRow:
    """One row of ``rate_state``, with its own connection usable from any thread."""

    def __init__(self, path: str | Path, name: str):
        self.name = name
        self.conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute(_SCHEMA)
            have = {r[1] for r in self.conn.execute("PRAGMA table_info(rate_state)")}
            for col, kind in _ADDED_COLUMNS:
                if col not in have:
                    try:
                        self.conn.execute(f"ALTER TABLE rate_state ADD COLUMN {col} {kind}")
                    except sqlite3.OperationalError:  # another process added it first
                        pass
            self.conn.execute("INSERT OR IGNORE INTO rate_state (name) VALUES (?)", (name,))

    def close(self) -> None:
        self.conn.close()


class SharedRateLimiter:
    """Job-wide limit of ``rate`` requests/second with bursts of ``burst``.

    Same interface as ``http.RateLimiter`` (``reserve`` / ``acquire``).
    """

    def __init__(self, path: str | Path, rate: float, burst: int = 1, name: str = "requests",
                 block: float = 0.05, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(1, burst)
        self._row = _SharedRow(path, name)
        self._clock = clock
        self._sleep = sleep
        self._emission = 1.0 / rate
        # Slots reserved per transaction: about ``block`` seconds' worth.
        self._block = max(1, min(self.capacity, int(rate * block)))
        self._slots: deque[float] = deque()
        self._lock = threading.Lock()

    def _take_block(self, now: float) -> None:
        row = self._row
        tolerance = (self.capacity - 1) * self._emission
        with row.lock:
            row.conn.execute("BEGIN IMMEDIATE")
            try:
                (tat,) = row.conn.execute("SELECT tat FROM rate_state WHERE name = ?", (row.name,)).fetchone()
                tat = max(tat, now)
                for _ in range(self._block):
                    self._slots.append(tat - tolerance)
                    tat += self._emission
                row.conn.execute("UPDATE rate_state SET tat = ? WHERE name = ?", (tat, row.name))
                row.conn.execute("COMMIT")
            except BaseException:
                row.conn.execute("ROLLBACK")
                raise

    def reserve(self) -> float:
        with self._lock:
            now = self._clock()
            # Slots already in the past were paid for but not used in time;
            # using them now would be a burst above the limit, so drop them.
            while self._slots and self._slots[0] < now - self._emission * self.capacity:
                self._slots.popleft()
            if not self._slots:
                self._take_block(now)
            return max(0.0, self._slots.popleft() - now)

    def acquire(self) -> None:
        wait = self.reserve()
        if wait > 0:
            self._sleep(wait)

    def close(self) -> None:
        self._row.close()


class SharedRateGate(RateGate):
    """``RateGate`` whose window budget and pauses are shared by every process on the job.

    Each request takes one unit of the job-wide budget in a short write
    transaction, so N processes together spend what the server said is
    left, not N times that. The budget is set from response headers minus
    the requests this process still has in flight; requests other
    processes have in flight at that moment can overshoot it by at most
    their number, once per window, and a 429 then holds every process.
    """

    def __init__(self, path: str | Path, name: str = "requests", clock: Callable[[], float] = time.time,
                 max_wait: float = 900.0, refresh: float = 0.25):
        super().__init__(clock=clock, max_wait=max_wait)
        self._row = _SharedRow(path, name)
        self._refresh = refresh
        self._hold = 0.0
        self._checked = float("-inf")

    def _shared_hold(self, now: float) -> float:
        if now - self._checked >= self._refresh:
            row = self._row
            with row.lock:
                (self._hold,) = row.conn.execute("SELECT hold_until FROM rate_state WHERE name = ?",
                                                 (row.name,)).fetchone()
            self._checked = now
        return self._hold

    def _publish(self, until: float) -> None:
        row = self._row
        with row.lock:
            row.conn.execute("UPDATE rate_state SET hold_until = MAX(hold_until, ?) WHERE name = ?",
                             (until, row.name))
        self._hold = max(self._hold, until)

    def _window_wait(self, now: float, take: bool) -> float:
        """Seconds until the shared window has budget; with ``take``, spend one unit if it has."""
        row = self._row
        with row.lock:
            reset, budget = row.conn.execute("SELECT win_reset, win_budget FROM rate_state WHERE name = ?",
                                             (row.name,)).fetchone()
            if budget is None or (reset is not None and now >= reset):
                return 0.0  # budget unknown (or a new window): send, and let the response say
            if budget <= 0:
                return reset - now
            if not take:
                return 0.0
            row.conn.execute("BEGIN IMMEDIATE")
            try:
                reset, budget = row.conn.execute("SELECT win_reset, win_budget FROM rate_state WHERE name = ?",
                                                 (row.name,)).fetchone()
                if budget is not None and reset is not None and now < reset:
                    if budget <= 0:
                        row.conn.execute("COMMIT")
                        return reset - now
                    row.conn.execute("UPDATE rate_state SET win_budget = win_budget - 1 WHERE name = ?",
                                     (row.name,))
                row.conn.execute("COMMIT")
            except BaseException:
                row.conn.execute("ROLLBACK")
                raise
            return 0.0

    def _wait(self, now: float) -> float:
        if self._until > now:
            return self._until - now
        return self._window_wait(now, take=False)

    def enter(self) -> float:
        now = self._clock()
        hold = self._shared_hold(now)
        if hold > now:
            return hold - now
        with self._lock:
            wait = self._until - now if self._until > now else self._window_wait(now, take=True)
            if wait <= 0:
                self.in_flight += 1
                return 0.0
        return wait

    def leave(self, status=None, headers=None):
        if headers is not None:
            remaining = (headers.get("x-rate-limit-remaining") or "").strip()
            reset = (headers.get("x-rate-limit-reset") or "").strip()
            now = self._clock()
            if remaining.isdigit() and reset.isdigit() and now < float(reset) <= now + MAX_RESET_HORIZON:
                with self._lock:
                    # Requests this process still has in flight will draw on what's left.
                    budget = int(remaining) - max(0, self.in_flight - 1)
                self._set_window(float(reset), budget)
        return super().leave(status, headers)

    def _set_window(self, reset_at: float, budget: int) -> None:
        row = self._row
        with row.lock:
            row.conn.execute("BEGIN IMMEDIATE")
            try:
                # A later reset starts a new window; the same reset can only lower
                # the budget; an earlier one is a late answer from the last window.
                row.conn.execute(
                    "UPDATE rate_state SET "
                    "win_budget = CASE WHEN win_reset IS NULL OR ? > win_reset THEN ? "
                    "                  WHEN ? = win_reset THEN MIN(COALESCE(win_budget, ?), ?) "
                    "                  ELSE win_budget END, "
                    "win_reset = CASE WHEN win_reset IS NULL OR ? > win_reset THEN ? ELSE win_reset END "
                    "WHERE name = ?",
                    (reset_at, budget, reset_at, budget, budget, reset_at, reset_at, row.name))
                row.conn.execute("COMMIT")
            except BaseException:
                row.conn.execute("ROLLBACK")
                raise

    def pause(self, seconds: float) -> None:
        super().pause(seconds)
        self._publish(self._clock() + min(seconds, self.max_wait))

    def close(self) -> None:
        self._row.close()
