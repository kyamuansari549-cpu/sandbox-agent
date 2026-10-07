"""Phase 4 web tools. Every tool returns a string.

Contract (same as tools.py): tools NEVER raise into the agent loop.
Any failure — bad URL, timeout, HTTP error, unparseable page — comes back
as an "ERROR: ..." string so the LLM can read it and recover.

Honest design note: these tools run on the HOST (the agent's own machine),
not inside the Phase 2 Docker sandbox (--network none would block them).
They are read-only HTTP GETs — they fetch and read, they can never execute
anything — so the Phase 3 safety gate does not apply to them either.
"""

import re
import urllib.parse
from html.parser import HTMLParser

import requests

import config

# DuckDuckGo's plain-HTML endpoint: no API key, no JS, easy to parse.
_DDG_URL = "https://html.duckduckgo.com/html/"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

_FETCH_TIMEOUT = 15  # seconds, per request

# HTML blocks that are navigation/chrome, never article content.
_NOISE_RE = re.compile(
    r"<(script|style|nav|footer|header|aside|noscript|form)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def _truncate(out, limit):
    if len(out) > limit:
        out = out[:limit] + f"\n... [truncated: output exceeded {limit} chars]"
    return out


def _html_to_text(html):
    """Strip noise blocks + tags, unescape entities, collapse whitespace."""
    import html as _html_mod

    text = _NOISE_RE.sub(" ", html)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _html_mod.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def fetch_url(url, max_chars=None):
    """Fetch a web page and return its visible text (truncated).

    Only http/https URLs. Refuses non-HTML content (PDFs, images, …).
    """
    max_chars = config.MAX_OUTPUT_CHARS if max_chars is None else max_chars
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return f"ERROR: invalid URL: {url}"
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"ERROR: invalid or unsupported URL (only http/https allowed): {url}"

    try:
        resp = requests.get(
            url, timeout=_FETCH_TIMEOUT, headers={"User-Agent": _BROWSER_UA}
        )
    except requests.exceptions.Timeout:
        return f"ERROR: fetching {url} timed out after {_FETCH_TIMEOUT}s."
    except requests.exceptions.RequestException as exc:
        return f"ERROR: could not fetch {url}: {exc}"
    except Exception as exc:  # never crash the agent loop
        return f"ERROR: could not fetch {url}: {exc}"

    if resp.status_code != 200:
        return f"ERROR: {url} returned HTTP {resp.status_code}."

    content_type = (resp.headers.get("content-type") or "").lower()
    if content_type and "html" not in content_type and not content_type.startswith("text/"):
        return (
            f"ERROR: not a web page (content-type: {content_type or 'unknown'}) — "
            "I can only read HTML pages, not files like PDFs or images."
        )

    try:
        text = _html_to_text(resp.text)
    except Exception as exc:
        return f"ERROR: could not parse page {url}: {exc}"
    if not text:
        return f"ERROR: no readable text found at {url}."
    return _truncate(text, max_chars)


class _DDGParser(HTMLParser):
    """Extract (title, url, snippet) triples from DuckDuckGo's HTML results.

    Result links look like:
        <a class="result__a" href="//duckduckgo.com/l/?uddg=<url-encoded>&rut=…">Title</a>
    followed later by:
        <a class="result__snippet" …>snippet text</a>
    The real destination URL hides in the `uddg` query parameter.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._capture = None  # "title" | "snippet" | None
        self._buf = []
        self._href = ""

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        classes = dict(attrs).get("class", "")
        if "result__a" in classes:
            self._capture = "title"
            self._buf = []
            self._href = dict(attrs).get("href", "")
        elif "result__snippet" in classes:
            self._capture = "snippet"
            self._buf = []

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._capture:
            return
        text = _WS_RE.sub(" ", "".join(self._buf)).strip()
        if self._capture == "title":
            self.results.append({"title": text, "url": self._real_url(self._href)})
        elif self._capture == "snippet" and self.results:
            # Attach to the most recent result that has no snippet yet.
            if "snippet" not in self.results[-1]:
                self.results[-1]["snippet"] = text
        self._capture = None
        self._buf = []
        self._href = ""

    @staticmethod
    def _real_url(href):
        """Unwrap DuckDuckGo's redirect to the real destination URL."""
        if not href:
            return ""
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs and qs["uddg"]:
            return urllib.parse.unquote(qs["uddg"][0])
        if href.startswith("//"):
            return "https:" + href
        return href


def web_search(query, num_results=5):
    """Search the web (DuckDuckGo, no API key) and return numbered results.

    Each result: title, URL, snippet. Returns a clear message when nothing
    is found, and "ERROR: ..." on request failure.
    """
    query = (query or "").strip()
    if not query:
        return "ERROR: empty search query."
    try:
        num_results = max(1, min(int(num_results), 10))
    except (TypeError, ValueError):
        num_results = 5

    try:
        # NOTE: this endpoint expects a POST with form-encoded data.
        # A GET request is answered with a bot-challenge page (HTTP 202)
        # that contains no results.
        resp = requests.post(
            _DDG_URL,
            data={"q": query},
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": _BROWSER_UA},
        )
    except requests.exceptions.Timeout:
        return f"ERROR: web search timed out after {_FETCH_TIMEOUT}s."
    except requests.exceptions.RequestException as exc:
        return f"ERROR: web search failed: {exc}"
    except Exception as exc:  # never crash the agent loop
        return f"ERROR: web search failed: {exc}"

    if resp.status_code != 200:
        return f"ERROR: search returned HTTP {resp.status_code}."

    if "anomaly-modal" in resp.text or "challenge-form" in resp.text:
        return (
            "ERROR: DuckDuckGo served a bot-check page instead of search results "
            "(automated requests are being rate-limited). Wait a minute and try again."
        )

    try:
        parser = _DDGParser()
        parser.feed(resp.text)
    except Exception as exc:
        return f"ERROR: could not parse search results: {exc}"

    results = [r for r in parser.results if r.get("title") or r.get("url")]
    results = results[:num_results]
    if not results:
        return f"(no results found for: {query})"

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title') or '(no title)'}")
        lines.append(f"   {r.get('url') or '(no url)'}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)
