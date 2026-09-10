"""Notification-swarm coalescing: popups dedup/rate-limit, reader cooldown.

TDD pins: identical bursts → 1 popup with ×N; distinct bursts → burst cap
+ 1 summary (never silently dropped); per-app reader cooldown; handsoff's
own app name never announced. Subprocess/dbus mocked, no hardware.
"""
from __future__ import annotations

import threading
import time
import types

import pytest

from conftest import HERE as ROOT

HERE = ROOT   # the repo root


@pytest.fixture()
def calls(H, monkeypatch):
    """Capture notify-send invocations; reset swarm state first."""
    H._notify_reset()
    got: list = []

    def fake_run(argv, **kw):
        got.append(list(argv))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    return got


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class TestNotifyCoalesce:
    def test_isolated_notify_sends_one_popup(self, H, calls):
        H.notify("hello world")
        assert _wait_for(lambda: len(calls) >= 1)
        time.sleep(0.6)  # no trailing duplicate may appear
        assert calls == [["notify-send", "-a", "handsoff", "handsoff",
                          "hello world"]]

    def test_identical_burst_coalesces_with_count(self, H, calls):
        for _ in range(5):
            H.notify("build finished")
        assert _wait_for(lambda: len(calls) >= 1)
        time.sleep(0.6)
        assert len(calls) == 1, calls
        assert calls[0][:4] == ["notify-send", "-a", "handsoff", "handsoff"]
        assert "build finished" in calls[0][4] and "×5" in calls[0][4]

    def test_distinct_burst_capped_with_summary(self, H, calls):
        for i in range(12):
            H.notify(f"event number {i}")
        assert _wait_for(lambda: len(calls) >= H._NOTIFY_BURST + 1)
        time.sleep(0.6)
        assert len(calls) == H._NOTIFY_BURST + 1, calls
        summary = calls[-1][4]
        assert "more" in summary and "notification" in summary

    def test_notify_never_raises(self, H, monkeypatch):
        H._notify_reset()

        def boom(argv, **kw):
            raise OSError("no notify-send here")

        monkeypatch.setattr(H.subprocess, "run", boom)
        H.notify("whatever")  # must not raise, sync or in flush thread
        time.sleep(0.8)


def _notify_lines(app, summary="hi", body="there"):
    """One realistic production dbus-monitor message: a `method call ...
    member=Notify` header (NOT a signal), the four payload strings, then the
    actions + hints trailers (action labels, sender-pid/desktop-entry/urgency
    hint strings, byte/uint32 variants) exactly as the session bus prints
    them. The trailers carry plenty of quoted strings that must NEVER fire
    their own announcements."""
    return [
        ("method call time=1725631234.123456 sender=:1.45 -> destination=:1.46 "
         "serial=42 path=/org/freedesktop/Notifications; "
         "interface=org.freedesktop.Notifications; member=Notify"),
        f'   string "{app}"',
        "   uint32 0",
        '   string ""',
        f'   string "{summary}"',
        f'   string "{body}"',
        "   array [",
        '      string "default"',
        '      string "Open"',
        "   ]",
        "   array [",
        "      dict entry(",
        '         string "desktop-entry"',
        '         variant             string "firefox"',
        "      )",
        "      dict entry(",
        '         string "sender-pid"',
        "         variant             uint32 1234",
        "      )",
        "      dict entry(",
        '         string "urgency"',
        "         variant             byte 1",
        "      )",
        "   ]",
        "   int32 5000",
    ]


