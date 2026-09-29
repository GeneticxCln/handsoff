"""World-events warnings: fetcher, severity, dedup, briefing, tick, tool.

H._http_get is mocked everywhere (no live network); the seen store is
redirected to tmp. Style follows test_desktop.py briefing stubs.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from conftest import HERE as ROOT, core_module

# Resolved on first use, inside the sandbox (a direct `from core import tools`
# would bake the developer's real user dirs in at collection time).
_core_tools = core_module("tools")

HERE = ROOT   # the repo root


@pytest.fixture(autouse=True)
def _web_access_on(H, monkeypatch):
    """The world feature fetches by design, and the shipped default for the
    web_access permission is OFF (correctly — the direct-call gate tests below
    pin that). Every test in this file that is not about the gate grants the
    permission explicitly, the way a user who enabled world warnings has."""
    perms = dict(H.SETTINGS.get("permissions") or {})
    perms["web_access"] = True
    monkeypatch.setitem(H.SETTINGS, "permissions", perms)


def _ddg_page(items):
    """Fake lite-DDG HTML: [(title, snippet)] with HTML junk to strip."""
    rows = []
    for t, s in items:
        rows.append(f'<a class="result-link" href="http://x">{t}</a>'
                    f'<td class="result-snippet">{s}</td>')
    return "\n".join(rows)


NEWS = [("Major earthquake strikes coast", "rescue teams respond"),
        ("Local bakery wins award", "sourdough celebrated"),
        ("Markets steady today", "indexes barely move"),
        ("New park opens downtown", "families gather"),
        ("City council meets Tuesday", "agenda posted"),
        ("Extra headline past cap", "should never surface"),
        ("Another past cap", "also hidden"),
        ("Yet another", "hidden too")]


def _http_ok(url, timeout=10.0):
    if "lite.duckduckgo" in url:
        return _ddg_page(NEWS).encode()
    if "geocoding-api" in url:
        return json.dumps({"results": [{"latitude": 1.0, "longitude": 2.0,
                                         "name": "X", "country": "Y"}]}).encode()
    if "api.open-meteo" in url:
        return json.dumps({"current": {"weather_code": 0, "wind_speed_10m": 5.0,
                                       "time": "2026-09-09T10:00"}}).encode()
    raise AssertionError(f"unexpected url {url}")


@pytest.fixture()
def net(H, monkeypatch):
    monkeypatch.setattr(H, "_http_get", _http_ok)
    return _http_ok


@pytest.fixture()
def seenfile(H, tmp_path, monkeypatch):
    f = tmp_path / "world-events-seen.json"
    monkeypatch.setattr(H, "WORLD_EVENTS_FILE", f)
    return f


class TestFetcher:
    def test_cap(self, H, net):
        events, degraded = H._world_events("news", 2)
        assert len(events) == 2 and degraded is False

    def test_overall_cap_five(self, H, net):
        events, _ = H._world_events("all", 99)
        assert len(events) <= 5

    def test_degraded_on_failure(self, H, monkeypatch):
        def boom(url, timeout=10.0):
            raise ConnectionError("offline")
        monkeypatch.setattr(H, "_http_get", boom)
        assert H._world_events("news", 5) == ([], True)

    def test_html_stripped(self, H, monkeypatch):
        page = _ddg_page([("<b>Quake</b> rocks <i>coast</i>", "a &amp; b")])
        monkeypatch.setattr(H, "_http_get", lambda url, timeout=10.0: page.encode())
        events, _ = H._world_events("news", 5)
        assert events and "<" not in events[0]["title"]
        assert events[0]["title"] == "Quake rocks coast"

    def test_partial_results_still_degraded(self, H, monkeypatch):
        def flaky(url, timeout=10.0):
            if "geocoding-api" in url:
                raise ConnectionError("geo down")
            if "lite.duckduckgo" in url:
                # distinct item per query (dedup would collapse identical ones)
                items = NEWS[1:2] if "Berlin" in url else NEWS[:1]
                return _ddg_page(items).encode()  # headroom left for weather
            return _http_ok(url, timeout)
        monkeypatch.setattr(H, "_http_get", flaky)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        events, degraded = H._world_events("all", 5)
        assert len(events) == 2 and degraded is True


class TestSeverity:
    @pytest.mark.parametrize("title", [
        "Major earthquake strikes coast",
        "Tsunami warning issued",
        "Category 4 hurricane landfall",
        "Missile strike reported",
        "Severe thunderstorm watch",
        "Tornado warning for county",
        "TERROR attack downtown",
    ])
    def test_urgent(self, H, title):
        assert H._world_is_urgent(title) is True

    @pytest.mark.parametrize("title", [
        "Local bakery wins award",
        "Markets steady today",
        "New park opens downtown",
    ])
    def test_filler(self, H, title):
        assert H._world_is_urgent(title) is False


class TestDedup:
    def test_mark_then_seen(self, H, seenfile):
        assert H._world_seen(H._world_norm_key("Quake hits")) is False
        H._world_mark_seen(["Quake hits"])
        assert H._world_seen(H._world_norm_key("Quake hits")) is True

    def test_expired_keys_forgotten(self, H, seenfile):
        seenfile.write_text(json.dumps(
            {"old news": time.time() - 40 * 3600}), encoding="utf-8")
        assert H._world_seen("old news") is False

    def test_store_capped(self, H, seenfile):
        H._world_mark_seen([f"headline number {i}" for i in range(80)])
        assert len(H._load_world_seen()) <= H.WORLD_EVENTS_MAX

    def test_tool_never_marks(self, H, net, seenfile, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setitem(H.SETTINGS, "home_place", "")
        out, err = belt.execute("world_events", {"count": 2})
        assert not err and "earthquake" in out
        assert not seenfile.exists()


def _briefing_stub(H, tools):
    class Stub:
        _briefing_done_date = ""
        _BRIEFING_SKIP_PREFIXES = H.Assistant._BRIEFING_SKIP_PREFIXES
        _maybe_briefing_prefix = H.Assistant._maybe_briefing_prefix
    s = Stub()
    s._tools = tools
    return s


class _WxTools:
    @staticmethod
    def execute(name, args):
        if name == "get_weather":
            return _core_tools.ToolResult("Sunny, 21 degrees.")
        return _core_tools.ToolResult("ERROR: x", "error")


class TestBriefing:
    def test_world_section_and_marks_spoken(self, H, net, seenfile, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        a = _briefing_stub(H, _WxTools())
        prefix = a._maybe_briefing_prefix("good morning")
        assert "World:" in prefix
        assert "earthquake" in prefix
        assert H._world_seen(H._world_norm_key(
            "Major earthquake strikes coast")) is True
        assert a._maybe_briefing_prefix("hello again") == ""  # once daily

    def test_skip_prefixes_skip_world_too(self, H, net, seenfile, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        a = _briefing_stub(H, _WxTools())
        assert a._maybe_briefing_prefix("open firefox") == ""

    def test_offline_news_still_briefs_weather(self, H, seenfile, monkeypatch):
        def offline(url, timeout=10.0):
            raise ConnectionError("offline")
        monkeypatch.setattr(H, "_http_get", offline)
        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "Berlin")
        a = _briefing_stub(H, _WxTools())
        prefix = a._maybe_briefing_prefix("good morning")
        assert "Sunny" in prefix and "World:" not in prefix

    def test_no_home_place_news_only(self, H, net, seenfile, monkeypatch):
        called = []

        class Tools:
            @staticmethod
            def execute(name, args):
                called.append(name)
                return _core_tools.ToolResult("Sunny.")

        monkeypatch.setitem(H.SETTINGS, "briefing", True)
        monkeypatch.setitem(H.SETTINGS, "home_place", "")
        a = _briefing_stub(H, Tools())
        prefix = a._maybe_briefing_prefix("good morning")
        assert "get_weather" not in called
        assert "World:" in prefix


def _ticker(H, monkeypatch, state="idle"):
    monkeypatch.setitem(H.SETTINGS, "world_warnings", True)
    monkeypatch.setitem(H.SETTINGS, "world_cooldown_min", 60.0)
    monkeypatch.setitem(H.SETTINGS, "home_place", "")
    a = H.Assistant.__new__(H.Assistant)
    a._world_last_announce = 0.0
    a._world_last_poll = 0.0            # never polled: the first tick may fetch
    a._state = state
    said, popped = [], []
    a._announce_now = said.append
    monkeypatch.setattr(H, "notify", popped.append)
    return a, said, popped


class TestBootFloorAnnounce:
    """time.monotonic() is uptime-based: a 0.0 'never announced' sentinel used
    to look like an announcement made just before boot, silently suppressing
    urgent warnings for a whole cooldown window after every reboot (found live
    when the host had been up 10 minutes). The announce checks must treat
    0.0 as never — regardless of host uptime."""

    def test_never_sentinel_warns_after_fresh_boot(self, H, net, seenfile, monkeypatch):
        a, said, popped = _ticker(H, monkeypatch)
        a._world_last_announce = 0.0        # never announced
        # simulate a freshly booted host: monotonic() inside its first window
        monkeypatch.setattr(H, "_MONOTONIC_BOOT_FLOOR",
                            time.monotonic() - 1.0)
        a._world_tick()
        assert len(said) == 1 and len(popped) == 1   # urgent warning survives

    def test_announced_before_boot_is_still_cooled_down(self, H, net, seenfile,
                                                        monkeypatch):
        """A sentinel of 0.0 is 'never'; a REAL pre-boot announcement cannot
        exist (monotonic starts at 0), so no legitimate case may cool down a
        fresh process for uptime-based reasons."""
        a, said, popped = _ticker(H, monkeypatch)
        a._world_last_announce = 0.0
        monkeypatch.setattr(H, "_MONOTONIC_BOOT_FLOOR", time.monotonic() - 1.0)
        assert H._announce_ok(0.0, 3600.0) is True
        assert H._announce_ok(time.monotonic(), 3600.0) is False   # just announced


class TestProactive:
    def test_flag_off_no_fetch(self, H, net, seenfile, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "world_warnings", False)
        a = H.Assistant.__new__(H.Assistant)
        a._world_last_announce = 0.0
        a._world_last_poll = 0.0
        a._state = "idle"
        a._announce_now = lambda t: (_ for _ in ()).throw(AssertionError())
        a._world_tick()  # must return before any fetch/announce

    def test_a_junk_value_that_means_off_neither_fetches_nor_announces(
            self, H, net, seenfile, monkeypatch):
        """`bool("false")` is True, so a junk `world_warnings` fetched and spoke
        severe-world-event warnings for a value that turned them off."""
        for raw in ("false", "no", "off", "nonsense"):
            a, said, popped = _ticker(H, monkeypatch)
            monkeypatch.setitem(H.SETTINGS, "world_warnings", raw)
            a._world_tick()
            assert said == [] and popped == [], (raw, said, popped)

    def test_cooldown_suppresses_refetch(self, H, net, seenfile, monkeypatch):
        a, said, popped = _ticker(H, monkeypatch)
        a._world_last_announce = time.monotonic()
        calls = []
        orig = H._http_get
        monkeypatch.setattr(H, "_http_get",
                            lambda *a_, **k: calls.append(1) or orig(*a_, **k))
        a._world_tick()
        assert calls == [] and said == [] and popped == []

    def test_urgent_announces_and_marks(self, H, net, seenfile, monkeypatch):
        a, said, popped = _ticker(H, monkeypatch)
        a._world_tick()
        assert len(said) == 1 and len(popped) == 1
        assert "earthquake" in said[0]
        assert H._world_seen(H._world_norm_key(
            "Major earthquake strikes coast")) is True
        # repeat tick: seen store suppresses the second announcement (the
        # poll cadence is reset too — the point here is the seen store)
        a._world_last_announce = 0.0
        a._world_last_poll = 0.0
        a._world_tick()
        assert len(said) == 1 and len(popped) == 1

    def test_speaking_suppresses_spoken_copy(self, H, net, seenfile, monkeypatch):
        a, said, popped = _ticker(H, monkeypatch, state="speaking")
        a._world_tick()
        assert said == [] and len(popped) == 1  # popup always, no TTS


class TestTool:
    def test_refused_without_web_access(self, H):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        belt._perm["web_access"] = False
        out, err = belt.execute("world_events", {"count": 2})
        assert err and out.startswith("REFUSED") and "web_access" in out

    def test_count_clamped(self, H, net, seenfile, monkeypatch):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        monkeypatch.setitem(H.SETTINGS, "home_place", "")
        out, _ = belt.execute("world_events", {"count": 99})
        assert len([ln for ln in out.splitlines()
                    if ln.startswith("- ")]) == 5
        out, _ = belt.execute("world_events", {"count": 0})
        assert len([ln for ln in out.splitlines()
                    if ln.startswith("- ")]) == 1


class TestThePollCadenceAndTheGate:
    """Two 2026-09-28 audit fixes: the poll has its own cadence (the announce
    cooldown cools the ANNOUNCEMENT, and keying the fetch on it alone sent
    the fixed queries out on every ten-second health tick), and the direct
    _world_events call — briefing and tick, not the tool — rides the same
    web_access permission the belt checks."""

    def test_the_poll_does_not_repeat_inside_its_window(
            self, H, net, seenfile, monkeypatch):
        a, said, popped = _ticker(H, monkeypatch)
        calls = []
        orig = H._http_get
        monkeypatch.setattr(
            H, "_http_get",
            lambda *a_, **k: calls.append(1) or orig(*a_, **k))
        a._world_tick()          # first tick: fetches
        assert calls, "the first poll must fetch"
        for _ in range(5):
            a._world_tick()      # still inside WORLD_POLL_S: quiet
        assert len(calls) == 1, calls

    def test_a_poll_makes_no_request_when_web_access_is_off(
            self, H, net, seenfile, monkeypatch):
        monkeypatch.setitem(H.SETTINGS, "world_warnings", True)
        monkeypatch.setitem(
            H.SETTINGS, "permissions", {"web_access": False})
        calls = []
        monkeypatch.setattr(
            H, "_http_get", lambda *a_, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError()))
        a, said, popped = _ticker(H, monkeypatch)
        monkeypatch.setitem(H.SETTINGS, "world_warnings", True)
        monkeypatch.setitem(H.SETTINGS, "permissions", {"web_access": False})
        a._world_tick()
        assert calls == [] and said == [] and popped == []

    def test_the_briefing_path_respects_the_same_gate(self, H, net, seenfile, monkeypatch):
        monkeypatch.setitem(
            H.SETTINGS, "permissions", {"web_access": False})
        events, degraded = H._world_events("all", 5)
        assert (events, degraded) == ([], False), \
            "permission off is quiet-and-healthy, not a degraded backend"
