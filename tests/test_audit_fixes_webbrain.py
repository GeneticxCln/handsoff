"""The 2026-09-28 audit fixes for `core/web.py` and `core/voice.py`, one test
per finding. The fixes span three modules whose own suites own the behaviour
AROUND these edges, so they are pinned together here:

* F1  (HIGH)  the SSRF ranges Python 3.14's flags stopped covering;
* F2  (HIGH)  the world-events permission gate the proactive fetches missed;
* F4  (MEDIUM) an https read refusing a redirect downgrade to http;
* F6  (LOW)   one wall-clock budget for a read's whole redirect walk;
* F8  (LOW)   a lid on the wedged resolver's leaked threads;
* F9  (LOW)   the pre-roll ring that grew unboundedly on a degenerate window;
* F11 (LOW)   DuckDuckGo's honest-empty marker, matched where the page puts it.

Finding 5 (the removed unchecked one-shot fallback) is pinned by the two
updated tests in `tests/test_web.py`; the brain fixes are in
`tests/test_brain.py`. The network never happens here — every fetch arrives
through the seam `serve()` installs.
"""
from __future__ import annotations

import socket
import time

import numpy as np
import pytest

from conftest import core_module
from test_web import PUBLIC_IP, Fetch, _public_dns, serve


@pytest.fixture()
def web(monkeypatch):
    """The same reset `tests/test_web.py`'s fixture does, plus the world gate."""
    mod = core_module("web")
    monkeypatch.setattr(mod, "_SEEN", {})
    monkeypatch.setattr(mod, "_QUOTA", {})
    monkeypatch.setattr(mod, "_READER_SEEN", {})
    monkeypatch.setattr(mod, "_CACHE", {})
    monkeypatch.setattr(mod, "_HTTP_GET", None)
    monkeypatch.setattr(mod, "_HTTP_IS_FN", True)
    monkeypatch.setattr(mod, "_HTTP_HOP", None)
    monkeypatch.setattr(mod, "_HTTP_HOP_IS_FN", True)
    monkeypatch.setattr(mod, "_SEARXNG_URL", "")
    monkeypatch.setattr(mod, "_HOSTED_READER", True)
    monkeypatch.setattr(mod, "_WORLD_GATE", None)
    monkeypatch.setattr(mod, "_LOG", None)
    monkeypatch.setattr(mod.socket, "getaddrinfo", _public_dns)
    return mod


BODY = (b"<html><body><h1>Fixture</h1>" + b"<p>word " * 200
        + b"</p></body></html>")


class TestTheRangesPython314StoppedCovering:
    """Finding 1 (HIGH): `_is_private_ip` leaned on the stdlib's flags, and
    Python 3.14 moved both 100.64.0.0/10 (CGNAT / overlay-VPN space) and
    192.0.0.0/24 (IETF protocol assignments) out of every flag the check
    consulted — measured there: `.is_private` False for 100.64.0.1 and
    `.is_global` True for 192.0.0.9. A model-supplied URL on those ranges
    would have read a VPN peer's admin page into the transcript."""

    @pytest.mark.parametrize("ip", [
        "100.64.0.0", "100.64.0.1", "100.115.92.19", "100.127.255.255",
        "192.0.0.0", "192.0.0.9", "192.0.0.255",
    ])
    def test_the_ranges_are_private(self, web, ip):
        assert web._is_private_ip(ip) is True, ip

    @pytest.mark.parametrize("ip", [
        "100.63.255.255", "100.128.0.0", "192.0.1.9", "8.8.8.8", "1.1.1.1",
    ])
    def test_the_boundaries_and_the_public_internet_stay_public(self, web, ip):
        assert web._is_private_ip(ip) is False, ip

    def test_the_loopback_boundary_refuses_by_its_own_flag(self, web):
        # 127.255.255.255: the LOOPBACK rule, not the explicit ranges — pinned
        # so a rewrite of the ranges cannot quietly lose the flag checks.
        assert web._is_private_ip("127.255.255.255") is True

    @pytest.mark.parametrize("url,ip", [
        ("http://100.64.0.1/admin", "100.64.0.1"),
        ("http://100.115.92.19/", "100.115.92.19"),
        ("http://192.0.0.9/", "192.0.0.9"),
    ])
    def test_read_page_refuses_the_ranges_by_name(self, web, url, ip):
        f = serve(web, Fetch())
        text, _via, problem = web.read_page(url)
        assert text == "" and ip in problem, (text, problem)
        assert "private network" in problem, problem
        assert not f.urls, "a refused address must not be fetched at all"

    def test_just_outside_the_ranges_is_still_fetched(self, web):
        serve(web, Fetch(**{"100.128.0.0": BODY}))
        text, via, problem = web.read_page("http://100.128.0.0/x")
        assert not problem and via == web.VIA_LOCAL, (text[:80], problem)