class TestReaderCooldown:
    def _run_loop(self, H, monkeypatch, apps):
        H._notify_reset()
        monkeypatch.setitem(H.SETTINGS, "notification_mute_apps", [])
        said: list = []
        a = H.Assistant.__new__(H.Assistant)
        a._announce_now = said.append
        lines = []
        for app in apps:
            lines.extend(_notify_lines(app))
        proc = types.SimpleNamespace(stdout=iter(lines), poll=lambda: 0)
        a._notification_loop(proc, threading.Event())
        return said

    def test_same_app_repeats_suppressed(self, H, monkeypatch):
        said = self._run_loop(H, monkeypatch, ["Slack", "Slack", "Slack"])
        assert len(said) == 1, said

    def test_distinct_apps_each_announced_once(self, H, monkeypatch):
        said = self._run_loop(H, monkeypatch, ["Slack", "Mail", "Slack"])
        # Slack, Mail announced; second Slack is within cooldown
        assert len(said) == 2, said
        assert "Slack" in said[0] and "Mail" in said[1]

    def test_own_app_name_never_announced(self, H, monkeypatch):
        said = self._run_loop(H, monkeypatch, ["handsoff", "Handsoff"])
        assert said == [], said

    def test_mute_list_still_honored(self, H, monkeypatch):
        H._notify_reset()
        monkeypatch.setitem(H.SETTINGS, "notification_mute_apps", ["noisy"])
        said: list = []
        a = H.Assistant.__new__(H.Assistant)
        a._announce_now = said.append
        proc = types.SimpleNamespace(
            stdout=iter(_notify_lines("NoisyApp")), poll=lambda: 0)
        a._notification_loop(proc, threading.Event())
        assert said == [], said

    def test_one_message_yields_one_announcement_despite_trailers(self, H, monkeypatch):
        """The actions/hints trailer strings (default, sender-pid,
        desktop-entry, urgency) must not fire their own announcements."""
        said = self._run_loop(H, monkeypatch, ["Firefox"])
        assert len(said) == 1, said
        assert "Firefox" in said[0] and "hi" in said[0]

    def test_urgency_hint_neither_mutes_nor_multiplies(self, H, monkeypatch):
        """Urgency travels as a byte variant in hints: an unrelated app is
        still announced exactly once."""
        said = self._run_loop(H, monkeypatch, ["Firefox"])
        assert len(said) == 1, said

    def test_handsoff_word_in_summary_or_body_is_self_muted(self, H, monkeypatch):
        """New contract: SELF-mute when app==handsoff or 'handsoff' in
        summary/body (our own popups echo the name); user mute list matches
        app (+summary word-ish) and never body alone."""
        H._notify_reset()
        monkeypatch.setitem(H.SETTINGS, "notification_mute_apps", [])
        said: list = []
        a = H.Assistant.__new__(H.Assistant)
        a._announce_now = said.append
        proc = types.SimpleNamespace(
            stdout=iter(_notify_lines("Something", summary="timer",
                                      body="handsoff snoozed it")),
            poll=lambda: 0)
        a._notification_loop(proc, threading.Event())
        assert said == [], said
        # summary mention also self-mutes
        said.clear()
        proc2 = types.SimpleNamespace(
            stdout=iter(_notify_lines("Something", summary="handsoff timer",
                                      body="done")),
            poll=lambda: 0)
        a._notification_loop(proc2, threading.Event())
        assert said == [], said

    def test_user_mute_matches_app_and_summary_word_not_body(self, H, monkeypatch):
        """User list: app substring ('noisy'→'NoisyApp') and summary whole-word
        mute; a body-only mention must still announce."""
        H._notify_reset()
        monkeypatch.setitem(H.SETTINGS, "notification_mute_apps", ["noisy"])
        said: list = []
        a = H.Assistant.__new__(H.Assistant)
        a._announce_now = said.append
        # body-only 'noisy' still announces (mute never looks at body)
        proc = types.SimpleNamespace(
            stdout=iter(_notify_lines("Firefox", summary="hi",
                                      body="noisy background chatter")),
            poll=lambda: 0)
        a._notification_loop(proc, threading.Event())
        assert len(said) == 1, said
        # summary whole-word mutes
        H._notify_reset()
        said.clear()
        proc2 = types.SimpleNamespace(
            stdout=iter(_notify_lines("Firefox", summary="noisy build",
                                      body="done")),
            poll=lambda: 0)
        a._notification_loop(proc2, threading.Event())
        assert said == [], said

    def test_stray_lines_and_back_to_back_messages(self, H, monkeypatch):
        H._notify_reset()
        monkeypatch.setitem(H.SETTINGS, "notification_mute_apps", [])
        said: list = []
        a = H.Assistant.__new__(H.Assistant)
        a._announce_now = said.append
        lines = ["stray quoted string before any Notify"]
        lines += _notify_lines("Slack", summary="one", body="first")
        lines += _notify_lines("Mail", summary="two", body="second")
        proc = types.SimpleNamespace(stdout=iter(lines), poll=lambda: 0)
        a._notification_loop(proc, threading.Event())
        assert len(said) == 2, said
        assert "Slack" in said[0] and "Mail" in said[1]
