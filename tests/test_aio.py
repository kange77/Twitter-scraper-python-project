"""The asyncio client and scraper, against a local aiohttp server."""
import asyncio
import json
import time

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from xscraper import cli  # noqa: E402
from xscraper import scraper as sync_scraper  # noqa: E402
from xscraper.aio import AsyncHttpClient, AsyncScraper  # noqa: E402
from xscraper.http import HttpError  # noqa: E402
from xscraper.parse import ParseError  # noqa: E402
from xscraper.storage import load  # noqa: E402


def serve(routes, test):
    """Run ``test(base_url, hits)`` against a server built from ``routes``."""
    hits = []

    async def main():
        app = web.Application()
        for path, handler in routes.items():
            async def wrapped(request, handler=handler):
                hits.append(request)
                return await handler(request)
            app.router.add_get(path, wrapped)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            return await test(f"http://127.0.0.1:{port}", hits)
        finally:
            await runner.cleanup()

    return asyncio.run(main())


@pytest.fixture
def endpoints(monkeypatch):
    def point_at(base):
        monkeypatch.setattr(sync_scraper, "TWEET_ENDPOINT", base + "/tweet-result")
        monkeypatch.setattr(sync_scraper, "TIMELINE_ENDPOINT", base + "/srv/{}")
    return point_at


def tweet_handler(tweet_results):
    by_id = {tweet_results["tweet"]["id_str"]: tweet_results["tweet"],
             tweet_results["parent"]["id_str"]: tweet_results["parent"]}

    async def handler(request):
        i = request.query["id"]
        if i == "777":
            return web.Response(text="<html>blocked</html>", content_type="text/html")
        if i == "500":
            return web.Response(status=403)
        if i not in by_id:
            return web.Response(status=404)
        return web.json_response(by_id[i])
    return handler


def test_tweets_in_order_with_missing_and_failures(tweet_results, endpoints):
    async def test(base, hits):
        endpoints(base)
        async with AsyncHttpClient(rate=1000, concurrency=4) as client:
            s = AsyncScraper(client)
            ids = ["1834231000000000000", "999", "1834231234567890123", "777", "500"]
            got = await s.tweets(ids, return_exceptions=True)
            assert [getattr(t, "id", t) for t in got[:3]] == ["1834231000000000000", None,
                                                               "1834231234567890123"]
            assert isinstance(got[3], ParseError) and isinstance(got[4], HttpError)
            with pytest.raises(HttpError):
                await s.tweets(["500"])
            chain = await s.thread("1834231234567890123")
            assert [t.id for t in chain] == ["1834231000000000000", "1834231234567890123"]

    serve({"/tweet-result": tweet_handler(tweet_results)}, test)


def test_retries_and_shared_pause_on_429(tweet_results, endpoints):
    calls = []
    ok = tweet_handler(tweet_results)

    async def flaky(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return web.Response(status=429, headers={"Retry-After": "0"})
        if len(calls) == 2:
            return web.Response(status=503)
        return await ok(request)

    async def test(base, hits):
        endpoints(base)
        async with AsyncHttpClient(rate=1000, backoff=0.01, concurrency=2) as client:
            t = await AsyncScraper(client).tweet("1834231234567890123")
            assert t.like_count == 48213 and len(calls) == 3
            assert client.gate.in_flight == 0

    serve({"/tweet-result": flaky}, test)


def test_budget_from_headers_prevents_429s(tweet_results, endpoints):
    ok = tweet_handler(tweet_results)
    window = {"reset": time.time() + 1.5, "left": 3}
    limited = []

    async def limited_handler(request):
        now = time.time()
        if now >= window["reset"]:
            window.update(reset=now + 1.5, left=3)
        if window["left"] <= 0:
            limited.append(now)
            return web.Response(status=429, headers={"x-rate-limit-reset": str(int(window["reset"]) + 1)})
        window["left"] -= 1
        resp = await ok(request)
        resp.headers["x-rate-limit-remaining"] = str(window["left"])
        resp.headers["x-rate-limit-reset"] = str(int(window["reset"]) + 1)
        return resp

    async def test(base, hits):
        endpoints(base)
        async with AsyncHttpClient(rate=1000, concurrency=1) as client:
            got = await AsyncScraper(client).tweets(["1834231234567890123"] * 5)
            assert all(t.id == "1834231234567890123" for t in got)
        assert not limited

    serve({"/tweet-result": limited_handler}, test)


def test_cli_async_streams_ids_from_a_file(tweet_results, endpoints, tmp_path, capsys):
    ids = tmp_path / "ids.txt"
    ids.write_text("# wanted\n1834231234567890123\n\n999  # gone\nhttps://x.com/NASA/status/1834231000000000000\n")
    out = tmp_path / "out.jsonl"

    async def test(base, hits):
        endpoints(base)
        # cli.main calls asyncio.run itself, so run it off this loop's thread.
        return await asyncio.to_thread(cli.main, ["tweet", "-i", str(ids), "--http", "async",
                                                  "--rate", "100", "-o", str(out)])

    assert serve({"/tweet-result": tweet_handler(tweet_results)}, test) == 0
    assert [t.id for t in load(out)] == ["1834231234567890123", "1834231000000000000"]
    assert "not available (deleted, private or withheld): 999" in capsys.readouterr().err


def test_cli_async_user_timelines(timeline_html, endpoints, tmp_path, capsys):
    async def timeline(request):
        if request.path.endswith("/nosuch"):
            return web.Response(status=404)
        return web.Response(text=timeline_html, content_type="text/html")

    async def test(base, hits):
        endpoints(base)
        return await asyncio.to_thread(cli.main, ["user", "NASA", "nosuch", "--http", "async",
                                                  "--rate", "100", "-o", str(tmp_path / "t.json")])

    assert serve({"/srv/{name}": timeline}, test) == 1
    assert len(json.loads((tmp_path / "t.json").read_text())) == 3
    assert "@nosuch: 404" in capsys.readouterr().err
