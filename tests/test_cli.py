import json

from tests.conftest import FakeResponse
from xscraper import cli
from xscraper.storage import export, load
from xscraper.parse import parse_timeline_page


def test_analyze_command(tmp_path, timeline_html, capsys):
    src = tmp_path / "tweets.jsonl"
    export(parse_timeline_page(timeline_html), src)
    out = tmp_path / "annotated.json"
    assert cli.main(["analyze", str(src), "--engine", "python", "-o", str(out)]) == 0
    text = capsys.readouterr().out
    assert "3 tweets analysed with the python engine" in text
    assert "top hashtags: #starlink (1)" in text
    assert "top domains: science.nasa.gov (1)" in text
    assert all(t.analysis for t in load(out))


def test_tweet_command(monkeypatch, tweet_results, tmp_path, capsys):
    def fake_get(self, url, params=None, headers=None):
        return FakeResponse(200, json.dumps(tweet_results["tweet"]))

    monkeypatch.setattr(cli.HttpClient, "get", fake_get)
    assert cli.main(["tweet", "1834231234567890123", "--analyze", "--engine", "python"]) == 0
    out = capsys.readouterr().out
    assert "@NASA  https://x.com/NASA/status/1834231234567890123" in out
    assert "♥ 48,213" in out and "sentiment" in out


def test_user_command_writes_store(monkeypatch, timeline_html, tmp_path, capsys):
    monkeypatch.setattr(cli.HttpClient, "get", lambda self, url, params=None, headers=None:
                        FakeResponse(200, timeline_html))
    db = tmp_path / "t.db"
    assert cli.main(["user", "NASA", "--no-retweets", "--dedupe", "-o", str(db)]) == 0
    assert cli.main(["user", "NASA", "-o", str(db)]) == 0
    err = capsys.readouterr().err
    assert "stored 2 new tweets" in err and "stored 1 new tweets" in err


def test_bad_input_exit_code(capsys):
    assert cli.main(["tweet", "not-an-id"]) == 2
    assert "error:" in capsys.readouterr().err


def test_tweet_batch_survives_one_failure(monkeypatch, tweet_results, capsys):
    from xscraper.http import HttpError

    def fake_get(self, url, params=None, headers=None):
        if params["id"] == "500":
            raise HttpError("giving up after 6 attempts (HTTP 503)", 503)
        return FakeResponse(200, json.dumps(tweet_results["tweet"]))

    monkeypatch.setattr(cli.HttpClient, "get", fake_get)
    # The tweets that worked are still printed, but the exit code reports the failure.
    assert cli.main(["tweet", "1834231234567890123", "500"]) == 1
    captured = capsys.readouterr()
    assert "failed: 500: giving up" in captured.err
    assert "@NASA" in captured.out


def test_user_batch_exit_code_reports_one_failure(monkeypatch, timeline_html, capsys):
    from xscraper.http import NotFound

    def fake_get(self, url, params=None, headers=None):
        if url.endswith("/nosuchuser"):
            raise NotFound("404 Not Found", 404)
        return FakeResponse(200, timeline_html)

    monkeypatch.setattr(cli.HttpClient, "get", fake_get)
    assert cli.main(["user", "NASA", "nosuchuser"]) == 1
    captured = capsys.readouterr()
    assert "@nosuchuser: 404" in captured.err and "@NASA" in captured.out


def test_analyze_missing_file(tmp_path, capsys):
    assert cli.main(["analyze", str(tmp_path / "nope.jsonl")]) == 2
    assert "no such file" in capsys.readouterr().err