class TestTheWorldEventsGate:
    """Finding 2 (HIGH): the host's proactive world-events fetch (the world
    warning tick, the morning briefing) called its `_world_events` directly,
    so the `web_access` permission the tools check never saw it. `core.web`
    now holds the gate (`configure(world_gate=...)`, read by
    `world_allowed()`), and the host consults it before any network I/O —
    defaulting to allow, so direct callers that never wired it keep behaving."""

    def test_no_gate_wired_means_allowed(self, web):
        assert web.world_allowed() is True

    def test_a_wired_gate_answers_in_bools(self, web, monkeypatch):
        monkeypatch.setattr(web, "_WORLD_GATE", lambda: False)
        assert web.world_allowed() is False
        monkeypatch.setattr(web, "_WORLD_GATE", lambda: True)
        assert web.world_allowed() is True

    def test_the_gate_is_read_per_use(self, web, monkeypatch):
        """A resolver, like every seam here: a live settings save takes
        effect on the NEXT poll, not the next restart."""
        state = {"on": True}
        monkeypatch.setattr(web, "_WORLD_GATE", lambda: state["on"])
        assert web.world_allowed() is True
        state["on"] = False
        assert web.world_allowed() is False

    def test_an_uncertain_gate_reads_as_off(self, web, monkeypatch):
        """A permission check that blew up must not open the door — and a
        hand-edited settings value carrying the string "false" is not consent
        either (the same `is True` reading the hosted-reader switch uses)."""
        def broken():
            raise RuntimeError("settings are being rewritten")

        monkeypatch.setattr(web, "_WORLD_GATE", broken)
        assert web.world_allowed() is False
        monkeypatch.setattr(web, "_WORLD_GATE", lambda: "false")
        assert web.world_allowed() is False


class TestTheRedirectDowngrade:
    """Finding 4 (MEDIUM): every hop was re-checked for WHERE it pointed but
    never for HOW it got there, so an https read could follow a redirect down
    to http and hand the page to whatever sits on the wire. A read that
    STARTS as http stays http — that behaviour is kept and pinned."""

    @staticmethod
    def _hop_fetch(hops):
        def hop(url, timeout=10.0, connect_to=None):
            return hops.get(url, (b"", ""))
        return hop

    def test_an_https_read_refuses_a_downgrade_to_http(self, web):
        web.configure(http_get_hop=self._hop_fetch({
            "https://example.com/a": (b"", "http://cdn.example.net/b"),
        }))
        text, _via, problem = web.read_page("https://example.com/a")
        assert text == "" and "unencrypted" in problem, (text, problem)
        assert "downgraded" in problem, problem

    def test_an_http_read_may_stay_http(self, web):
        web.configure(http_get_hop=self._hop_fetch({
            "http://example.com/a": (b"", "http://cdn.example.net/b"),
            "http://cdn.example.net/b": (BODY, ""),
        }))
        text, via, problem = web.read_page("http://example.com/a")
        assert not problem and via == web.VIA_LOCAL, (text[:80], problem)

    def test_an_https_read_may_stay_https(self, web):
        web.configure(http_get_hop=self._hop_fetch({
            "https://example.com/a": (b"", "/b"),
            "https://example.com/b": (BODY, ""),
        }))
        text, via, problem = web.read_page("https://example.com/a")
        assert not problem and via == web.VIA_LOCAL, (text[:80], problem)


class TestTheReadDeadline:
    """Finding 6 (LOW): six hops of eight seconds each plus a resolver apiece
    is a minute the user waits through per page, times `read_top` pages. The
    walk now runs on one wall clock and stops with a named refusal instead of
    starting another hop; the per-socket timeouts are untouched."""

    def test_a_walk_past_its_budget_stops_with_a_named_refusal(
            self, web, monkeypatch):
        monkeypatch.setattr(web, "READ_DEADLINE_S", 0.3)
        hops = []

        def hop(url, timeout=10.0, connect_to=None):
            hops.append(url)
            time.sleep(0.12)
            return b"", "/next"          # keeps redirecting forever

        web.configure(http_get_hop=hop)
        text, _via, problem = web.read_page("https://example.com/a")
        assert text == "" and "abandoned" in problem, (text, problem)
        assert len(hops) < web.MAX_REDIRECTS + 1, (
            f"the walk ran to the redirect cap anyway: {len(hops)} hops")


