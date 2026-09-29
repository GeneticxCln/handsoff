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
import pathlib
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

    def test_a_large_file_is_read_bounded(self, H, tmp_path, monkeypatch):
        """The read is bounded at the OPEN, not merely truncated after.

        Truncation-only is observationally identical for a regular file — the
        post-read clip produces the same string — so this test spies on the
        size argument the read makes: with the bound removed, the file is
        read WHOLE into memory before any clip. A 5x file here stands in for
        the 3.6 GB `/dev/zero` read the bound exists to prevent (the special-
        file refusal catches devices; this catches the regular-file case).
        """
        belt = _belt(H)
        big = tmp_path / "big.txt"
        big.write_text("x" * (belt.MAX_READ * 5), encoding="utf-8")
        asked = []
        real_open = pathlib.Path.open

        def spying_open(self, mode="r", *a, **k):
            fh = real_open(self, mode, *a, **k)

            class Spy:
                def __getattr__(self, name):
                    return getattr(fh, name)

                def __enter__(self):
                    fh.__enter__()
                    return self

                def __exit__(self, *exc):
                    return fh.__exit__(*exc)

                def read(self, n=-1):
                    asked.append(n)
                    return fh.read(n)

            return Spy()

        monkeypatch.setattr(pathlib.Path, "open", spying_open)
        out, err = belt.execute("read_file", {"path": str(big)})
        assert not err, out
        assert "[truncated" in out, out[:200]
        # The read asked for a BOUND, and the bytes it got back stayed under
        # it: `read()` with no size (n == -1) would have taken all 800k.
        assert asked and asked[0] not in (-1, None), asked
        assert asked[0] < len("x" * (belt.MAX_READ * 5)), asked


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
    def test_a_recycled_pid_is_refused(self, H, monkeypatch):
        """The offer named hsoff-victim; the pid is this test's own process.
        SIGTERMing whatever recycles the number inside the 60 s window was a
        misfire waiting for a coincidence.

        The offer is PINNED onto every path the tool reaches it by
        (`conftest.pin_offer`). Arming `H._kill_offer` directly worked only while
        one app module existed: once a driver test had imported a second one, the
        tool's `_dep()` resolved to THAT module's own offer, this one was never
        consulted, and the tool answered "the kill offer expired" — an ordering
        dependence that the CI ordering probe (seeded by the commit SHA) hits on
        some commits and not others, and that the previous commit's message
        recorded as "the one remaining suite failure"."""
        from conftest import pin_offer
        pin_offer(H, monkeypatch, "kill").arm(
            H.ToolBelt.KILL_CONFIRM_S, pid=os.getpid(), name="hsoff-victim")
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


