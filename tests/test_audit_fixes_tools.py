"""Pins for the 2026-09-28 audit fixes in core/tools.py.

Each test names the finding it closes. The fixes: the cargo CONFIRM floor is
keyed on the argv the exec will run (not the raw string), the credential
denylist covers the classic CLI/DB secret files, Flatpak browser profiles and
the app's own settings.json, the terminal markers cover st/guake/yakuake/
BlackBox without substring false positives, a CONFIRM offer describes the
ARMED payload, read_file refuses special files and reads bounded, validator
refusals land in the decision log, dry_run covers state-changing tools,
confirm_kill re-checks the pid, the file watcher refuses a symlink swap,
open_app keeps the interpreter/blocklist company, an unreadable niri config
fails the Super-chord check closed, and the screenshot lands 0600.
"""
import json
import os
import re
import threading
from pathlib import Path

import pytest


def _belt(H):
    return H.ToolBelt(on_restart_pending=lambda: None)


class TestTheCargoConfirmFloor:
    def test_leading_whitespace_and_tab_still_confirm(self, H):
        """' cargo build' and 'cargo\\tbuild' both reach cargo — the exec
        strips and shlex-parses first — so the CONFIRM floor has to judge the
        same argv. Keyed on the raw first token, neither variant confirmed,
        and a build.rs is arbitrary code running without the user."""
        tb = _belt(H)
        for command in (" cargo build", "cargo\tbuild", "cargo build"):
            out, err = tb.execute("run_command", {"command": command})
            assert err and "CONFIRM REQUIRED" in out, (command, out, err)
            # Nothing ran: the offer is armed, the tool was not dispatched.
            assert tb._pending_confirm.state()[0] is not None
            tb._pending_confirm.clear()

    def test_a_plain_command_still_runs_without_confirm(self, H):
        tb = _belt(H)
        tb._pending_confirm.clear()
        out, err = tb.execute("run_command", {"command": "pactl info"})
        assert "CONFIRM REQUIRED" not in out, out


