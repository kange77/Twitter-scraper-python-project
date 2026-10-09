"""RateGate compares X's x-rate-limit-reset (X's clock) with the local wall clock.
Server window: 0 requests left, resets in 60 s by the server's clock."""
import time
from xscraper.http import RateGate
server_now = 1_800_000_000.0
headers = {"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(int(server_now + 60))}
for skew in (0, +90, -600):
    g = RateGate(clock=lambda: server_now + skew)
    g.enter(); g.leave(200, headers)
    print(f"local clock {skew:+5d}s vs X: next request waits {g.enter():6.0f}s "
          f"(correct: 60s{', sent into a certain 429' if skew >= 60 else ''})")