class TestRunCommandBuffersABoundedAmountOfOutput:
    """`run_command` buffered a command's whole output before cutting the reply
    to 2000 characters. `cat` is on the whitelist and `/dev/zero` is not a
    credential path, so `cat /dev/zero` buffered without end: measured on this
    tree, a 4 s timeout held 2.5 GiB and took 11 s to return — in the process
    that also holds the speech models, which makes the production timeout (15 s)
    a way to be killed by the OOM killer. The same call decoded strictly
    (`text=True`), so `cat` of any binary file raised UnicodeDecodeError out of
    the tool and the model was told "that is a bug in the tool".
    """

    def test_a_command_that_never_stops_writing_costs_the_cap_not_the_machine(
            self, H, monkeypatch):
        import tracemalloc
        belt = _belt(H)
        monkeypatch.setattr(H.ToolBelt, "OUTPUT_CAP", 4096)
        monkeypatch.setattr(H.ToolBelt, "TIMEOUT", 2)
        started = H.time.monotonic()
        tracemalloc.start()
        try:
            out, err = belt.execute("run_command", {"command": "cat /dev/zero"})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        elapsed = H.time.monotonic() - started
        assert "timed out" in out, out
        # Two seconds of `cat /dev/zero` buffered whole is hundreds of MiB (the
        # old call: ~580 MiB); the capped reader holds the cap plus one chunk.
        assert peak < 16 * 1024 * 1024, (
            f"{peak / 2**20:.0f} MiB was held while a command wrote for 2 s")
        assert elapsed < 8.0, (
            f"returned after {elapsed:.1f}s for a 2 s timeout: the output was "
            f"being buffered, and draining it after the kill took the difference")

    def test_the_buffer_is_capped_and_keeps_the_head(self, H, monkeypatch,
                                                     tmp_path):
        big = tmp_path / "big.txt"
        big.write_text("HEAD-" + "x" * 500_000, encoding="utf-8")
        monkeypatch.setattr(H.ToolBelt, "OUTPUT_CAP", 1000)
        proc, stdout, stderr = H.ToolBelt._run_capped(["cat", str(big)], 10)
        assert proc.returncode == 0
        assert len(stdout) == 1000 and stdout.startswith("HEAD-"), len(stdout)
        assert stderr == ""

    def test_stdout_and_stderr_stay_separate_streams(self, H, tmp_path):
        proc, stdout, stderr = H.ToolBelt._run_capped(
            ["ls", str(tmp_path / "does-not-exist")], 10)
        assert proc.returncode != 0
        assert stdout == "" and "does-not-exist" in stderr, (stdout, stderr)

    def test_a_binary_file_is_text_with_replacements_not_an_exception(
            self, H, tmp_path):
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"\x7fELF\xff\xfe\x00\xf0binary\x80" * 40)
        out, err = _belt(H).execute("run_command", {"command": f"cat {blob}"})
        assert not err and out.startswith("exit code 0"), out
        assert "bug in the tool" not in out

    def test_a_host_replacement_of_run_is_still_the_seam(self, H, monkeypatch):
        """Every test and every embedder replaces `subprocess.run`; a fake that
        hands back its own stdout/stderr must be honoured over the pipes."""
        class Done:
            returncode = 3
            stdout = "fake out\n"
            stderr = "fake err\n"

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            return Done()

        monkeypatch.setattr(H.subprocess, "run", fake_run)
        out, err = _belt(H).execute("run_command", {"command": "echo hi"})
        assert out == "exit code 3\nstdout:\nfake out\nstderr:\nfake err", out
        assert seen["argv"] == ["echo", "hi"]
        assert seen["kwargs"]["timeout"] == H.ToolBelt.TIMEOUT
        assert seen["kwargs"]["stdin"] is H.subprocess.DEVNULL, (
            "a command must not inherit the bubble's own stdin")

    def test_it_leaves_no_reader_thread_and_no_descriptor_behind(self, H):
        before_threads = {t.name for t in threading.enumerate()}
        before_fds = len(os.listdir("/proc/self/fd"))
        for _ in range(20):
            H.ToolBelt._run_capped(["echo", "x"], 10)
        assert {t.name for t in threading.enumerate()} - before_threads == set()
        assert len(os.listdir("/proc/self/fd")) <= before_fds + 1, (
            "each call must close both pipes")

    def test_a_timeout_still_raises_and_still_releases_the_pipes(self, H):
        before_fds = len(os.listdir("/proc/self/fd"))
        with pytest.raises(H.subprocess.TimeoutExpired):
            H.ToolBelt._run_capped(["cat", "/dev/zero"], 0.5)
        assert len(os.listdir("/proc/self/fd")) <= before_fds + 1


