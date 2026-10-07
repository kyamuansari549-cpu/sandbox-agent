"""Phase 4 tests: fetch_url and web_search, plus tools.py wiring.

ALL network access is mocked — no real HTTP request from these tests
ever leaves the machine.
"""

import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_mod
import tools as tools_mod
import webtools as webtools_mod


# --------------------------------------------------------------------------
# helpers


class _FakeResp:
    def __init__(self, text="", status_code=200, content_type="text/html"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


def _mock_get(monkeypatch, fake):
    """Replace webtools' requests.get with a fake (records calls)."""
    calls = []
    if callable(fake) and not isinstance(fake, _FakeResp):
        def _get(*a, **k):
            calls.append((a, k))
            return fake(*a, **k)
    else:
        def _get(*a, **k):
            calls.append((a, k))
            return fake
    monkeypatch.setattr(webtools_mod.requests, "get", _get)
    return calls


def _mock_post(monkeypatch, fake):
    """Replace webtools' requests.post with a fake (records calls)."""
    calls = []
    if callable(fake) and not isinstance(fake, _FakeResp):
        def _post(*a, **k):
            calls.append((a, k))
            return fake(*a, **k)
    else:
        def _post(*a, **k):
            calls.append((a, k))
            return fake
    monkeypatch.setattr(webtools_mod.requests, "post", _post)
    return calls


_SAMPLE_PAGE = """\
<html><head><title>Test Page</title>
<style>body { color: red; }</style>
<script>var x = 1; alert(x);</script>
</head><body>
<nav><a href="/home">Home</a><a href="/about">About</a></nav>
<header>Site Header</header>
<h1>Hello World</h1>
<p>This is the <b>visible</b> article text &amp; it matters.</p>
<footer>Copyright 2026</footer>
</body></html>"""

_SAMPLE_DDG = """\
<html><body>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage1&amp;rut=abc123">First Result Title</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage1">First snippet text here.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result">
  <div class="links_main links_deep">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="https://direct.example.org/other">Second Result</a>
    </h2>
    <a class="result__snippet">Second snippet without redirect.</a>
  </div>
</div>
</body></html>"""


# --------------------------------------------------------------------------
# fetch_url


def test_fetch_url_strips_noise(monkeypatch):
    _mock_get(monkeypatch, _FakeResp(_SAMPLE_PAGE))
    out = webtools_mod.fetch_url("https://example.com/article")
    assert "Hello World" in out
    assert "visible" in out and "article text" in out
    assert "&" in out  # entity unescaped
    for noise in ("alert(x)", "color: red", "Site Header", "Copyright 2026"):
        assert noise not in out, f"noise leaked into output: {noise!r}"
    assert "<" not in out and ">" not in out


def test_fetch_url_non_200(monkeypatch):
    _mock_get(monkeypatch, _FakeResp("nope", status_code=404))
    out = webtools_mod.fetch_url("https://example.com/missing")
    assert out.startswith("ERROR:")
    assert "404" in out


def test_fetch_url_timeout(monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.Timeout("timed out")
    _mock_get(monkeypatch, _boom)
    out = webtools_mod.fetch_url("https://example.com/slow")
    assert out.startswith("ERROR:")
    assert "timed out" in out


def test_fetch_url_rejects_non_http_scheme(monkeypatch):
    calls = _mock_get(monkeypatch, _FakeResp("x"))
    out = webtools_mod.fetch_url("file:///etc/passwd")
    assert out.startswith("ERROR:")
    assert calls == []  # no request was even attempted


def test_fetch_url_refuses_pdf(monkeypatch):
    _mock_get(monkeypatch, _FakeResp("%PDF-1.4...", content_type="application/pdf"))
    out = webtools_mod.fetch_url("https://example.com/doc.pdf")
    assert out.startswith("ERROR:")
    assert "PDF" in out or "pdf" in out.lower()


def test_fetch_url_truncates(monkeypatch):
    long_html = "<html><body><p>" + "word " * 5000 + "</p></body></html>"
    _mock_get(monkeypatch, _FakeResp(long_html))
    out = webtools_mod.fetch_url("https://example.com/long", max_chars=100)
    assert len(out) <= 100 + 80  # content + truncation marker
    assert "truncated" in out


def test_fetch_url_connection_error(monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError("dns fail")
    _mock_get(monkeypatch, _boom)
    out = webtools_mod.fetch_url("https://nope.invalid/")
    assert out.startswith("ERROR:")


# --------------------------------------------------------------------------
# web_search


def test_web_search_parses_results(monkeypatch):
    calls = _mock_post(monkeypatch, _FakeResp(_SAMPLE_DDG))
    out = webtools_mod.web_search("example query", num_results=5)
    assert out.startswith("1. First Result Title")
    assert "https://example.com/page1" in out  # uddg redirect unwrapped
    assert "First snippet text here." in out
    assert "2. Second Result" in out
    assert "https://direct.example.org/other" in out
    assert "Second snippet without redirect." in out
    # sanity: the query actually went to DuckDuckGo via POST form data
    assert any("duckduckgo.com" in str(a) for a, k in calls)
    assert any(k.get("data", {}).get("q") == "example query" for a, k in calls)


def test_web_search_respects_num_results(monkeypatch):
    _mock_post(monkeypatch, _FakeResp(_SAMPLE_DDG))
    out = webtools_mod.web_search("example query", num_results=1)
    assert "1. First Result Title" in out
    assert "2." not in out


def test_web_search_empty_results(monkeypatch):
    _mock_post(monkeypatch, _FakeResp("<html><body><div>nothing here</div></body></html>"))

    class _FakeJsonResp(_FakeResp):
        def json(self):
            return {"AbstractText": "", "RelatedTopics": []}

    _mock_get(monkeypatch, _FakeJsonResp())
    out = webtools_mod.web_search("zzz unlikely query zzz")
    assert "no results found" in out
    assert not out.startswith("ERROR:")


def test_web_search_bot_check_page(monkeypatch):
    # Primary HTML endpoint serves a bot-check page -> falls back to the
    # instant-answer API. Mock both: fallback returns nothing useful here,
    # so we still get an ERROR.
    _mock_post(monkeypatch, _FakeResp(
        '<html><body><div class="anomaly-modal__mask">'
        '<form class="challenge-form"></form></div></body></html>'
    ))

    class _FakeJsonResp(_FakeResp):
        def json(self):
            return {"AbstractText": "", "RelatedTopics": []}

    _mock_get(monkeypatch, _FakeJsonResp())
    out = webtools_mod.web_search("anything")
    assert out.startswith("ERROR:")
    assert "bot-check" in out


def test_web_search_request_failure(monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError("offline")
    _mock_post(monkeypatch, _boom)
    _mock_get(monkeypatch, _boom)  # fallback also offline
    out = webtools_mod.web_search("anything")
    assert out.startswith("ERROR:")


def test_web_search_empty_query():
    out = webtools_mod.web_search("   ")
    assert out.startswith("ERROR:")


# --------------------------------------------------------------------------
# web_search fallback (instant-answer API when HTML endpoint is blocked)


class _FakeJsonResp(_FakeResp):
    def __init__(self, payload, status_code=200):
        super().__init__(text="", status_code=status_code)
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


_SAMPLE_JSON = {
    "Heading": "Horror fiction",
    "AbstractText": "Horror is a genre of speculative fiction...",
    "AbstractURL": "https://en.wikipedia.org/wiki/Horror_fiction",
    "RelatedTopics": [
        {"FirstURL": "https://example.com/gothic",
         "Text": "Gothic fiction – a genre combining horror and romance"},
        {"FirstURL": "https://example.com/king",
         "Text": "Stephen King – American horror author"},
    ],
}


def test_web_search_falls_back_on_http_202(monkeypatch):
    # Primary blocked with 202 -> instant-answer fallback serves results.
    _mock_post(monkeypatch, _FakeResp("", status_code=202))
    _mock_get(monkeypatch, _FakeJsonResp(_SAMPLE_JSON))
    out = webtools_mod.web_search("horror story", num_results=5)
    assert not out.startswith("ERROR:")
    assert "Horror fiction" in out
    assert "https://en.wikipedia.org/wiki/Horror_fiction" in out
    assert "Stephen King" in out
    assert "rate-limited" in out  # honesty note appended


def test_web_search_fallback_respects_num_results(monkeypatch):
    _mock_post(monkeypatch, _FakeResp("", status_code=202))
    _mock_get(monkeypatch, _FakeJsonResp(_SAMPLE_JSON))
    out = webtools_mod.web_search("horror story", num_results=1)
    assert "1. Horror fiction" in out
    assert "2." not in out


def test_web_search_fallback_bad_json(monkeypatch):
    _mock_post(monkeypatch, _FakeResp("", status_code=202))
    _mock_get(monkeypatch, _FakeJsonResp(ValueError("no json")))
    out = webtools_mod.web_search("anything")
    assert out.startswith("ERROR:")
    assert "fallback" in out


# --------------------------------------------------------------------------
# tools.py wiring


def test_wiring_tools_dict():
    assert tools_mod.TOOLS["fetch_url"] is webtools_mod.fetch_url
    assert tools_mod.TOOLS["web_search"] is webtools_mod.web_search


def test_wiring_schemas():
    names = {s["function"]["name"] for s in tools_mod.TOOL_SCHEMAS}
    assert "fetch_url" in names
    assert "web_search" in names
    for s in tools_mod.TOOL_SCHEMAS:
        fn = s["function"]
        if fn["name"] == "fetch_url":
            assert fn["parameters"]["required"] == ["url"]
            assert "url" in fn["parameters"]["properties"]
        if fn["name"] == "web_search":
            assert fn["parameters"]["required"] == ["query"]
            assert "query" in fn["parameters"]["properties"]
