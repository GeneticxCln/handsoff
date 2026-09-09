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
    return [
        'signal sender=:1.2 member=Notify string=""',
        f'   string "{app}"',
        '   string ""',
        f'   string "{summary}"',
        f'   string "{body}"',
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