class TestTheResolverLid:
    """Finding 8 (LOW): a wedged resolver leaves its worker thread behind per
    attempt, so a burst of dead name servers leaked a daemon thread each. The
    in-flight count now has a lid; past it the name fails immediately, with
    the resolution-failure wording."""

    def test_at_capacity_the_name_fails_without_a_thread(self, web,
                                                         monkeypatch):
        asked = []
        monkeypatch.setattr(web, "_DNS_THREADS_MAX", 0)
        monkeypatch.setattr(web.socket, "getaddrinfo",
                            lambda *a, **k: asked.append(a))
        text, _via, problem = web.read_page("https://example.com/x")
        assert text == "" and "could not be resolved" in problem, (text, problem)
        assert "busy" in problem, problem
        assert asked == [], "getaddrinfo ran anyway"

    def test_a_timed_out_resolve_frees_its_slot(self, web, monkeypatch):
        """The lid counts ATTEMPTS, not successes: the thread left behind past
        its join must still give its slot back, or the lid itself becomes the
        wedge it was meant to bound."""
        monkeypatch.setattr(web, "_DNS_TIMEOUT_S", 0.1)

        def stuck(host, port, *a, **k):
            time.sleep(0.5)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (PUBLIC_IP, port))]

        monkeypatch.setattr(web.socket, "getaddrinfo", stuck)
        text, _via, problem = web.read_page("https://slow.example/x")
        assert text == "" and "could not be resolved within" in problem, problem
        assert web._DNS_THREADS == 0, web._DNS_THREADS


class TestThePrerollRing:
    """Finding 9 (LOW): `_max_pre` computed 0 when the host's frame is longer
    than the whole pre-roll window, and `del self._buf[:-0]` deletes NOTHING —
    the ring grew one frame per feed for as long as the process listened. The
    window is clamped to at least one frame, so the eviction always evicts."""

    class _Model:
        def predict(self, chunk):
            return {"w": 0.0}

    def test_a_degenerate_window_still_evicts(self):
        voice = core_module("voice")
        spotter = voice.WakeSpotter(sample_rate=16_000, frame=48_000,
                                    model=self._Model())
        assert spotter._max_pre == 1, spotter._max_pre
        frame = np.zeros(48_000, dtype=np.int16)
        for _ in range(6):
            spotter.feed(frame)
        assert len(spotter._buf) <= 1, (
            f"the pre-roll ring grew to {len(spotter._buf)} frames")

    def test_the_normal_window_is_untouched(self):
        voice = core_module("voice")
        spotter = voice.WakeSpotter(sample_rate=16_000, frame=1_024,
                                    model=self._Model())
        assert spotter._max_pre == int(2.0 * 16_000 // 1_024), spotter._max_pre
        frame = np.zeros(1_024, dtype=np.int16)
        for _ in range(40):
            spotter.feed(frame)
        assert len(spotter._buf) == spotter._max_pre, len(spotter._buf)


class TestTheDuckDuckGoEmptyMarker:
    """Finding 11 (LOW): "no results" matched as a SUBSTRING of the whole
    page, so a reshaped page whose snippet text mentioned the phrase read as
    an honest empty and hid the reshape. The marker is now matched where the
    real empty page puts it: in a heading."""

    def test_a_snippet_mentioning_no_results_is_not_an_empty_web(self, web):
        page = (b"<html><body><td class='result-snippet'>why does search say "
                b"no results?</td></body></html>")
        serve(web, Fetch(**{"lite.duckduckgo.com": page}))
        with pytest.raises(RuntimeError, match="unrecognised reply"):
            web.ddg_search("anything")

    def test_a_result_beside_a_snippet_mentioning_the_phrase_is_read(
            self, web):
        page = (b'<a class="result-link" href="https://example.com/x">T</a>'
                b"<td class='result-snippet'>no results were seen before</td>")
        serve(web, Fetch(**{"lite.duckduckgo.com": page}))
        (hit,) = web.ddg_search("anything")
        assert hit.url == "https://example.com/x"

    def test_the_honest_empty_is_still_honest_in_any_heading(self, web):
        page = (b"<html><body><h3>No results.</h3>"
                + b"<!-- filler -->" * 40 + b"</body></html>")
        serve(web, Fetch(**{"lite.duckduckgo.com": page}))
        assert web.ddg_search("anything") == []