class TestTheClipboardIsNotAlwaysText:
    """`paste_text` decoded `wl-paste` strictly, so an image on the clipboard —
    the first thing most people copy that is not words — raised
    UnicodeDecodeError out of the tool, and the model said "that is a bug in the
    tool" about a perfectly ordinary clipboard."""

    def _fake_paste(self, H, monkeypatch, payload):
        class Done:
            returncode = 0
            stdout = payload
            stderr = b""
        monkeypatch.setattr(H.subprocess, "run", lambda argv, **kw: Done())

    def test_an_image_is_named_as_data_not_a_crash(self, H, monkeypatch):
        self._fake_paste(H, monkeypatch, b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x00IHDR")
        out, err = _belt(H).execute("paste_text", {})
        assert not err, out
        assert "not text" in out and "bug in the tool" not in out, out

    def test_bytes_that_are_valid_utf8_but_not_text_are_named_too(
            self, H, monkeypatch):
        self._fake_paste(H, monkeypatch, b"\x00\x01\x02 header \x00" + b"a" * 100)
        out, err = _belt(H).execute("paste_text", {})
        assert not err and "not text" in out, out

    def test_ordinary_text_reads_exactly_as_before(self, H, monkeypatch):
        self._fake_paste(H, monkeypatch, "héllo wörld — ünïcode".encode("utf-8"))
        out, err = _belt(H).execute("paste_text", {})
        assert not err and out == (
            "clipboard holds 21 chars: 'héllo wörld — ünïcode'"), out

    def test_a_long_clipboard_is_summarised_and_an_empty_one_is_named(
            self, H, monkeypatch):
        self._fake_paste(H, monkeypatch, b"x" * 300)
        out, _ = _belt(H).execute("paste_text", {})
        assert out.startswith("clipboard holds 300 chars: ") and "(+180 more chars)" in out
        self._fake_paste(H, monkeypatch, b"")
        assert _belt(H).execute("paste_text", {})[0] == "clipboard is empty"

    def test_a_replacement_that_answers_in_text_still_works(self, H, monkeypatch):
        """Existing fakes (and any embedder) hand back `str`."""
        self._fake_paste(H, monkeypatch, "from a str fake")
        out, err = _belt(H).execute("paste_text", {})
        assert not err and out == "clipboard holds 15 chars: 'from a str fake'", out


class TestAFileSomeoneElseWroteIsSearchedNotDecodedStrictly:
    """The niri config, the systemd unit: files other programs (and the user's
    editor) write, which this code only SEARCHES. Strict UTF-8 raised
    UnicodeDecodeError — a ValueError, not the OSError those call sites caught —
    out of the chord check and out of the doctor, the one tool that has to work
    when something is already wrong."""

    def test_the_super_chord_check_reads_a_latin1_niri_config(self, H, tmp_path,
                                                              monkeypatch):
        cfg = tmp_path / ".config" / "niri"
        cfg.mkdir(parents=True)
        (cfg / "config.kdl").write_bytes(
            b'// caf\xe9 keybinds \xff\n'
            b'binds {\n    Mod+V { spawn "handsoff.py" "--ptt" "toggle"; }\n}\n')
        monkeypatch.setattr(H, "HOME", tmp_path)
        belt = _belt(H)
        assert belt._super_binding_known("Mod+V") is True

    def test_the_hardware_probe_reads_a_unit_with_a_stray_byte(self, tmp_path):
        import hardware
        unit = tmp_path / "handsoff.service"
        unit.write_bytes(b"[Unit]\nDescription=Jos\xe9's bubble\n[Service]\nRestart=always\n")
        out = hardware._systemd({"systemd_unit_file": str(unit)})
        assert out["ok"] is True and out["auto_restart"] is True, out


class TestAMalformedCommandIsARefusalNotABug:
    """Fuzzing the validator (30,000 strings) found exactly one class of input
    that made it RAISE: a NUL byte or a lone surrogate, which reached
    `os.path.expanduser` (`~x\\x00` -> ValueError, `~\\ud800` -> UnicodeEncodeError)
    and came back to the model as "that is a bug in the tool" about what is a
    malformed command."""

    @pytest.mark.parametrize("command", [
        "echo a\x00b", "~nosuchuser\x00", "~root\ud800", "ls \ud800",
        "cat /tmp/\x00", "\x00", "ls \udc80",
    ])
    def test_it_is_refused_by_name(self, H, command):
        out, err = _belt(H).execute("run_command", {"command": command})
        assert err and out.startswith("REFUSED"), (command, out)
        assert "bug in the tool" not in out, out

    def test_ordinary_unicode_is_still_a_command(self, H):
        out, err = _belt(H).execute("run_command", {"command": "echo héllo 😀"})
        assert not err and "héllo 😀" in out, out


class TestTheDecisionLogTrimSurvivesATornLine:
    """`log_decision` trims decisions.jsonl by reading it back. A power cut can
    leave the last line cut mid-character, and that read was strict UTF-8 inside
    a handler that names only OSError: the torn tail raised out of the trim, the
    outer handler swallowed it, and the log was never pruned again — every tool
    call afterwards re-read a file that only grew."""

    def test_the_log_is_still_pruned(self, H, monkeypatch, tmp_path):
        import core.tools as _t
        monkeypatch.setattr(H, "STATE_DIR", tmp_path)
        monkeypatch.setattr(H, "DECISIONS_FILE", tmp_path / "decisions.jsonl")
        f = tmp_path / "decisions.jsonl"
        row = b'{"id": "%d", "tool": "run_command", "target": "' + b"x" * 200 + b'"}\n'
        f.write_bytes(b"".join(row % i for i in range(1500))
                      + b'{"id": "torn", "target": "caf\xc3')
        assert f.stat().st_size > 262144
        _t.log_decision("run_command", "echo hi", "ALLOW")
        lines = f.read_bytes().splitlines()
        assert len(lines) <= _t._DECISIONS_MAX + 1, (
            f"{len(lines)} lines: a torn tail stopped the trim")
        assert b'"decision": "ALLOW"' in lines[-1], "the newest decision was lost"
        f.read_bytes().decode("utf-8")   # what it rewrote is text again


class TestAnOutOfRangeNumberIsAnArgumentProblem:
    """`int(float("inf"))` and `float(10**400)` raise OverflowError, which is an
    ArithmeticError and not the ValueError the binding step catches. It escaped
    to the belt's catch-all, so the model was told the ASSISTANT's own check had
    failed and not to retry — for an argument it could simply have fixed."""

    @pytest.mark.parametrize("tool,arg,extra", [
        ("world_events", "count", {}), ("read_calendar", "days", {}),
        ("read_page", "max_chars", {"url": "https://example.com/"}),
        ("wait", "seconds", {}), ("pomodoro", "work_minutes", {"action": "start"}),
        ("wait_for_window", "timeout", {"name": "x"})])
    @pytest.mark.parametrize("value", [float("inf"), "9" * 5000])
    def test_it_is_refused_as_bad_arguments(self, H, tool, arg, extra, value):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute(tool, {arg: value, **extra})
        assert err, out
        assert "could not be evaluated" not in out, (
            "an argument problem was reported as the assistant's own failure: "
            f"{out}")

    @pytest.mark.parametrize("tool,arg,extra", [
        ("wait", "seconds", {}), ("pomodoro", "work_minutes", {"action": "start"}),
        ("wait_for_window", "timeout", {"name": "x"})])
    def test_a_float_argument_too_large_for_a_float_is_too(self, H, tool, arg, extra):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute(tool, {arg: 10 ** 400, **extra})
        assert err and "could not be evaluated" not in out, out


class TestReadFileRefusesAPathNoFilesystemCanHold:
    """A NUL byte or a lone surrogate raised ValueError/UnicodeEncodeError out of
    resolve() and exists(), and the model was told "that is a bug in the tool"."""

    @pytest.mark.parametrize("path", ["a\x00b", "\x00", "~\x00", "\ud800", "x/\ud800/y"])
    def test_it_is_a_refusal_not_a_bug_report(self, H, path):
        belt = H.ToolBelt(on_restart_pending=lambda: None)
        out, err = belt.execute("read_file", {"path": path})
        assert "cannot exist" in out, out
        assert "bug in the tool" not in out and "failed with" not in out, out