class TestTheDenylistAdditions:
    @pytest.mark.parametrize("path", [
        "~/.pgpass", "~/.my.cnf", "~/.wgetrc", "~/.s3cfg",
        "~/.config/rclone/rclone.conf",
        "~/.var/app/com.google.Chrome/config/google-chrome/Local State",
        "~/.var/app/org.mozilla.firefox/.mozilla/salt",
    ])
    def test_read_file_refuses_the_new_secret_paths(self, H, path):
        belt = _belt(H)
        out, err = belt.execute("read_file", {"path": path})
        assert err and out.startswith("REFUSED"), (path, out)
        assert ("holds credentials or key material" in out
                or "holds browser profile credentials" in out
                or "credential or shell-history" in out), out

    def test_cat_through_run_command_refuses_them_too(self, H, monkeypatch, tmp_path):
        """The whitelist's own secret-argument check judges every argv token,
        so `cat` of a newly denied file is refused the same way the .ssh rule
        already refused `cat ~/.ssh/id_rsa`."""
        monkeypatch.setattr(H, "DECISIONS_FILE", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        belt = _belt(H)
        out, err = belt.execute("run_command", {"command": "cat ~/.pgpass"})
        assert "REFUSED" in out, out

    def test_the_apps_own_settings_is_a_secret(self, H):
        """settings.json holds the calendar's bearer-token URLs; the calendar
        tool already refuses to echo the URL, so handing the whole file to
        read_file was the same secret leaving by another door."""
        belt = _belt(H)
        out, err = belt.execute(
            "read_file", {"path": str(Path(H.CONFIG_DIR) / "settings.json")})
        assert err and out.startswith("REFUSED"), out
        assert "bearer-token" in out, out
class TestTheTerminalMarkers:
    def test_the_missing_terminals_are_markers_now(self):
        from core.tools import ToolBelt
        for app_id in ("st", "guake", "yakuake", "com.raggesilver.BlackBox"):
            assert ToolBelt._terminal_marker({"app_id": app_id, "title": ""}), app_id

    def test_st_is_exact_so_its_letters_do_not_refuse_everything(self):
        from core.tools import ToolBelt
        for app_id in ("weston", "steam", "gnome-settings", "webstorm"):
            assert ToolBelt._terminal_marker({"app_id": app_id, "title": ""}) is None, app_id


class TestTheConfirmOfferNamesTheArmedPayload:
    def test_a_second_call_describes_what_yes_will_run(self, H, monkeypatch, tmp_path):
        """The offer is armed on the FIRST call and a second call does not
        re-arm — but the message used to be built from the SECOND call's args,
        so the user could approve `pactl info` while `yes` executed a volume
        change. The offer now speaks for itself."""
        monkeypatch.setattr(H, "DECISIONS_FILE", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        tb = _belt(H)
        tb._policy = H._core_tools.DecisionPolicy(
            {"command_policy": {"run_command": "CONFIRM"}})
        tb._set_user_turn(1)
        armed_command = "pactl set-sink-volume @DEFAULT_SINK@ -10%"
        first, _ = tb.execute("run_command", {"command": armed_command})
        assert "CONFIRM REQUIRED" in first and armed_command in first
        second, _ = tb.execute("run_command", {"command": "pactl info"})
        assert "CONFIRM REQUIRED" in second
        assert armed_command in second, second
        assert "pactl info" not in second, second


class TestReadFileBoundaries:
    def test_a_special_file_is_refused_not_hung(self, H):
        """/dev/zero and a FIFO have no end; read_bytes() blocked the turn
        worker forever (verified with a FIFO, 2026-09-28)."""
        belt = _belt(H)
        out, err = belt.execute("read_file", {"path": "/dev/null"})
        assert err and "not a regular file" in out, out

    def test_a_large_file_is_read_bounded(self, H, tmp_path):
        belt = _belt(H)
        big = tmp_path / "big.txt"
        big.write_text("x" * (belt.MAX_READ * 5), encoding="utf-8")
        out, err = belt.execute("read_file", {"path": str(big)})
        assert not err, out
        assert "[truncated" in out, out[:200]
        assert len(out) < belt.MAX_READ + 500


class TestTheDecisionLogTellsTheTruth:
    def test_a_validator_refusal_is_logged_as_a_refusal(self, H, monkeypatch, tmp_path):
        """`cat ~/.ssh/id_rsa` used to sit in decisions.jsonl as ALLOW /
        dispatched — the tool body refused it, and nothing ever said so."""
        monkeypatch.setattr(H, "DECISIONS_FILE", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        belt = _belt(H)
        belt.execute("run_command", {"command": "cat ~/.ssh/id_rsa"})
        entries = [json.loads(line)
                   for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
        verdicts = [e["decision"] for e in entries]
        assert "DENY" in verdicts, entries
        deny = next(e for e in entries if e["decision"] == "DENY")
        assert "refused" in deny["result"], deny

    def test_an_unknown_tool_is_logged(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "DECISIONS_FILE", tmp_path / "decisions.jsonl")
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        belt = _belt(H)
        belt.execute("definitely_not_a_tool", {})
        entries = [json.loads(line)
                   for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
        assert any(e["result"] == "refused: unknown tool name" for e in entries), entries


class TestDryRunCoversStateChangers:
    @pytest.fixture(autouse=True)
    def _dry_run_on(self, H, monkeypatch):
        monkeypatch.setattr(H._core_tools, "setting_flag",
                            lambda key, default=False: True)
        return H

    def test_set_reminder_is_reported_not_stored(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        monkeypatch.setattr(H, "REMINDERS_FILE", tmp_path / "reminders.json")
        belt = _belt(H)
        out, err = belt.execute(
            "set_reminder", {"wake_name": "tea", "when_due": "in 5 minutes"})
        assert err and "DRY-RUN" in out, out
        assert not (tmp_path / "reminders.json").exists(), \
            "a dry-run rehearsal must not persist an alarm"

    def test_watch_file_is_reported_not_started(self, H, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        belt = _belt(H)
        out, err = belt.execute(
            "watch_file", {"path": "/tmp/whatever.log", "pattern": "x"})
        assert err and "DRY-RUN" in out, out
        assert not list(belt._file_watchers.keys()), \
            "a dry-run rehearsal must not start a real watcher"


class TestConfirmKill:
    def test_a_recycled_pid_is_refused(self, H):
        """The offer named hsoff-victim; the pid is this test's own process.
        SIGTERMing whatever recycles the number inside the 60 s window was a
        misfire waiting for a coincidence."""
        H._kill_offer.arm(H.ToolBelt.KILL_CONFIRM_S,
                          pid=os.getpid(), name="hsoff-victim")
        belt = _belt(H)
        out, err = belt.execute("confirm_kill", {"answer": "yes"})
        assert err and "recycled" in out, out


class TestOpenApp:
    def test_interpreters_and_blocklist_are_refused(self, H):
        belt = _belt(H)
        for app in ("perl", "env", "node", "make"):
            out, err = belt.execute("open_app", {"app": app})
            assert "REFUSED" in out, (app, out)


class TestTheSuperChordCheck:
    def test_an_unreadable_config_fails_closed(self, H, monkeypatch, tmp_path):
        """Unreadable config.kdl used to read as 'legacy allow', skipping the
        terminal gate — the one direction a guard about what reaches a
        terminal must never open."""
        monkeypatch.setattr(H, "HOME", tmp_path)
        assert H.ToolBelt._super_binding_known("mod+e") is False


class TestTheFileWatchLoop:
    def test_a_symlink_swap_stops_the_watcher(self, H, tmp_path):
        """The loop re-opened the raw path every poll; a local racer could
        swap the watched file for a link into a secret store and have
        matching lines announced. O_NOFOLLOW + a per-poll denylist re-check
        end the watch instead of following the swap."""
        target = tmp_path / "watched.log"
        target.write_text("hello\n", encoding="utf-8")
        link = tmp_path / "swap.log"
        link.symlink_to(target)
        emits = []
        stop = threading.Event()

        def grow():
            target.write_text("hello\nsecret line\n", encoding="utf-8")

        threading.Timer(1.3, grow).start()
        threading.Timer(4.0, stop.set).start()
        H.ToolBelt._file_watch_loop(link, re.compile("secret"), stop, emits.append)
        assert emits, "the loop must say why it stopped"
        assert "lost" in emits[-1] or "stopped" in emits[-1], emits


class TestTheScreenshotPermissions:
    def test_the_screenshot_lands_0600(self, H, monkeypatch, tmp_path):
        """grim writes the path itself, skipping atomic_private_write — the
        last image of the user's screen used to land 0644 in a 0755 dir."""
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(H.ToolBelt, "SCREENSHOT_FILE", shot)
        belt = _belt(H)

        def fake_run(argv, **kwargs):
            shot.write_bytes(b"\x89PNG\r\n\x1a\n")

            class P:
                returncode = 0
                stderr = ""
            return P()

        # Patch the seam's TARGET (the app module's subprocess), not the
        # _InjectedProxy: setattr on the proxy leaves an instance attribute
        # that shadows __getattr__ for every later test in the process.
        monkeypatch.setattr(H.subprocess, "run", fake_run)
        assert belt._take_screenshot() == ""
        # 0600: no group or other bits — owner-write is part of 0600 itself.
        assert (shot.stat().st_mode & 0o077) == 0, oct(shot.stat().st_mode)
