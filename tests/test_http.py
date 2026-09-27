import pytest
import requests

from tests.conftest import FakeResponse
from xscraper.http import HttpClient, HttpError, NotFound, RateGate, RateLimiter, retry_after_seconds


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses, **kw):
    sleeps = []
    now = [1000.0]

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    c = HttpClient(rate=1000, session=FakeSession(responses), sleep=sleep, clock=lambda: now[0], **kw)
    return c, sleeps


def test_retries_then_succeeds_honouring_retry_after():
    c, sleeps = client([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(503),
                        FakeResponse(200, b"ok")], max_backoff=60)
    assert c.get("https://x").content == b"ok"
    assert 7 <= sleeps[0] <= 8  # server-requested wait plus jitter
    assert len(sleeps) == 2


def test_connection_errors_exhaust_retries():
    c, sleeps = client([requests.ConnectionError("boom")] * 3, retries=2)
    with pytest.raises(HttpError, match="after 3 attempts"):
        c.get("https://x")
    assert len(sleeps) == 2


def test_404_and_403_do_not_retry():
    c, sleeps = client([FakeResponse(404)])
    with pytest.raises(NotFound):
        c.get("https://x")
    c, sleeps = client([FakeResponse(403)])
    with pytest.raises(HttpError) as exc:
        c.get("https://x")
    assert exc.value.status == 403 and not sleeps


def test_proxy_rotation_and_cookies():
    c, _ = client([FakeResponse(500), FakeResponse(200)], proxies=["http://p1", "http://p2"],
                  cookies="a=b")
    c.get("https://x")
    assert [k["proxies"]["https"] for k in c.session.calls] == ["http://p1", "http://p2"]
    assert c.session.headers["Cookie"] == "a=b"


def test_retry_after_parsing():
    assert retry_after_seconds(FakeResponse(429, headers={"Retry-After": "3"})) == 3
    assert retry_after_seconds(FakeResponse(429, headers={"x-rate-limit-reset": "1100"}), now=1000) == 100
    date = "Wed, 21 Oct 2015 07:28:10 GMT"
    assert retry_after_seconds(FakeResponse(429, headers={"Retry-After": date}), now=1445412480) == 10
    assert retry_after_seconds(FakeResponse(429, headers={"Retry-After": "soon"})) is None
    assert retry_after_seconds(FakeResponse(429)) is None


def test_rate_limiter_waits_between_tokens():
    now = [0.0]
    waits = []

    def sleep(s):
        waits.append(s)
        now[0] += s

    limiter = RateLimiter(rate=2, burst=2, clock=lambda: now[0], sleep=sleep)
    for _ in range(4):
        limiter.acquire()
    assert waits == pytest.approx([0.5, 0.5])


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def limits(remaining, reset):
    return {"x-rate-limit-remaining": str(remaining), "x-rate-limit-reset": str(reset)}


def test_gate_holds_requests_once_the_window_budget_is_spent():
    clock = Clock()
    gate = RateGate(clock=clock)
    assert gate.enter() == 0 and gate.enter() == 0 and gate.enter() == 0
    # Server says 2 left; two other requests are still in flight, so nothing is.
    gate.leave(200, limits(2, 1010))
    assert gate.enter() == pytest.approx(10)
    clock.now = 1010
    assert gate.enter() == 0  # new window: budget unknown until a response says


def test_gate_counts_down_budget_and_ignores_the_previous_window():
    clock = Clock()
    gate = RateGate(clock=clock)
    gate.enter()
    gate.leave(200, limits(2, 1010))
    assert gate.enter() == 0 and gate.enter() == 0
    assert gate.enter() > 0
    gate.leave(200, limits(50, 1005))  # late answer carrying an older reset
    assert gate.enter() > 0


def test_gate_pauses_everyone_on_429():
    clock = Clock()
    gate = RateGate(clock=clock)
    gate.enter()
    assert gate.leave(429, {"Retry-After": "30"}) == 30
    assert gate.enter() == pytest.approx(30)
    gate.leave(200, {})  # a success without limit headers doesn't lift the pause
    assert gate.wait_time() == pytest.approx(30)


def test_client_waits_for_reset_instead_of_drawing_a_429():
    c, sleeps = client([FakeResponse(200, b"a", headers=limits(0, 1060)), FakeResponse(200, b"b")])
    c.get("https://x")
    assert not sleeps
    assert c.get("https://x").content == b"b"
    assert len(sleeps) == 1 and 60 <= sleeps[0] <= 60.25


def test_pool_is_sized_for_the_workers():
    c = HttpClient(pool_size=64)
    assert c.session.get_adapter("https://x.com")._pool_maxsize == 64
