"""The search router and the page reader (core/web.py).

Two capabilities, and one rule that is the reason most of these tests exist:
**nothing reports healthy because it is configured.** A backend that answered is
`ok`; one nothing has asked is `untried`; one that failed carries the reason and
the time. That is the difference between this and the `check()` that returns `ok`
by assumption (measured on Agent Reach's own web channel, whose reader answered
401 for search and 403 for a Reddit page it advertises as readable).

The network never happens here: `core/web.py` takes the host's fetch as one
seam, so every backend, the router, the reader and the doctor lines are driven
by fixtures through `configure()`. The suite's one live touch is the smoke test
at the bottom, which skips itself when the machine is offline.
"""
from __future__ import annotations

import http.client
import http.server
import json
import socket
import threading
import time
import urllib.parse

import pytest

from conftest import core_module
from settings_schema import DEFAULT_SETTINGS

PUBLIC_IP = "93.184.216.34"
JINA = "https://r.jina.ai/"


def _public_dns(host, port, *args, **kwargs):
    """Every name resolves to a public address, so the reader's destination
    check is exercised by the tests that mean to exercise it."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]


@pytest.fixture()
def web(monkeypatch):
    mod = core_module("web")
    # Process-global state, reset per test: the record (doctor), the cache, the
    # reader counters and the seams themselves.
    monkeypatch.setattr(mod, "_SEEN", {})
    monkeypatch.setattr(mod, "_QUOTA", {})
    monkeypatch.setattr(mod, "_READER_SEEN", {})
    monkeypatch.setattr(mod, "_CACHE", {})
    monkeypatch.setattr(mod, "_HTTP_GET", None)
    monkeypatch.setattr(mod, "_HTTP_IS_FN", True)
    # The reader's redirect seam too: leaving it set would send every reader
    # test to the host's REAL fetch (the fixture's own contract is "the seams
    # themselves", and this is a seam — forgetting it cost 5 minutes a run).
    monkeypatch.setattr(mod, "_HTTP_HOP", None)
    monkeypatch.setattr(mod, "_HTTP_HOP_IS_FN", True)
    monkeypatch.setattr(mod, "_SEARXNG_URL", "")
    monkeypatch.setattr(mod, "_LOG", None)
    monkeypatch.setattr(mod.socket, "getaddrinfo", _public_dns)
    return mod


class Fetch:
    """A recording stand-in for the host's `_http_get`.

    Routes are matched by substring, longest first, so a test can say what the
    web says without a server. Anything unrouted is a loud failure rather than a
    silent empty page: a test that forgets a route should not read as "the
    backend found nothing".
    """

    def __init__(self, **routes):
        self.routes = dict(routes)
        self.urls: list = []
        self.hold = None        # optional threading.Event, to hold a request open

    def __call__(self, url, timeout=10.0):
        self.urls.append(url)
        if self.hold is not None:
            self.hold.wait(5)
        # The reader URL WRAPS its target (`…/https://example.com/x`), so the
        # fallback is routed first: matching by substring alone would hand a
        # proxied request the target's fixture and nothing would ever fall back.
        if url.startswith(JINA) and "r.jina.ai" in self.routes:
            return self._value(self.routes["r.jina.ai"])
        for key in sorted(self.routes, key=len, reverse=True):
            if key in url:
                return self._value(self.routes[key])
        raise AssertionError(f"unrouted fetch: {url}")

    @staticmethod
    def _value(value):
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, bytes) else value.encode()


def serve(web, handler, searxng=""):
    web.configure(http_get=handler, searxng_url=searxng)
    web.cache_clear()
    return handler


SE_JSON = json.dumps({
    "items": [{"title": "&quot;CUDA&quot; error", "link": "https://so/q/1",
               "score": 5, "tags": ["cuda", "windows"]}],
    "quota_max": 300, "quota_remaining": 299}).encode()

# Taken from a REAL `lite.duckduckgo.com/lite/` response: single-quoted class
# attribute, href before class, and the redirect carrying an escaped ampersand.
# The parser that read only double quotes matched none of this and reported "no
# results", so the live-search test below is the other half of the pin.
DDG_LIVE_HTML = (
    b'<a rel="nofollow" href="//duckduckgo.com/l/?uddg='
    + urllib.parse.quote("https://www.example.com/alpha", safe="").encode()
    + b'&amp;rut=abc123" class=\'result-link\'>Alpha <b>title</b></a>\n</td>\n'
    + b'<td>&nbsp;</td><td class=\'result-snippet\'>snip &amp; one</td>')
# The same thing in the other quoting style, so neither can rot unnoticed.
DDG_HTML = (
    b'<a rel="nofollow" class="result-link" href="//duckduckgo.com/l/?uddg='
    + urllib.parse.quote("https://example.com/alpha", safe="").encode()
    + b'">Alpha <b>title</b></a><td class="result-snippet">snip &amp; one</td>')

LOCAL_PAGE = (b"<html><head><style>p{}</style></head><body>"
              b"<script>evil()</script><h1>Title &amp; Co</h1>"
              + b"<p>word " * 200 + b"</p></body></html>")


class TestBackends:
    """Each keyless backend parses its own payload — and only its own."""

    @pytest.mark.parametrize("page,url", [
        (DDG_LIVE_HTML, "https://www.example.com/alpha"),
        (DDG_HTML, "https://example.com/alpha"),
    ])
    def test_ddg_parses_the_real_markup_and_unwraps_the_redirect(self, web, page, url):
        f = serve(web, Fetch(**{"lite.duckduckgo.com": page}))
        (hit,) = web.ddg_search("anything")
        assert (hit.title, hit.snippet) == ("Alpha title", "snip & one")
        assert hit.url == url, (
            "the DDG redirector must be unwrapped: reading the redirect URL would "
            "fetch a redirect page instead of the result")
        assert f.urls[0].startswith("https://lite.duckduckgo.com/lite/?q=")

    # The real thing, from a burst of queries: DuckDuckGo answers with an
    # anti-bot page instead of results. Reporting that as "no results" was the
    # first version's bug, and it is indistinguishable from an empty web.
    DDG_CHALLENGE = (b'<form id="img-form" action="//duckduckgo.com/anomaly.js?'
                     b'sv=lite&cc=botnet&amp;ti=1789317280"></form>'
                     b'<form id="challenge-form" action="//duckduckgo.com/anomaly.js?'
                     b'sv=lite"></form>')

    def test_a_bot_challenge_is_a_failure_not_an_empty_web(self, web):
        serve(web, Fetch(**{"lite.duckduckgo.com": self.DDG_CHALLENGE}))
        with pytest.raises(RuntimeError, match="bot challenge"):
            web.ddg_search("anything")
        # ...and through the router it is NAMED, so the answer can say the
        # search was blocked instead of pretending nothing matched.
        results, notes = web.search("anything", source="ddg")
        assert results == []
        assert "bot challenge" in notes[0], notes
        assert "ddg FAILED (DuckDuckGo served a bot challenge" in web.search_note()

    def test_a_page_that_says_no_results_is_an_honest_empty(self, web):
        page = b"<html><body><h2>No results.</h2>" + b"<!-- filler -->" * 40 + b"</body></html>"
        serve(web, Fetch(**{"lite.duckduckgo.com": page}))
        assert web.ddg_search("anything") == []

    def test_a_reshaped_page_is_a_failure_not_an_empty_web(self, web):
        serve(web, Fetch(**{"lite.duckduckgo.com": b"<html><body>hello</body></html>"}))
        with pytest.raises(RuntimeError, match="unrecognised reply"):
            web.ddg_search("anything")

    def test_ddg_reads_the_href_whatever_the_attribute_order(self, web):
        page = (b'<a href="https://direct.example/x" class="result-link">Direct</a>')
        serve(web, Fetch(**{"lite.duckduckgo.com": page}))
        assert web.ddg_search("q")[0].url == "https://direct.example/x"

    def test_wikipedia_builds_a_url_for_every_hit(self, web):
        payload = json.dumps({"query": {"search": [
            {"title": "Blue Yeti", "snippet": "a <b>mic</b>"}]}}).encode()
        serve(web, Fetch(**{"wikipedia.org": payload}))
        hit = web.wikipedia_search("blue yeti")[0]
        assert hit.title == "Blue Yeti" and hit.snippet == "a mic"
        assert hit.url == "https://en.wikipedia.org/wiki/Blue_Yeti"

    def test_stackexchange_reports_the_quota_it_was_given(self, web):
        serve(web, Fetch(**{"stackexchange": SE_JSON}))
        hit = web.stackexchange_search("cuda")[0]
        assert hit.title == '"CUDA" error' and hit.url == "https://so/q/1"
        assert "cuda, windows" in hit.snippet and "score 5" in hit.snippet
        assert web._QUOTA["stackexchange"] == "quota 299/300", (
            "the quota is the reason an empty answer must not be read as "
            "'nothing exists', so it is kept where doctor can show it")

    def test_stackexchange_backoff_is_a_named_failure(self, web):
        payload = json.dumps({"items": [], "backoff": 30}).encode()
        serve(web, Fetch(**{"stackexchange": payload}))
        with pytest.raises(RuntimeError, match="backoff 30"):
            web.stackexchange_search("q")

    def test_hn_falls_back_to_the_discussion_url(self, web):
        payload = json.dumps({"hits": [{"title": "Show HN", "url": None,
                                        "objectID": "42", "points": 5,
                                        "num_comments": 2}]}).encode()
        serve(web, Fetch(**{"hn.algolia": payload}))
        hit = web.hn_search("show hn")[0]
        assert hit.url == "https://news.ycombinator.com/item?id=42"
        assert "5 points" in hit.snippet

    def test_github_reports_the_repository_url_and_stars(self, web):
        payload = json.dumps({"items": [{"full_name": "a/b", "description": "d",
                                         "html_url": "https://github.com/a/b",
                                         "stargazers_count": 12}]}).encode()
        serve(web, Fetch(**{"api.github.com": payload}))
        hit = web.github_search("a b")[0]
        assert hit.url == "https://github.com/a/b" and "12 stars" in hit.snippet

    def test_searxng_needs_a_configured_instance(self, web):
        serve(web, Fetch())
        with pytest.raises(RuntimeError, match="no SearXNG configured"):
            web.searxng_search("q")

    def test_searxng_names_an_instance_error(self, web):
        serve(web, Fetch(**{"127.0.0.1": json.dumps({"error": "format disabled"}).encode()}),
              searxng="http://127.0.0.1:8888")
        with pytest.raises(RuntimeError, match="format disabled"):
            web.searxng_search("q")


class TestRouting:
    """The route is an order, not a filter: every hit stays reachable."""

    def test_the_route_follows_the_shape_of_the_question(self, web):
        assert web._route("faster-whisper cuda error")[0] == "stackexchange"
        assert web._route("numpy repository release")[0] == "github"
        assert web._route("latest news today")[0] == "searxng"
        assert web._route("how tall is mount everest")[0] == "searxng"
        for route in (web._route("cuda error"), web._route("a repo"),
                      web._route("news"), web._route("anything else")):
            assert set(route) <= set(web.BACKENDS), route
            assert len(route) == len(set(route)), "a route must not repeat a backend"

    def test_a_failing_primary_falls_through_and_says_so(self, web):
        payload = json.dumps({"hits": [{"title": "HN answer", "url": "https://h/x",
                                        "objectID": "1", "points": 1,
                                        "num_comments": 0}]}).encode()
        f = serve(web, Fetch(**{"stackexchange": OSError("boom"),
                                "hn.algolia": payload, "searxng": b"{}"}))
        results, notes = web.search("cuda error", limit=1)
        assert [r.backend for r in results] == ["hn"], (
            "a failing primary must degrade to the next backend, not to nothing")
        assert any("stackexchange failed" in n for n in notes), notes
        assert not any("lite.duckduckgo.com" in u for u in f.urls), (
            "the walk stops once it has enough results")

    def test_total_failure_names_every_backend_and_the_reason(self, web):
        serve(web, Fetch(**{"stackexchange": OSError("boom"),
                            "hn.algolia": Exception("boom"),
                            "github": Exception("boom"),
                            "wikipedia.org": json.dumps({"query": {"search": []}}).encode(),
                            "lite.duckduckgo.com": b"<html>nothing</html>"}))
        results, notes = web.search("cuda error")
        assert results == [] and len(notes) == 1, notes
        text = notes[0]
        for name in ("stackexchange", "hn", "github", "ddg", "wikipedia"):
            assert name in text, (name, text)
        assert "nothing found" in text, (
            "an empty answer has to be distinguishable from a broken one")

    def test_an_unknown_source_is_refused_without_a_request(self, web):
        f = serve(web, Fetch())
        results, notes = web.search("q", source="nope")
        assert results == [] and "unknown source" in notes[0]
        assert not f.urls

    def test_an_explicit_source_uses_only_that_backend(self, web):
        f = serve(web, Fetch(**{"lite.duckduckgo.com": DDG_HTML}))
        results, _ = web.search("q", source="ddg")
        assert [r.backend for r in results] == ["ddg"]
        assert len(f.urls) == 1, f.urls

    def test_a_blank_query_is_not_a_search(self, web):
        f = serve(web, Fetch())
        assert web.search("   ")[0] == []
        assert not f.urls


class TestDiscipline:
    """The cache, the cap, and the record doctor reads."""

    def test_a_repeated_question_is_not_asked_twice(self, web):
        f = serve(web, Fetch(**{"stackexchange": SE_JSON}))
        web.search("cuda error")
        calls = len(f.urls)
        assert web.search("cuda error")[0][0].title == '"CUDA" error'
        assert len(f.urls) == calls, "the cache must answer the repeat"

    def test_an_expired_answer_is_dropped_not_just_ignored(self, web):
        """The cache used to RETURN None on expiry but keep the entry: a
        long-running bubble accumulated one dead slot per distinct query for
        as long as it lived."""
        serve(web, Fetch(**{"stackexchange": SE_JSON}))
        web.search("cuda error", source="stackexchange")
        web._store("stale-key", ["old"], [])
        assert "stale-key" in web._CACHE
        web._CACHE["stale-key"] = (time.time() - 1, ["old"], [])   # expired
        assert web._cached("stale-key") is None
        assert "stale-key" not in web._CACHE, "the dead entry was left behind"

    def test_the_cache_is_bounded(self, web):
        """Unbounded growth from a process that runs for weeks, asking a new
        question every time."""
        for i in range(web._CACHE_MAX * 2):
            web._store(f"q{i}", [i], [])
        assert len(web._CACHE) <= web._CACHE_MAX
        # and the NEWEST survive: eviction must not throw away what was just
        # asked for
        assert web._cached(f"q{web._CACHE_MAX * 2 - 1}") is not None

    def test_one_request_in_flight_per_backend(self, web):
        hold = threading.Event()
        f = serve(web, Fetch(**{"stackexchange": SE_JSON}))
        f.hold = hold
        first = []
        thread = threading.Thread(
            target=lambda: first.append(web.search("cuda one", source="stackexchange")))
        thread.start()
        try:
            time.sleep(0.2)          # let the first request take its slot
            results, notes = web.search("cuda two", source="stackexchange")
            assert results == [], (
                "a second request to the same backend must wait its turn, not "
                "hammer the site alongside the first")
            assert any("busy" in n for n in notes), notes
        finally:
            f.hold = None
            hold.set()
            thread.join(10)
        assert first and first[0][0], "the first search must still succeed"
        results, _ = web.search("cuda three", source="stackexchange")
        assert results, "the slot must be free once the first request finished"

    def test_a_used_backend_is_ok_and_an_untouched_one_is_untried(self, web):
        serve(web, Fetch(**{"stackexchange": SE_JSON}))
        web.search("cuda error", source="stackexchange")
        note = web.search_note()
        assert "stackexchange ok" in note and "quota 299/300" in note
        assert "ddg untried" in note, (
            "a backend nothing has asked must never be reported as healthy — "
            "that assumption is the defect this record exists to replace")
        assert "ddg ok" not in note

    def test_a_failed_backend_keeps_its_reason(self, web):
        serve(web, Fetch(**{"stackexchange": OSError("connection revoked")}))
        web.search("cuda error", source="stackexchange")
        assert "stackexchange FAILED (connection revoked" in web.search_note()

    def test_the_http_seam_accepts_a_resolver_and_a_plain_function(self, web):
        f = Fetch(**{"lite.duckduckgo.com": DDG_HTML})
        # A plain function: the natural thing to pass, and the form that must
        # not be called with no arguments.
        web.configure(http_get=f, searxng_url="")
        assert web.ddg_search("q")[0].title == "Alpha title"
        # A resolver: re-read on every use, which is what keeps a monkeypatched
        # host function and a reloaded setting live.
        swapped = Fetch(**{"lite.duckduckgo.com": DDG_HTML})
        holder = {"fn": swapped}
        web.configure(http_get=lambda: holder["fn"], searxng_url="")
        before = len(swapped.urls)
        assert web.ddg_search("q2")[0].title == "Alpha title"
        assert len(swapped.urls) == before + 1

    def test_format_results_names_the_failure_beside_the_hits(self, web):
        serve(web, Fetch(**{"stackexchange": SE_JSON,
                            "hn.algolia": OSError("boom")}))
        results, notes = web.search("cuda error")
        text = web.format_results(results, notes, "cuda error")
        assert text.startswith("Results for 'cuda error'")
        assert "https://so/q/1" in text, "URLs are what `read_top` needs"
        assert "note:" in text, "a failed backend is named even when others answered"


class TestReader:
    """Local first, the hosted reader only as a disclosed fallback."""

    def test_a_local_page_never_reaches_a_third_party(self, web):
        f = serve(web, Fetch(**{"example.com": LOCAL_PAGE}))
        text, via, problem = web.read_page("https://example.com/page")
        assert not problem and via == web.VIA_LOCAL
        assert text.startswith("via local fetch")
        assert "Title & Co" in text and "evil()" not in text, (
            "script bodies are not page text")
        assert all("r.jina.ai" not in u for u in f.urls), (
            "a page readable here must not be sent to a third-party reader")

    # A page with a lot of markup and no text is a JavaScript shell; a page that
    # is simply SMALL is small, and must be read here rather than handed to a
    # third party. The live proof found this the hard way: example.com (60
    # characters, perfectly readable) was being sent to the hosted reader.
    SHELL = (b"<html><head><script>" + b"app()\n" * 400
             + b"</script></head><body><div id=\"root\"></div></body></html>")

    def test_a_page_with_no_text_falls_back_and_says_who_fetched_it(self, web):
        f = serve(web, Fetch(**{"example.com": self.SHELL,
                                "r.jina.ai": b"Markdown Content:\nReader copy"}))
        text, via, problem = web.read_page("https://example.com/app")
        assert not problem and via.startswith(web.VIA_JINA)
        assert text.startswith("via Jina Reader (third-party)")
        assert "Reader copy" in text
        assert any("r.jina.ai" in u for u in f.urls), "a JS shell is not a page"

    def test_a_short_page_is_read_here_and_not_sent_away(self, web):
        page = b"<html><body><p>This domain is for use in examples.</p></body></html>"
        f = serve(web, Fetch(**{"example.com": page}))
        text, via, problem = web.read_page("https://example.com")
        assert not problem and via == web.VIA_LOCAL
        assert "for use in examples" in text
        assert all("r.jina.ai" not in u for u in f.urls), (
            "a small page that reads fine here must not be sent to a third-party "
            "reader: the address is the user's")

    def test_a_challenge_page_is_not_read_as_the_page(self, web):
        # LONG enough to pass the useless-page test on its own, so the only
        # reason to fall back is that the page is a challenge — otherwise this
        # guard would pass with the detection removed (it did, until the fixture
        # was padded: the raw mutation survived, and that is what found this).
        challenge = (b"<html><body><h1>Just a moment...</h1>"
                     + b"<p>Checking your browser " + b"filler " * 120
                     + b"</p></body></html>")
        f = serve(web, Fetch(**{"example.com": challenge,
                                "r.jina.ai": b"Markdown Content:\nThe real text"}))
        text, via, _ = web.read_page("https://example.com/x")
        assert "The real text" in text and via.startswith(web.VIA_JINA)
        assert any("r.jina.ai" in u for u in f.urls), (
            "the local body was extracted into 40 characters of challenge text: "
            "it has to be recognised as a block page, not returned as the page")

    def test_a_blocked_site_is_named_rather_than_returned_as_text(self, web):
        blocked = (b"Title:\nWarning: Target URL returned error 403: Forbidden\n"
                   b"You've been blocked by network security.\n"
                   b"To continue, log in to your Reddit account")
        serve(web, Fetch(**{"example.com": b"<html><body></body></html>",
                            "r.jina.ai": blocked}))
        text, via, problem = web.read_page("https://example.com/r")
        assert text == "" and via == "", (text[:80], via)
        assert "refuses automated readers" in problem, problem
        # The signature that matched is NAMED, so the reason is the page's own
        # words rather than a generic "blocked". Which one matches depends on the
        # order the signatures are checked in, so the test allows either of the
        # two this fixture contains.
        assert ("network security block" in problem
                or "login wall" in problem), problem

    def test_a_stale_copy_is_announced(self, web):
        serve(web, Fetch(**{"example.com": b"<html></html>",
                            "r.jina.ai": b"Warning: This is a cached snapshot of the "
                                         b"original page\nMarkdown Content:\nOld copy"}))
        text, via, _ = web.read_page("https://example.com/x")
        assert "cached snapshot" in via.lower() and "cached snapshot" in text.lower()
        assert via in text.splitlines()[0], (
            "the header and the reported vocabulary must agree")

    def test_a_total_reader_failure_names_both_attempts(self, web):
        serve(web, Fetch(**{"example.com": b"<html></html>",
                            "r.jina.ai": OSError("no route")}))
        text, via, problem = web.read_page("https://example.com/x")
        assert text == "" and via == ""
        assert "local fetch" in problem and "third-party reader failed" in problem, problem

    def test_long_text_is_truncated_with_the_size_named(self, web):
        serve(web, Fetch(**{"example.com": LOCAL_PAGE}))
        text, _via, _p = web.read_page("https://example.com/x", max_chars=500)
        assert len(text) <= 700 and "truncated" in text

    @pytest.mark.parametrize("url,expected", [
        ("http://127.0.0.1:8080/admin", "private network"),
        ("http://[::1]/x", "private network"),
        ("http://10.0.0.5/x", "private network"),
        ("http://169.254.169.254/latest/meta-data/", "private network"),
        ("http://192.168.1.1/", "private network"),
        ("http://router/", "not a public host"),
        ("http://printer.local/", "not a public host"),
        ("file:///etc/passwd", "not an http(s) address"),
        ("javascript:alert(1)", "not an http(s) address"),
        ("https://user:pw@example.com/x", "credentials"),
        ("", "no address given"),
        # `urlsplit` defers the port check to `.port`, which RAISES — and this
        # address comes from a model, so a bad port is a refusal like any other.
        ("http://example.com:99999/x", "port that is not a number"),
        ("http://example.com:abc/x", "port that is not a number"),
        ("http://example.com:-1/x", "port that is not a number"),
    ])
    def test_the_reader_refuses_this_machine_and_the_lan(self, web, url, expected):
        f = serve(web, Fetch())
        text, via, problem = web.read_page(url)
        assert text == "" and expected in problem, problem
        assert not f.urls, "a refused address must not be fetched at all"

    def test_a_name_that_resolves_to_this_machine_is_refused(self, web, monkeypatch):
        monkeypatch.setattr(web.socket, "getaddrinfo",
                            lambda host, port, *a, **k: [
                                (socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                 ("127.0.0.1", port))])
        f = serve(web, Fetch())
        text, _via, problem = web.read_page("https://rebind.example/x")
        assert text == "" and "private address" in problem, problem
        assert not f.urls, (
            "the destination is checked, not the spelling: a public name landing "
            "on this machine is still this machine")

    def test_a_page_that_does_not_resolve_says_so(self, web, monkeypatch):
        def _boom(host, port, *a, **k):
            raise socket.gaierror("nodename nor servname provided")
        monkeypatch.setattr(web.socket, "getaddrinfo", _boom)
        text, _via, problem = web.read_page("https://nowhere.example/x")
        assert text == "" and "cannot be resolved" in problem, problem

    # ---------------------------------------------- the redirect hole (SSRF)
    # `_public_url` checks ONE address, and a redirect is a NEW one. A
    # model-supplied URL could pass every rule and then answer
    # `302 Location: http://169.254.169.254/…` — the cloud metadata endpoint —
    # or point at the LAN, and the page landed in the transcript as though it
    # were public. The host's urlopen followed the chain before `core.web` saw
    # anything, so the reader now takes a seam that does not follow by itself
    # and walks the hops here.

    def _hop_fetch(self, web, hops):
        """A hop seam playing a chain: {url: (body_bytes, location)}."""
        def hop(url, timeout=10.0):
            body, location = hops.get(url, (b"", ""))
            return body, location
        return hop

    def test_a_redirect_to_a_private_address_is_refused(self, web):
        web.configure(http_get_hop=self._hop_fetch(web, {
            "https://example.com/start": (b"", "http://169.254.169.254/latest/meta-data/"),
        }))
        text, _via, problem = web.read_page("https://example.com/start")
        assert text == "" and "169.254.169.254" in problem, (text, problem)
        assert "not fetched" in problem, problem
        assert "on this machine or a private network" in problem, problem

    def test_a_redirect_chain_is_walked_and_every_hop_rechecked(self, web):
        """A public hop, then a LAN hop: the second one is refused. A walk that
        only rechecked the FIRST hop would read the LAN page here."""
        web.configure(http_get_hop=self._hop_fetch(web, {
            "https://example.com/a": (b"", "https://cdn.example.net/b"),
            "https://cdn.example.net/b": (b"", "http://192.168.1.1/router"),
        }))
        text, _via, problem = web.read_page("https://example.com/a")
        assert text == "" and "192.168.1.1" in problem, (text, problem)

    def test_a_relative_location_is_resolved_and_still_public(self, web):
        """`Location: /b` is what a server actually sends; resolving it is not
        a hole, and a site that bounces you to itself must still read."""
        body = b"<html><body><h1>Fixture</h1>" + b"<p>sentence " * 60 + b"</p></body></html>"
        web.configure(http_get_hop=self._hop_fetch(web, {
            "https://example.com/a": (b"", "/b"),
            "https://example.com/b": (body, ""),
        }))
        text, via, problem = web.read_page("https://example.com/a")
        assert not problem and via == web.VIA_LOCAL, (text[:80], problem)
        assert "sentence" in text

    def test_an_endless_redirect_loop_is_refused_not_followed(self, web):
        web.configure(http_get_hop=self._hop_fetch(web, {
            "https://example.com/a": (b"", "https://example.com/a"),
        }))
        text, _via, problem = web.read_page("https://example.com/a")
        assert text == "" and "redirected more than" in problem, (text, problem)

    def test_read_results_skips_a_hit_without_an_address(self, web):
        f = serve(web, Fetch(**{"example.com": LOCAL_PAGE}))
        hits = [web.Result("no url", "s", "", "ddg"),
                web.Result("has url", "s", "https://example.com/a", "ddg")]
        blocks, notes = web.read_results(hits, 3, max_chars=1000)
        assert len(blocks) == 1 and not notes
        assert blocks[0].startswith("--- page 1: https://example.com/a (")
        assert len(f.urls) == 1

    def test_read_results_clamps_the_count(self, web):
        serve(web, Fetch(**{"example.com": LOCAL_PAGE}))
        hits = [web.Result(f"t{i}", "s", f"https://example.com/{i}", "ddg")
                for i in range(5)]
        blocks, _notes = web.read_results(hits, 99, max_chars=400)
        assert len(blocks) == web.READ_TOP_MAX, (
            "a model asking for 99 pages gets the documented ceiling")

    def test_a_read_that_fails_is_a_note_not_a_block(self, web):
        serve(web, Fetch(**{"example.com": OSError("boom")}))
        hits = [web.Result("t", "s", "https://example.com/a", "ddg")]
        blocks, notes = web.read_results(hits, 1)
        assert blocks == [] and notes and "could not read" in notes[0]


class TestLastProblem:
    """The reason a quiet backend is quiet, for callers that must REPORT it.

    The news tool used to print "offline?" while DuckDuckGo was in fact
    refusing with a bot challenge — a wrong diagnosis the user cannot act on.
    This is the accessor it asks instead of guessing.
    """

    def test_a_working_backend_has_no_problem_to_report(self, web):
        web._record(web._SEEN, "ddg", True)
        assert web.last_problem("ddg") == ""

    def test_the_recorded_sentence_is_what_comes_back(self, web):
        web._record(web._SEEN, "ddg", False, "DuckDuckGo served a bot challenge")
        assert web.last_problem("ddg") == "DuckDuckGo served a bot challenge"

    def test_an_untried_or_unknown_backend_reports_nothing(self, web):
        assert web.last_problem("ddg") == ""          # never asked
        assert web.last_problem("nonsense") == ""     # not a backend at all
        web._record(web._SEEN, "hn", False)          # failure with no reason
        assert web.last_problem("hn") == ""


class TestDoctor:
    """The lines that make "why did that answer lie" answerable."""

    def test_untried_is_not_ok(self, web):
        lines = web.doctor_lines()
        assert lines[0].startswith("search: ")
        assert "untried" in lines[0] and " ok" not in lines[0].replace("untried", ""), (
            "a fresh process has proven nothing, and the line must say exactly that")
        assert lines[1].startswith("reader: ")

    def test_the_searxng_entry_is_a_real_local_probe(self, web, monkeypatch):
        serve(web, Fetch(), searxng="http://127.0.0.1:8888")
        seen = {}

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _connect(addr, timeout=None):
            seen["addr"] = addr
            seen["timeout"] = timeout
            return _Conn()

        monkeypatch.setattr(web.socket, "create_connection", _connect)
        assert "searxng ok" in web.search_note()
        assert seen["addr"] == ("127.0.0.1", 8888) and seen["timeout"] <= 1.0, seen

    def test_a_dead_searxng_says_not_running_and_an_empty_setting_says_off(self, web, monkeypatch):
        def _refuse(addr, timeout=None):
            raise ConnectionRefusedError("closed")
        monkeypatch.setattr(web.socket, "create_connection", _refuse)
        serve(web, Fetch(), searxng="http://127.0.0.1:8888")
        assert "searxng not running" in web.search_note()
        serve(web, Fetch(), searxng="")
        assert "searxng off" in web.search_note()

    def test_the_reader_line_names_which_reader_was_used(self, web):
        serve(web, Fetch(**{"example.com": LOCAL_PAGE}))
        web.read_page("https://example.com/a")
        assert "local fetch ok" in web.reader_note()
        assert "Jina fallback unused" in web.reader_note()
        serve(web, Fetch(**{"example.com": b"<html></html>",
                            "r.jina.ai": b"Markdown Content:\nx"}))
        web.read_page("https://example.com/b")
        assert "Jina fallback used" in web.reader_note()

    def test_a_refused_reader_is_recorded_with_its_reason(self, web):
        serve(web, Fetch(**{"example.com": b"<html></html>",
                            "r.jina.ai": OSError("no route")}))
        web.read_page("https://example.com/x")
        note = web.reader_note()
        assert "local fetch FAILED" in note and "Jina fallback refused" in note


class TestTheCheckedAddressIsTheOneFetched:
    """The pin: the name is checked ONCE, and the socket goes where it pointed.

    `_public_target` validated a NAME and the transport resolved that name
    again, so a name server was free to answer the policy with a public address
    and the socket with 127.0.0.1 — DNS rebinding, and no check on the name can
    close it. The reader now hands each hop's checked addresses to the fetch
    (`connect_to`), and the host's fetch dials them while keeping the name for
    the Host header and the TLS identity.
    """

    def _pinning_hop(self, hops, seen):
        def hop(url, timeout=10.0, connect_to=None):
            seen.append((url, connect_to))
            return hops.get(url, (b"", ""))
        return hop

    def test_the_policy_hands_back_the_addresses_it_checked(self, web):
        clean, pins, problem = web._public_target("https://example.com/x")
        assert not problem and clean == "https://example.com/x", (clean, problem)
        assert pins == [(PUBLIC_IP, 443)], pins
        _clean, pins, _problem = web._public_target("http://example.com/x")
        assert pins == [(PUBLIC_IP, 80)], pins

    def test_the_first_hop_is_handed_the_checked_address(self, web):
        seen = []
        web.configure(http_get_hop=self._pinning_hop({}, seen))
        web.read_page("https://example.com/start")
        assert seen and seen[0][0] == "https://example.com/start", seen
        assert seen[0][1] == [(PUBLIC_IP, 443)], (
            f"the fetch was not told which address was checked: {seen[0][1]!r}")

    def test_every_redirect_hop_is_handed_its_own_checked_address(
            self, web, monkeypatch):
        """A chain is a new address each time, so it is a new pin each time.

        A walk that carried the FIRST hop's address forward would fetch the final
        hop at the first hop's host — the same class of mistake as not checking
        the redirect at all.
        """
        first, second = PUBLIC_IP, "1.1.1.1"

        def dns(host, port, *a, **k):
            ip = first if host == "example.com" else second
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

        monkeypatch.setattr(web.socket, "getaddrinfo", dns)
        seen = []
        web.configure(http_get_hop=self._pinning_hop({
            "https://example.com/a": (b"", "https://cdn.example.net/b"),
            "https://cdn.example.net/b": (b"", ""),
        }, seen))
        web.read_page("https://example.com/a")
        assert seen[0][1] == [(first, 443)], seen
        landed = [c for u, c in seen if u.startswith("https://cdn.example.net/b")]
        assert landed and landed[0] == [(second, 443)], (
            f"a redirect hop was not pinned to its own checked address: {seen}")

    def test_a_hop_that_cannot_pin_is_told_what_it_gives_up(self, web,
                                                            monkeypatch):
        """The seam is detected, not required, so a partial install still reads
        — but it is TOLD, in the journal, that the window it cannot close stays
        open. Silence here would look like a closed hole."""
        warnings = []

        class _Log:
            @staticmethod
            def warning(msg, *args):
                warnings.append(msg % args if args else msg)
            debug = exception = warning

        monkeypatch.setattr(web, "_LOG", _Log)
        fetched = []

        def hop(url, timeout=10.0):        # an older caller's seam: no pin
            fetched.append(url)
            return b"", ""

        web.configure(http_get_hop=hop)
        web.read_page("https://example.com/x")
        assert fetched, "a seam that cannot pin must still fetch"
        assert any("cannot be pinned" in w for w in warnings), warnings
        assert any("rebinding" in w for w in warnings), warnings

    def test_a_reader_with_no_hop_seam_says_both_things_it_gives_up(
            self, web, monkeypatch):
        """A partial install still reads, but the journal must not report a
        closed hole: the one-shot path follows redirects unchecked AND
        resolves the name a second time."""
        warnings = []

        class _Log:
            @staticmethod
            def warning(msg, *args):
                warnings.append(msg % args if args else msg)
            debug = exception = warning

        monkeypatch.setattr(web, "_LOG", _Log)
        monkeypatch.setattr(web, "_HTTP_HOP", None)
        monkeypatch.setattr(web, "_HTTP_GET",
                            lambda url, timeout=10.0: b"<html>x</html>")
        web.read_page("https://example.com/x")
        said = " ".join(warnings)
        assert "redirects are followed unchecked" in said, warnings
        assert "resolved a second time" in said, warnings

    def test_a_seam_that_vanishes_mid_walk_is_not_a_silent_unpin(
            self, web, monkeypatch):
        """`_hop` returning None means the seam went away while the chain was
        being walked. Falling back is right; falling back SILENTLY would look
        like a pinned read in the journal, which is the whole accounting."""
        warnings = []

        class _Log:
            @staticmethod
            def warning(msg, *args):
                warnings.append(msg % args if args else msg)
            debug = exception = warning

        monkeypatch.setattr(web, "_LOG", _Log)
        monkeypatch.setattr(web, "_HTTP_HOP_IS_FN", False)
        monkeypatch.setattr(web, "_HTTP_HOP", lambda: None)     # resolver
        monkeypatch.setattr(web, "_HTTP_GET",
                            lambda url, timeout=10.0: b"<html>x</html>")
        web.read_page("https://example.com/x")
        said = " ".join(warnings)
        assert "vanished mid-walk" in said, warnings
        assert "resolved" in said, warnings

    def test_a_name_that_never_answers_does_not_hold_the_turn(self, web,
                                                             monkeypatch):
        """`getaddrinfo` has no timeout, and this runs inside a spoken turn."""
        monkeypatch.setattr(web, "_DNS_TIMEOUT_S", 0.2)

        def stuck(host, port, *a, **k):
            time.sleep(2.0)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

        monkeypatch.setattr(web.socket, "getaddrinfo", stuck)
        start = time.monotonic()
        text, _via, problem = web.read_page("https://slow.example/x")
        elapsed = time.monotonic() - start
        assert text == "" and "could not be resolved within" in problem, problem
        assert elapsed < 1.5, f"the resolver held the turn for {elapsed:.1f}s"


class TestHandsoffWiring:
    """The app's side: the tools, the gate, the doctor, the delegations."""

    def test_the_app_injects_the_redirect_checking_fetch(self, H):
        """The hole this closes was real, and a MISSING seam would reopen it
        silently — the reader would warn and read in one shot, which is exactly
        the behaviour that was wrong. So the wiring itself is pinned: the app
        hands `core.web` a hop fetch, and that fetch refuses to follow.
        """
        assert H._web._HTTP_HOP is not None, (
            "the host did not inject the redirect-checking fetch")
        assert callable(H._http_get_hop)
        # One hop = one request: a 3xx is handed back, not chased.
        assert H._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "http://169.254.169.254/") is None, (
            "the host's hop fetch follows redirects — the reader cannot check "
            "an address it never sees")
        # ...and it must be able to accept the addresses the reader checked:
        # a seam that cannot take them resolves the name a SECOND time, which
        # is the rebinding window the pin exists to close. The reader degrades
        # to a warning, so nothing else would fail if this regressed.
        assert H._web._accepts_connect_to(H._http_get_hop), (
            "the host's hop fetch cannot be handed the checked addresses, so "
            "the reader resolves every name twice")

    def test_the_pinned_fetch_dials_the_checked_address_and_keeps_the_name(
            self, H):
        """End to end and offline: the socket goes to the pin, the name stays.

        `example.invalid` is reserved (RFC 2606) and cannot resolve, so the
        ONLY way this request can reach the local server is through the pin —
        which is what makes this a proof rather than a rehearsal. What the
        server sees is the other half: a Host header of the NAME, because a pin
        that rewrote the request to an IP would break virtual hosts and the TLS
        identity (`_pinned_connection` keeps `self.host`).
        """
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["host"] = self.headers.get("Host")
                seen["path"] = self.path
                body = b"<html><body>pinned</body></html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):        # keep the suite's output clean
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            body, location = H._http_get_hop(
                f"http://example.invalid:{port}/x?q=1", 5.0,
                connect_to=[("127.0.0.1", port)])
        finally:
            srv.shutdown()
            srv.server_close()
        assert not location and b"pinned" in body, (body, location)
        assert seen.get("host", "").startswith("example.invalid"), seen
        assert seen.get("path") == "/x?q=1", seen

    def test_the_pin_replaces_the_dial_and_nothing_else(self, H, monkeypatch):
        """`http.client` dials through `self._create_connection`, and the pin
        replaces exactly that callable — so the address dialled is the checked
        one while `host` (Host header, TLS SNI, certificate check) stays the
        name. Pinned here rather than described in the docstring."""
        dialled = []

        def fake(address, timeout=None, source_address=None):
            dialled.append((address, timeout))
            raise OSError("refused by the test")

        monkeypatch.setattr(H.socket, "create_connection", fake)
        conn_class = H._pinned_connection(http.client.HTTPSConnection,
                                          [("93.184.216.34", 443)])
        conn = conn_class("example.com", 443, timeout=3)
        assert conn.host == "example.com", (
            "the Host header and the TLS SNI must stay the name")
        assert conn.port == 443, conn.port
        with pytest.raises(OSError):
            conn.connect()
        assert dialled == [( ("93.184.216.34", 443), 3 )], dialled

    def test_a_second_checked_address_is_still_tried(self, H, monkeypatch):
        """A dual-stack name keeps its fallback: the pins are tried in the
        order the policy checked them, so a family this machine cannot reach
        costs a connection attempt and not the fetch."""
        dialled = []

        def fake(address, timeout=None, source_address=None):
            dialled.append(address)
            if len(dialled) == 1:
                raise OSError("no route to host")
            return socket.socket()          # a real socket; never connected

        monkeypatch.setattr(H.socket, "create_connection", fake)
        conn_class = H._pinned_connection(
            http.client.HTTPConnection,
            [("93.184.216.34", 80), ("1.1.1.1", 80)])
        conn = conn_class("example.com", 80, timeout=3)
        conn.connect()
        assert dialled == [("93.184.216.34", 80), ("1.1.1.1", 80)], dialled

    def test_the_pinned_fetch_never_resolves_the_NAME(self, H, monkeypatch):
        """The rebinding window is the transport's second resolution of a NAME,
        and this is the property that closes it: the only string the pinned dial
        hands the resolver is the address the policy checked. `getaddrinfo` is
        still called on that LITERAL (a numeric address is returned as-is, no
        server consulted); the URL's host name never reaches it."""
        asked = []
        real = H.socket.getaddrinfo

        def watching(host, port, *args, **kwargs):
            asked.append((str(host), port))
            return real(host, port, *args, **kwargs)

        monkeypatch.setattr(H.socket, "getaddrinfo", watching)
        probe = socket.socket()                     # a port nothing is on
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
        probe.close()
        with pytest.raises(OSError):
            H._http_get_hop("http://rebinding.example/x", 2.0,
                            connect_to=[("127.0.0.1", closed)])
        assert asked == [("127.0.0.1", closed)], (
            "the pinned fetch resolved something other than the checked "
            f"address: {asked}")


    def test_the_news_tool_names_the_recorded_reason(self, H, monkeypatch):
        """A quiet headline source is REPORTED with its cause, never guessed.

        The tool said "offline?" while DuckDuckGo was refusing with a bot
        challenge. The sentence now comes from the web layer's own record, so
        the user is told the thing they can act on.
        """
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H, "_world_events", lambda scope, n: ([], True))
        monkeypatch.setattr(H._web, "_SEEN", {})
        H._web._record(H._web._SEEN, "ddg", False,
                       "DuckDuckGo served a bot challenge")
        out = belt.world_events(3)
        assert "offline" not in out, out
        assert "refused" in out and "DuckDuckGo served a bot challenge" in out, out

    def test_the_news_tool_does_not_invent_a_reason(self, H, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setattr(H, "_world_events", lambda scope, n: ([], True))
        monkeypatch.setattr(H._web, "_SEEN", {})       # nothing was recorded
        out = belt.world_events(3)
        assert "refused" in out and "offline" not in out, out
        # ...and an empty-but-healthy answer is not a failure at all
        monkeypatch.setattr(H, "_world_events", lambda scope, n: ([], False))
        assert belt.world_events(3) == "no world headlines right now"

    def test_the_tools_exist_and_are_gated(self, H):
        names = {t["function"]["name"] for t in H.TOOLS}
        assert {"web_search", "read_page"} <= names
        # ...and the rest of the web surface the model is handed: the shape of
        # the schema IS the model's ability, so a tool that is not here does
        # not exist as far as the AI is concerned, however the module is wired.
        assert {"lookup_fact", "world_events"} <= names, sorted(names)
        props = next(t["function"]["parameters"]["properties"]
                     for t in H.TOOLS if t["function"]["name"] == "web_search")
        assert {"source", "read_top"} <= set(props), (
            "the backend and the two-stage read are parameters of ONE tool: the "
            "fixed prompt cannot afford five more schemas")
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt._perm["web_access"] = False
        for call in (("web_search", {"query": "x"}),
                     ("read_page", {"url": "https://example.com"})):
            out, err = belt.execute(*call)
            assert err and out.startswith("REFUSED") and "web_access" in out, call

    def test_a_search_and_a_read_through_the_tools(self, H, monkeypatch):
        monkeypatch.setattr(H._web.socket, "getaddrinfo", _public_dns)
        body = (b"<html><body><h1>Fixture</h1>" + b"<p>sentence " * 60 + b"</p></body></html>")
        def fake(url, timeout=10.0):
            if "stackexchange" in url:
                return json.dumps({
                    "items": [{"title": "An answer", "link": "https://so.example/q/1",
                               "score": 1, "tags": ["t"]}],
                    "quota_max": 300, "quota_remaining": 299}).encode()
            if "so.example" in url:
                return body
            raise AssertionError(url)
        monkeypatch.setattr(H, "_http_get", fake)
        # The reader goes through the REDIRECT-CHECKING seam, so the app's hop
        # is patched too — and recording which seam served the read is the
        # point: a reader that quietly used the plain fetch would follow an
        # unchecked redirect.
        hops = []
        def fake_hop(url, timeout=10.0):
            hops.append(url)
            return fake(url, timeout), ""
        monkeypatch.setattr(H, "_http_get_hop", fake_hop)
        monkeypatch.setattr(H, "_web", H._web)      # the app's own module
        monkeypatch.setattr(H._web, "_HTTP_HOP", fake_hop)
        H._web.cache_clear()
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("web_search", {"query": "a cuda error"})
        assert not err and "https://so.example/q/1" in out, out
        out2, err2 = belt.execute("web_search", {"query": "a cuda error", "read_top": 1})
        assert not err2 and "sentence" in out2 and "via local fetch" in out2, out2
        assert any("so.example" in u for u in hops), (
            f"the read did not go through the redirect-checking seam: {hops}")

    def test_reading_is_off_unless_asked(self, H, monkeypatch):
        seen = []
        def fake(url, timeout=10.0):
            seen.append(url)
            return json.dumps({"items": [{"title": "T", "link": "https://so.example/q/1",
                                          "score": 1, "tags": []}]}).encode()
        monkeypatch.setattr(H, "_http_get", fake)
        H._web.cache_clear()
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("web_search", {"query": "a cuda error",
                                              "source": "stackexchange"})
        assert not err and len(seen) == 1, seen
        assert "so.example" not in "".join(seen), (
            "read_top of 0 must not fetch the result pages")

    def test_doctor_carries_the_two_lines(self, H, monkeypatch):
        monkeypatch.setattr(H, "_http_get", lambda url, timeout=10.0: (_ for _ in ()).throw(
            OSError("offline")))
        text = H.run_doctor()
        assert "\nsearch: " in text and "\nreader: " in text, text
        assert "appearance:" in text, "the web lines must not replace the others"

    def test_the_app_hands_the_module_resolvers_not_values(self, H):
        """The seam that keeps a monkeypatched fetch and a reloaded setting live.

        A bound function object or a copied string would leave both stale, so
        the app must pass something that reads `_http_get` / SETTINGS per use.
        """
        calls = []
        def fake(url, timeout=10.0):
            calls.append(url)
            return b'<a class="result-link" href="https://example.com/x">T</a>'
        original = H._http_get
        original_settings = H.SETTINGS.get("searxng_url")
        try:
            H._http_get = fake
            H._web.cache_clear()
            assert H._web.ddg_search("resolver check")[0].url == "https://example.com/x"
            assert calls, "the module must call the CURRENT H._http_get"
            # The setting too: a copied string would freeze whatever was set at
            # import, so a live save would point the router somewhere else than
            # the settings app shows.
            H.SETTINGS["searxng_url"] = "http://127.0.0.1:9999"
            assert H._web._searxng_url() == "http://127.0.0.1:9999", (
                "the module must read the CURRENT searxng_url setting")
        finally:
            H._http_get = original
            H.SETTINGS["searxng_url"] = original_settings

    def test_world_events_and_lookup_still_use_the_delegations(self, H, monkeypatch):
        # The Wikipedia REST summary is deliberately NOT a standard article, so
        # `lookup_fact` has to reach its fallback — which is the delegated
        # `_wiki_search`. A fake that answered the summary endpoint instead would
        # let the delegation be deleted with this test still green (it did).
        def fake(url, timeout=10.0):
            if "duckduckgo" in url:
                return (b'<a class="result-link" href="https://example.com/n">'
                        b'Quake hits</a><td class="result-snippet">a big earthquake</td>')
            if "rest_v1/page/summary" in url:
                return json.dumps({"type": "disambiguation"}).encode()
            return json.dumps({"query": {"search": [{"title": "Blue Yeti",
                                                     "snippet": "a <b>mic</b>"}]}}).encode()
        monkeypatch.setattr(H, "_http_get", fake)
        H._web.cache_clear()
        events, _degraded = H._world_events("news", 1)
        assert events and events[0]["urgent"], events
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("lookup_fact", {"topic": "Blue Yeti"})
        assert not err and "Blue Yeti" in out, out

    def test_the_searxng_setting_is_known_and_normalised(self):
        assert "searxng_url" in DEFAULT_SETTINGS
        from core.settings import coerce_settings
        s = dict(DEFAULT_SETTINGS)
        s["searxng_url"] = "  http://127.0.0.1:8888/  "
        coerce_settings(s)
        assert s["searxng_url"] == "http://127.0.0.1:8888", (
            "the configured instance is normalised, and an EMPTY value disables "
            "the attempt rather than falling back to a default")
        s["searxng_url"] = ""
        coerce_settings(s)
        assert s["searxng_url"] == ""


class TestLive:
    """One live touch, self-skipping: the fixtures above can all be right while
    the real endpoints have changed shape — which is exactly how the
    double-quote-only parser survived, and how DuckDuckGo's bot challenge was
    found being reported to the user as "no results".

    Stack Exchange is the backend used here because it is an API rather than a
    scrape, so it answers the same way every time. DuckDuckGo is checked too and
    is ALLOWED to come back empty — after a burst of queries it serves a bot
    challenge — because what is being pinned is that the router says which of
    the two happened instead of reporting an empty web either way.
    """

    def test_the_router_answers_from_the_real_web(self, H):
        H._web.cache_clear()
        try:
            results, notes = H._web.search("python list comprehension",
                                           source="stackexchange", limit=2)
        except Exception:
            pytest.skip("offline")
        if not results:
            pytest.skip(f"offline, or today's quota is spent ({notes})")
        assert results[0].title and results[0].url, results
        assert H._web._QUOTA.get("stackexchange"), (
            "a live API call reports its quota, which is what doctor shows")

    def test_duckduckgo_answers_or_says_that_it_refused(self, H):
        # WHICH of those happened cannot be forced from here: DuckDuckGo may
        # serve results, a bot challenge, or a rate-limit status, and which one
        # arrives depends on nothing this test controls. So what is pinned is the
        # property that has to hold in ALL of them — an empty answer names the
        # backend it asked and gives a reason in the vocabulary the module
        # already uses. The challenge DETECTION itself is pinned
        # deterministically by `test_a_bot_challenge_is_a_failure_not_an_empty_
        # web`; a live test cannot make a challenge happen, and a test that
        # accepted only the two reasons it had seen is how an HTTP 429 turns
        # into a red suite that is nobody's regression.
        H._web.cache_clear()
        try:
            results, notes = H._web.search("python list comprehension",
                                           source="ddg", limit=2)
        except Exception:
            pytest.skip("offline")
        if results:
            assert results[0].title and results[0].url, results
            return
        assert notes and notes[0].startswith("nothing found"), (
            f"an empty answer has to say that nothing was FOUND: {notes!r}")
        assert "ddg (DuckDuckGo): " in notes[0], (
            f"an empty answer has to name the backend it asked: {notes[0]!r}")
        reason = notes[0].split("ddg (DuckDuckGo): ", 1)[1].strip()
        assert reason == "no results" or reason.startswith("failed ("), (
            f"the reason has to be the vocabulary the rest of the module uses, "
            f"not a raw exception: {reason!r}")
