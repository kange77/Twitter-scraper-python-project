"""Rate limits shared by every crawler process working on one job.

Several processes on one job would each run their own token bucket and
their own view of X's rate-limit window, so together they would send N
times ``--rate`` and keep drawing 429s after one of them had been told to
stop. These classes keep that state in the job's SQLite file instead:

* ``SharedRateLimiter`` is a GCRA ("virtual scheduling") limiter: the job
  stores the theoretical arrival time of the next request, and each process
  reserves a small block of evenly spaced send slots in one transaction, so
  the job as a whole sends at most ``rate`` requests per second.
* ``SharedRateGate`` is ``http.RateGate`` plus a job-wide hold: when one
  process learns the window is used up or gets a 429 with Retry-After, it
  publishes "hold until T" and every process waits for T.

This shares one budget; it doesn't multiply it. Adding processes adds CPU
for parsing and storing, not requests per second.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .http import RateGate

_SCHEMA = """CREATE TABLE IF NOT EXISTS rate_state (
    name TEXT PRIMARY KEY,
    tat REAL NOT NULL DEFAULT 0,
    hold_until REAL NOT NULL DEFAULT 0)"""


class _SharedRow:
    """One row of ``rate_state``, with its own connection usable from any thread."""

    def __init__(self, path: str | Path, name: str):
        self.name = name
        self.conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute(_SCHEMA)
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
    """``RateGate`` whose pauses apply to every process on the job."""

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

    def enter(self) -> float:
        now = self._clock()
        hold = self._shared_hold(now)
        if hold > now:
            return hold - now
        wait = super().enter()
        if wait > 0:
            # This process knows the window is spent (or it was told to pause); tell the others.
            self._publish(now + min(wait, self.max_wait))
        return wait

    def pause(self, seconds: float) -> None:
        super().pause(seconds)
        self._publish(self._clock() + min(seconds, self.max_wait))

    def close(self) -> None:
        self._row.close()
