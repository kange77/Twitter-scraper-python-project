import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tweet_results():
    return json.loads((FIXTURES / "tweet_results.json").read_text())


@pytest.fixture
def timeline_html():
    return (FIXTURES / "timeline_nasa.html").read_text()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = self.content.decode()
        self.headers = headers or {}

    def json(self):
        return json.loads(self.content)


class FakeClient:
    """Stands in for HttpClient: routes URLs/params to canned responses."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        return self.handler(url, params or {})
