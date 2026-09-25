"""desk_retest — the automated half of ACCEPTANCE §10, in ONE command.

§10 is the desk retest for three features: the scratch sweep with its
reversible archive, the doctor's `state:` line, and the hygiene trend. Most of
it is mechanical — stage a probe, restart the unit, read what the journal and
the line said — and that is what this driver does, so the only thing left for a
person is the part no script can stand in for: **A1–A4, a real voice at the real
microphone** (`_wake_anywhere`, the VAD retry, and the wake gate generally).

What it touches on this desk, so nobody is surprised by a quiet assistant:

* it RESTARTS `handsoff.service` once per check that needs a fresh start,
  because every rule under test runs at startup — PACED to the unit's own
  `StartLimitBurst`/`StartLimitIntervalSec`, counted from the JOURNAL (every
  start systemd actually performed: this run's, an earlier run's,
  `Restart=always` respawns alike) and kept one start under the burst, since a
  retest that restarts in a burst is a crash loop to systemd and would leave
  the bubble down for the rest of the limit window (measured live 2026-09-25);
* it plants probe files directly in the real state dir and removes them again —
  the probes are the only things it deletes, and they carry the `tmpdeskcheck`
  prefix (the stranded real TTS dir B1 archives is NOT one: it survives the
  run, so the person can play it back per §10's B2, and the archive's own
  7-day TTL reclaims it in the end)
* for B1 it sends SIGKILL to the running bubble on purpose (systemd's
  `Restart=always` brings it straight back; that is the scenario) — but only
  once the reply's `tts.wav` holds bytes, because a kill during the voice's
  load strands an EMPTY dir with nothing for B2 to play (found live
  2026-09-25); and it grades the wav's presence, as §10's B1 always asked.

Two backends, one set of checks:

* `--simulate` runs the whole thing against a THROWAWAY state dir with the
  checkout's own code and no systemd at all (the child calls the same functions
  `main()` calls, in the same order). Nothing on the desk is disturbed — this is
  the mode the suite drives, and how you rehearse the driver itself.
* the default backend is the real thing: the deployed copy, the real unit, the
  real journal. B1 and A5's arming half only exist there and say so when run
  under `--simulate`.

The output is an evidence block meant to be pasted under §10's items, with the
OWED line naming what a human still has to do. Exit status is 0 when every
automated check passed, 1 otherwise (a desk tool, not a gate).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
UNIT = "handsoff.service"
STATE_REL = Path("handsoff")
QUARANTINE = "scratch-quarantine"
#: Probes are named so a reader can tell them apart from a real leak, and so
#: `--only`'s cleanup can find every one of them without guessing.
PROBE_PREFIX = "tmpdeskcheck"
PROBE_FILE = "deskcheck.tmp"
GRACE_BACKDATE_S = 1200          # comfortably past the runtime's 600 s grace
OWED_HUMAN = ("A1", "A2", "A3", "A4")
#: The three legitimate readings of the doctor's `wake:` line for a custom name
#: (the exact strings live in the host; these are the shapes it must produce).
WAKE_LINE_SHAPES = (
    "audio spotter untried",
    "audio spotter is off",
    "FAILED to load",
    "has NO model for this name",
    "audio spotter is loaded but reports no model names",
    "no wake word required",
)
#: The child that performs one startup pass. It calls what `main()` calls, in
#: `main()`'s order — `_prepare_runtime()` (private dirs, the sweep, the trend
#: row), `setup_logging()`, then the sweep summary that only works once the
#: journal exists — and then prints the doctor's line and the hygiene JSON from
#: the SAME process, so a check can never compare two different processes'
#: readings and call it agreement.
#: The render-only child C5 uses: no `_prepare_runtime()`, so a synthetic week
#: can be judged without a start (and without touching a real history).
WEEK_PASS = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import handsoff as H
print("STATE_LINE", H._state_hygiene_line())
print("TREND_JSON", json.dumps(H._state_hygiene()["trend"]))
"""

START_PASS = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import handsoff as H
ok = H._prepare_runtime()
H.setup_logging()
H._log_swept_scratch()
print("PREPARE_OK", bool(ok))
print("STATE_LINE", H._state_hygiene_line())
print("HYGIENE_JSON", json.dumps(H._state_hygiene()))
"""


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


class Result:
    """One check's outcome, with the evidence a person can paste."""

    def __init__(self, item: str, title: str) -> None:
        self.item = item
        self.title = title
        self.ok: bool | None = None
        self.evidence: list[str] = []
        self.why = ""

    def passed(self, *evidence: str) -> "Result":
        self.ok = True
        self.evidence.extend(evidence)
        return self

    def failed(self, why: str, *evidence: str) -> "Result":
        self.ok = False
        self.why = why
        self.evidence.extend(evidence)
        return self

    def skipped(self, why: str) -> "Result":
        self.ok = None
        self.why = why
        return self


class Desk:
    """Where a check runs: the real unit+state dir, or a throwaway root.

    Everything a check needs is a method here, so the two backends differ in
    ONE place and the checks stay honest about which environment they were run
    against (`self.mode`).
    """

    mode = "systemd"

    def __init__(self, root: Path, bin_dir: Path, python: str) -> None:
        self.root = root
        self.bin_dir = bin_dir
        self.python = python
        self.state = root / STATE_REL
        self.created: list[Path] = []
        self.restarts = 0
        self.notes: list[str] = []

    # ---------------------------------------------------------------- probing
    def plant_dir(self, name: str, payload: bytes | None = b"RIFF-tts") -> Path:
        path = self.state / name
        path.mkdir(parents=True, exist_ok=True)
        self.created.append(path)
        if payload is not None:
            (path / "tts.wav").write_bytes(payload)
        return path

    def plant_file(self, name: str, payload: bytes = b"{\"partial\":true}") -> Path:
        path = self.state / name
        path.write_bytes(payload)
        self.created.append(path)
        return path

    def backdate(self, path: Path, seconds: int = GRACE_BACKDATE_S) -> None:
        """Age an entry past the grace window, which is what makes it the
        sweep's business at all: a fresh leftover belongs to the living."""
        stamp = time.time() - seconds
        if path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                os.utime(child, (stamp, stamp))
        os.utime(path, (stamp, stamp))

    def archive_dir(self) -> Path:
        return self.state / QUARANTINE

    def dated_archive(self, day: str | None = None) -> Path:
        day = day or datetime.date.today().isoformat()
        return self.archive_dir() / day

    def archived(self, name: str) -> Path | None:
        """The archived copy of `name`, wherever in the dated folders it is."""
        if not self.archive_dir().is_dir():
            return None
        for folder in sorted(self.archive_dir().iterdir()):
            for candidate in (folder / name, *folder.glob(f"{name}~*")):
                if candidate.exists():
                    return candidate
        return None

    def scratch_names(self) -> list[str]:
        names = []
        try:
            entries = list(self.state.iterdir())
        except OSError:
            return names
        for entry in entries:
            if entry.is_symlink():
                continue
            name = entry.name
            if (entry.is_dir() and name.startswith("tmp")) \
                    or (entry.is_file() and name.endswith(".tmp")):
                names.append(name)
        return sorted(names)

    # ------------------------------------------------------------------ start
    def start_pass(self) -> None:
        raise NotImplementedError

    def service_answers(self) -> bool:
        """Whether the BUBBLE itself answered — never a client's local report."""
        raise NotImplementedError

    def state_line(self) -> str:
        raise NotImplementedError

    def hygiene_json(self) -> dict:
        raise NotImplementedError

    def log_text(self, since: float | None = None) -> str:
        raise NotImplementedError

    def log_since_last_start(self) -> str:
        """What the journal said SINCE this start began.

        Scoped on purpose: a warning or a `swept` line from an earlier check
        would let this one pass on evidence it did not produce.
        """
        raise NotImplementedError

    def wake_line(self) -> str:
        raise NotImplementedError

    # ---------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        """Remove what this run PLANTED, and nothing else.

        Bounded by `PROBE_PREFIX` on purpose: B1 registers the REAL stranded
        TTS dir it just reclaimed, and the run's first live session destroyed
        that archived reply at exit — the exact artifact §10's B2 exists to
        `cp`/`file`/`aplay` by hand, found live 2026-09-25 (the earlier
        session's `tmptq9yudu_` was gone before this session started, and
        this session's `tmpklue3jp7` vanished between B1's PASS and the
        `cp`). The 7-day TTL is the reply's designed way out, not the run's.
        """
        for path in reversed(self.created):
            if not path.name.startswith(PROBE_PREFIX):
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        # ...and the archived copies of those probes, which live under the
        # date folder the run itself chose: a retest that left its own probes
        # in the archive would be exactly the leak it is checking for. Real
        # scratch (B1's reply) keeps its name `tmpXXXXXXXX`, so the prefix
        # gate cannot touch it.
        for path in list(self.created):
            if not path.name.startswith(PROBE_PREFIX):
                continue
            archived = self.archived(path.name)
            if archived is not None:
                try:
                    if archived.is_dir():
                        shutil.rmtree(archived, ignore_errors=True)
                    else:
                        archived.unlink(missing_ok=True)
                except OSError:
                    pass
        self.created.clear()


class SystemdDesk(Desk):
    """The real thing: the service systemd owns and the start it just did."""

    mode = "systemd"
    #: `doctor` is NOT usable to ask "is the bubble up?": with the unit down the
    #: client prints `(bubble not running — local report)` and its OWN `state:`
    #: line, and still exits 0. `status` needs the control socket, so its exit
    #: code is the honest answer (found live 2026-09-25: C2 graded a client's
    #: reading while the unit sat in `start-limit-hit`).
    READY_VERB = "status"
    #: How long a start may take before the driver calls it gone: this bubble
    #: loads whisper and warms TTS before it answers.
    START_TIMEOUT_S = 180.0

    def __init__(self, root: Path, bin_dir: Path, python: str) -> None:
        super().__init__(root, bin_dir, python)
        self._mark = time.time()
        self._starts: list[float] = []
        self._client_only = False
        self._budget_note = False

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True)

    def is_active(self) -> bool:
        return self._systemctl("is-active", UNIT).stdout.strip() == "active"

    def main_pid(self) -> str:
        return self._systemctl("show", "-p", "MainPID", "--value",
                               UNIT).stdout.strip()

    def speak(self, text: str) -> subprocess.Popen:
        """Start a spoken reply and do not wait for it.

        B1 needs the bubble MID-synthesis: the scratch dir is created before
        the TTS call, so killing while a `say` is in flight is what strands it.
        """
        return subprocess.Popen(
            [self.python, str(self.bin_dir / "handsoff.py"), "--ptt", "say",
             text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def kill9(self) -> str:
        """The scenario B1 exists for: the process dies with no chance to
        clean up its own scratch, so the NEXT start has to."""
        pid = self.main_pid()
        if pid and pid != "0":
            subprocess.run(["kill", "-9", pid], check=False)
            # systemd's `Restart=always` respawn IS a start, so it spends the
            # same budget the pacer below is keeping track of
            self._starts.append(time.time())
        return pid

    @staticmethod
    def _span_s(value: str) -> float | None:
        """A systemd time span as seconds: `2min`, `90`, `1min 30s`.

        `systemctl show` prints durations as SPANS, not bare numbers — the
        first parser read `2min` with `isdigit()`, silently fell back to the
        default, and only matched this unit by luck.
        """
        total, seen = 0.0, False
        for token in value.split():
            unit = token.lstrip("0123456789")
            digits = token[:len(token) - len(unit)]
            if not digits:
                continue
            factor = {"": 1.0, "s": 1.0, "sec": 1.0, "ms": 1e-3, "us": 1e-6,
                      "min": 60.0, "h": 3600.0, "d": 86400.0}.get(unit)
            if factor is None:
                return None
            total += float(digits) * factor
            seen = True
        return total if seen else None

    def _start_limits(self) -> tuple[int, float]:
        """The unit's own start policy, read from systemd rather than assumed."""
        burst, interval = 5, 120.0
        out = self._systemctl("show", "-p", "StartLimitBurst",
                              "-p", "StartLimitIntervalSec", UNIT).stdout
        for line in out.splitlines():
            key, _, value = line.partition("=")
            value = value.strip()
            if not value:
                continue
            if key == "StartLimitBurst" and value.isdigit():
                burst = max(1, int(value))
            elif key == "StartLimitIntervalSec":
                seconds = self._span_s(value)
                if seconds and seconds > 0:
                    interval = seconds
        return burst, interval

    def _journal_start_stamps(self, interval: float) -> list[float]:
        """Epoch stamps of every start the journal shows inside the window.

        The journal is the ground truth the pacer was missing: an in-process
        list cannot know about starts this run did not cause (the previous
        run's tail, `Restart=always` respawns), which is exactly how a paced
        retest still managed to trip the limit (found live 2026-09-25).
        """
        try:
            proc = subprocess.run(
                ["journalctl", "--user", "-u", UNIT, "--no-pager",
                 "-o", "short-iso",
                 "--since", f"@{time.time() - interval:.0f}"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return []
        stamps = []
        for line in proc.stdout.splitlines():
            if "Started handsoff" not in line:
                continue
            head = line.split(" ", 1)[0]
            try:
                stamps.append(datetime.datetime.fromisoformat(head).timestamp())
            except ValueError:
                continue
        return stamps

    def _recent_starts(self, interval: float) -> list[float]:
        """Starts inside the window: the journal's, or the process's own if
        the journal cannot be asked (a CI container without one)."""
        now = time.time()
        stamps = self._journal_start_stamps(interval)
        if stamps:
            return [t for t in stamps if now - t < interval]
        return [t for t in self._starts if now - t < interval]

    def _wait_for_budget(self) -> None:
        """Spend systemd's start budget honestly instead of tripping it.

        A desk retest restarts the unit once per check, and a burst of
        restarts is a crash LOOP as far as systemd is concerned: this unit
        sets `StartLimitBurst=5` / `StartLimitIntervalSec=120`, so the sixth
        start was REFUSED, the unit went `failed (start-limit-hit)` and the
        bubble stayed off the air — a retest that broke the desk it came to
        measure (found live 2026-09-25, twice). So the driver reads the
        policy, counts the starts systemd ACTUALLY did, and keeps one start
        of headroom under the burst: the retest must never be the straw. It
        says why it is holding rather than leaving a silent stall.
        """
        burst, interval = self._start_limits()
        while True:
            recent = self._recent_starts(interval)
            if len(recent) < burst - 1:
                return
            hold = interval - (time.time() - min(recent)) + 1.0
            if not self._budget_note:
                self._budget_note = True
                self.notes.append(
                    f"held restarts to stay inside the unit's own start limit "
                    f"({burst} per {interval:.0f}s, counted from the journal) "
                    f"— a retest looks like a crash loop to systemd")
            print(f"  holding {hold:.0f}s — start limit {burst}/{interval:.0f}s",
                  file=sys.stderr, flush=True)
            time.sleep(max(1.0, hold))

    def service_answers(self) -> bool:
        proc = subprocess.run([self.python, str(self.bin_dir / "handsoff.py"),
                               "--ptt", self.READY_VERB],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode == 0

    def _start_limit_hit(self) -> bool:
        return "start-limit-hit" in self._systemctl(
            "show", "-p", "Result", UNIT).stdout

    def _await_ready(self) -> bool:
        end = time.time() + self.START_TIMEOUT_S
        while time.time() < end:
            if self.is_active() and self.service_answers():
                return True
            if self._start_limit_hit():
                return False       # waiting cannot clear it; recover instead
            time.sleep(2.0)
        return False

    def ensure_up(self) -> None:
        """A retest that finds the bubble down first tries to bring it back.

        A killed or crashed predecessor can leave the unit `failed` with no
        socket (the session that died mid-run on 2026-09-25 did exactly that);
        grading a doctor over a dead bubble is the one lie this driver exists
        never to tell, so it revives first and says so.
        """
        if self.is_active():
            return
        self._systemctl("reset-failed", UNIT)
        self._wait_for_budget()
        self._starts.append(time.time())
        self._systemctl("start", UNIT)
        if self._await_ready():
            self.notes.append("the unit was down when the retest began; "
                              "`reset-failed` + `start` brought it back")
        else:
            self.notes.append(
                "the unit was down and DID NOT come back — every check below "
                "will fail or be answered by a client, not the bubble")

    def start_pass(self) -> None:
        """A fresh start, waited for by the START and not by a stopwatch.

        The first cut polled the doctor for the word `swept` — which a CLIENT
        answers for a dead bubble (`sweep not run this process`), so a slow
        start read as a failed sweep. Readiness is the control socket now, and
        the deadline is generous because the bubble loads whisper and warms TTS
        before it answers.
        """
        self._wait_for_budget()
        self._mark = time.time()
        self._starts.append(self._mark)
        self._systemctl("restart", UNIT)
        self.restarts += 1
        if self._await_ready():
            return
        # the documented recovery (`specs/50-ops.md` §6): a unit that hit the
        # limit stays `failed` until reset-failed clears the state AND the
        # limiter's counters — retried once, held to the budget again, because
        # the refused start itself still sits in the window
        for _attempt in range(2):
            self._systemctl("reset-failed", UNIT)
            self._starts.append(time.time())
            self._systemctl("start", UNIT)
            if self._await_ready():
                self.notes.append("the unit had hit its start limit; "
                                  "`reset-failed` + `start` brought it back")
                return
            self._wait_for_budget()
        self.notes.append(
            "the unit did not answer after a start, so the doctor is being "
            "answered by a CLIENT, not the bubble")

    def _doctor(self) -> str:
        proc = subprocess.run(
            [self.python, str(self.bin_dir / "handsoff.py"), "--ptt", "doctor"],
            capture_output=True, text=True, timeout=120)
        return proc.stdout

    def _service_reading(self) -> str:
        """The doctor's text, or "" when a CLIENT answered for a dead bubble.

        A dead bubble still answers `doctor`, from a LOCAL report that carries
        its own `state:`/`wake:` lines — a check grading those would be grading
        a process that never ran the sweep (found live 2026-09-25, when a
        start-limit stall made C2 read a client's line).
        """
        text = self._doctor()
        self._client_only = doctor_is_local(text)
        return "" if self._client_only else text

    def state_line(self) -> str:
        for line in self._service_reading().splitlines():
            if line.startswith("state:"):
                return line
        return ""

    def wake_line(self) -> str:
        for line in self._service_reading().splitlines():
            if line.startswith("wake:"):
                return line
        return ""

    def hygiene_json(self) -> dict:
        return _child_json(self.python, self.bin_dir)

    def log_text(self, since: float | None = None) -> str:
        args = ["journalctl", "--user", "-u", UNIT, "--no-pager", "-o", "cat"]
        if since is not None:
            args += ["--since", f"@{since:.0f}"]
        return subprocess.run(args, capture_output=True, text=True).stdout

    def log_since_last_start(self) -> str:
        return self.log_text(since=self._mark)


class SimDesk(Desk):
    """A throwaway root and the checkout's own code, no systemd involved.

    The startup pass is the child above, pointed at a temp `XDG_STATE_HOME`, so
    the sweep, the archive, the trend row and the line are the REAL code paths;
    only the process boundary and systemd are simulated. Checks that genuinely
    need a process to be killed (B1) or hands-free to be armed (A5's warning)
    report themselves as desk-only instead of faking it.
    """

    mode = "simulate"

    def service_answers(self) -> bool:
        # the child that just ran IS the bubble here, and it printed its own
        # reading — there is no second process to be mistaken for it
        return True

    def __init__(self, root: Path, bin_dir: Path, python: str) -> None:
        super().__init__(root, bin_dir, python)
        self._last: dict = {}
        self._log_base = 0
        self._stderr_all = ""

    def start_pass(self) -> None:
        env = dict(os.environ)
        env["XDG_STATE_HOME"] = str(self.root)
        env["XDG_CONFIG_HOME"] = str(self.root / "cfg")
        self._log_base = len(self.log_text())
        proc = subprocess.run([self.python, "-c", START_PASS, str(self.bin_dir)],
                              capture_output=True, text=True, env=env,
                              timeout=180)
        # stderr is part of the journal, not noise: the sweep runs BEFORE
        # logging exists, so its warnings reach the journal through
        # `logging.lastResort` — which is stderr. Searching only the log file
        # would call a refusal that did happen "never logged".
        self._stderr_all += proc.stderr or ""
        self.restarts += 1
        self._last = {}
        for line in proc.stdout.splitlines():
            head, _, rest = line.partition(" ")
            self._last[head] = rest
        if "STATE_LINE" not in self._last:
            self.notes.append(
                f"the simulated start pass printed no line: "
                f"{proc.stderr.strip()[-300:]}")

    def state_line(self) -> str:
        return self._last.get("STATE_LINE", "")

    def wake_line(self) -> str:
        return ""            # no hands-free, so no spotter was ever armed

    def hygiene_json(self) -> dict:
        try:
            return json.loads(self._last.get("HYGIENE_JSON", "") or "{}")
        except ValueError:
            return {}

    def log_text(self, since: float | None = None) -> str:
        try:
            text = (self.state / "handsoff.log").read_text(encoding="utf-8")
        except OSError:
            text = ""
        return text + "\n" + self._stderr_all

    def log_since_last_start(self) -> str:
        return self.log_text()[self._log_base:]


def _child_json(python: str, bin_dir: Path) -> dict:
    """The structured reading, from a client process against `bin_dir`.

    A fresh client has never run `_prepare_runtime`, so `last_sweep.at` is
    None and nothing is swept by asking — which is the property that makes this
    a safe read, not a side effect.
    """
    snippet = ("import sys, json; sys.path.insert(0, sys.argv[1]); "
               "import handsoff as H; "
               "print(json.dumps(H.doctor_json()['state_hygiene']))")
    proc = subprocess.run([python, "-c", snippet, str(bin_dir)],
                          capture_output=True, text=True, timeout=180)
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


# --------------------------------------------------------------- interpreters
def sweep_entries(log: str) -> list[tuple[int, str]]:
    """Every `swept N stale scratch entr(y|ies) … into DEST` the journal holds,
    oldest first. The phrasing is the host's own (`_log_swept_scratch`), and a
    reader that guessed at it could pass while the line said nothing."""
    found = []
    for line in log.splitlines():
        if "swept " not in line or "stale scratch" not in line:
            continue
        try:
            rest = line.split("swept ", 1)[1]
            count = int(rest.split(" ", 1)[0])
        except (IndexError, ValueError):
            continue
        dest = line.split(" into ", 1)[1].split(" (recoverable", 1)[0] \
            if " into " in line else ""
        found.append((count, dest.strip()))
    return found


def parse_state_line(line: str) -> dict:
    """The `state:` sentence as facts: what is scratched, what the last start
    reclaimed, the size, and the trend clause. Anything unreadable stays None
    so a check fails loudly instead of comparing nothing with nothing."""
    out = {"raw": line, "scratch_left": None, "swept": None,
           "sweep_age": "", "size": "", "entries": None, "trend": "",
           "trend_delta": None}
    if not line.startswith("state:"):
        return out
    pieces = [p.strip() for p in line.split(";")]
    # the FIRST clause rides directly behind `state:`, so it is part 0 — reading
    # from 1 on silently dropped the scratch count and made three checks fail
    # against a line that was telling the truth (caught by --simulate)
    pieces[0] = pieces[0].split("state:", 1)[1].strip()
    for part in pieces:
        if part.startswith("no leaked scratch"):
            out["scratch_left"] = 0
        elif "scratch-shaped entr" in part:
            try:
                out["scratch_left"] = int(part.split()[0])
            except (IndexError, ValueError):
                pass
        elif part.startswith("last start swept"):
            if "swept nothing" in part:
                out["swept"] = 0
            else:
                try:
                    out["swept"] = int(part.split("swept ", 1)[1].split()[0])
                except (IndexError, ValueError):
                    pass
            if "(" in part:
                out["sweep_age"] = part.split("(", 1)[1].rstrip(")")
        elif part.startswith("sweep not run"):
            out["swept"] = None
        # the trend clause is read BEFORE the size clause, because a mature one
        # reads `7-day trend +412.0 MB, +3 entries` — it ENDS in `entries`, so
        # the size branch swallowed it and a truthful line read as having no
        # trend at all (the shape the host's renderer really produces)
        elif "trend" in part:
            out["trend"] = part
            if "trend " in part and "," in part:
                out["trend_delta"] = part.split("trend ", 1)[1]
        elif part.endswith("entries") or part.endswith("entry"):
            pieces = part.split(" in ", 1)
            if len(pieces) == 2:
                out["size"] = pieces[0]
                try:
                    out["entries"] = int(pieces[1].split()[0])
                except (IndexError, ValueError):
                    pass
    return out


def doctor_is_local(doctor_output: str) -> bool:
    """Whether `--ptt doctor` was answered by a CLIENT, not the bubble.

    With the unit down the client still answers, from a local report that holds
    its own `state:`/`wake:` lines (`sweep not run this process`) — so the
    bubble's reported state is unjudgeable, and the honest thing is to say so
    rather than to grade a process that never ran the sweep.
    """
    return "bubble not running" in doctor_output


def wake_line_ok(line: str) -> tuple[bool, str]:
    """A `wake:` line must name the channel that opens, never claim coverage
    for a name no model covers (the whole point of the line)."""
    if not line.startswith("wake:"):
        return False, "no `wake:` line in the doctor's output"
    for shape in WAKE_LINE_SHAPES:
        if shape in line:
            return True, line
    return False, f"a `wake:` line that names none of the known channels: {line}"


# --------------------------------------------------------------------- checks
def check_a5(d: Desk) -> Result:
    r = Result("A5", "the spotter-coverage warning, and a `wake:` line that "
                     "names the truth")
    if d.mode == "systemd" and not d.service_answers():
        return r.failed("the bubble is not answering `--ptt status`, so the "
                        "`wake:` line here would be a CLIENT's reading of a "
                        "bubble that is not up")
    line = d.wake_line()
    if not line and d.mode == "simulate":
        return r.skipped("needs systemd (the deployed unit answers the doctor)")
    ok, why = wake_line_ok(line)
    if not ok:
        return r.failed(why, f"doctor: {line or '(nothing)'}")
    evidence = [f"doctor: {line}"]
    warned = [ln for ln in d.log_since_last_start().splitlines()
              if "wake spotter has no model" in ln]
    if warned:
        evidence.append(f"journal: {warned[-1].strip().split('handsoff: ')[-1]}")
    elif d.mode == "systemd":
        evidence.append("journal: no arming warning — hands-free is off, so "
                        "the spotter was never armed (the line above is what "
                        "can be read without it)")
    return r.passed(*evidence)


def check_b1(d: Desk) -> Result:
    r = Result("B1", "a real kill mid-synthesis, reclaimed into the archive")
    if d.mode == "simulate":
        return r.skipped("needs the real unit: only a real SIGKILL leaves the "
                         "stranded TTS dir this check is about")
    if not d.is_active():
        return r.failed("the unit is not active, so there is nothing to kill")
    before = set(d.scratch_names())
    speech = d.speak("Please read this out slowly for the desk retest, "
                     "because the process has to be killed while it is still "
                     "speaking this sentence out loud.")
    # Kill MID-synthesis, not during the voice's load: the scratch dir exists
    # from the first moment, but its `tts.wav` only appears once synthesis
    # starts writing, and a kill before that strands an EMPTY dir — nothing
    # for B2's `aplay` to play (both live runs of 2026-09-25 did exactly
    # that). So wait until the wav has bytes, then kill while it writes.
    stranded_now: list[str] = []
    deadline = time.time() + 60.0
    while time.time() < deadline:
        stranded_now = sorted(set(d.scratch_names()) - before)
        if any((d.state / n / "tts.wav").is_file()
               and (d.state / n / "tts.wav").stat().st_size > 0
               for n in stranded_now):
            break
        time.sleep(1.0)
    pid = d.kill9()
    speech.poll()
    time.sleep(2.0)
    stranded = sorted(set(d.scratch_names()) - before)
    for name in stranded:
        d.created.append(d.state / name)      # ours to account for afterwards
    evidence = [f"killed PID {pid} mid-synthesis"]
    if not stranded:
        return r.failed(
            "no stranded scratch dir after SIGKILL — either nothing was "
            "speaking (start a long reply first) or the cleanup ran",
            *evidence)
    evidence.append("stranded: " + ", ".join(stranded))
    empty = [n for n in stranded
             if not (d.state / n / "tts.wav").is_file()
             or (d.state / n / "tts.wav").stat().st_size == 0]
    if empty:
        return r.failed(
            f"the stranded dir holds no tts.wav bytes ({', '.join(empty)}) — "
            "§10's B1 asks for the reply archived `with its tts.wav intact`, "
            "and an empty dir has nothing for B2 to play back",
            *evidence)
    # the automatic respawn is INSIDE the grace window and must not reclaim it
    deadline = time.time() + 60.0
    while time.time() < deadline and not d.is_active():
        time.sleep(2.0)
    for name in stranded:
        d.backdate(d.state / name)
    d.start_pass()
    sweeps = sweep_entries(d.log_since_last_start())
    still = [n for n in stranded if (d.state / n).exists()]
    archived = {n: d.archived(n) for n in stranded}
    if still:
        return r.failed(f"still in the state dir after the start: {still}",
                        *evidence)
    if not sweeps:
        return r.failed("the journal carries no `swept …` line for the "
                        "reclaim — the summary never reached the journal",
                        *evidence)
    evidence.append(f"journal: swept {sweeps[-1][0]} into {sweeps[-1][1]}")
    for name, path in archived.items():
        if path is None:
            return r.failed(f"{name} was reclaimed but is nowhere in the "
                            f"archive — a reclaim nobody can find", *evidence)
        evidence.append(f"archive: {path.relative_to(d.state)}")
    return r.passed(*evidence)


def check_b2(d: Desk) -> Result:
    r = Result("B2", "the archived bytes are the bytes that were reclaimed")
    payload = b"RIFF-desk-retest-payload"
    probe = d.plant_dir(f"{PROBE_PREFIX}-recover", payload=payload)
    d.backdate(probe)
    d.start_pass()
    archived = d.archived(probe.name)
    if archived is None:
        return r.failed("the probe was not archived, so there is nothing to "
                        "recover",
                        f"journal: {d.log_since_last_start()[-300:]}")
    recovered = archived / "tts.wav"
    same = recovered.exists() and recovered.read_bytes() == payload
    evidence = [f"planted sha256 {_sha(payload)} → "
                f"{archived.relative_to(d.state)}"]
    if not same:
        return r.failed("the archived copy differs from what was planted",
                        *evidence)
    evidence.append(f"recovered sha256 {_sha(payload)} (identical), "
                    f"{recovered.stat().st_size} bytes")
    return r.passed(*evidence)


def check_b3(d: Desk) -> Result:
    r = Result("B3", "the grace window: fresh scratch survives a start")
    probe = d.plant_dir(f"{PROBE_PREFIX}-fresh")
    d.start_pass()
    if not probe.is_dir():
        return r.failed("a fresh (inside-grace) probe was reclaimed — a "
                        "sibling mid-synthesis would lose its working dir",
                        f"line: {d.state_line()}")
    parsed = parse_state_line(d.state_line())
    if not parsed.get("scratch_left"):
        return r.failed("the probe survived but the line did not report it",
                        f"line: {d.state_line()}")
    d.backdate(probe)
    d.start_pass()
    if probe.exists():
        return r.failed("the backdated probe survived past its grace",
                        f"line: {d.state_line()}")
    if d.archived(probe.name) is None:
        return r.failed("the probe left the state dir but is not in the archive")
    return r.passed(
        f"fresh: survived and reported — {d.state_line()}",
        f"backdated: reclaimed into {d.archived(probe.name).relative_to(d.state)}")


def check_b4(d: Desk) -> Result:
    r = Result("B4", "the archive's own TTL drops expired dated folders")
    today = d.dated_archive()
    today.mkdir(parents=True, exist_ok=True)
    old_day = (datetime.date.today()
               - datetime.timedelta(days=8)).isoformat()
    old = d.dated_archive(old_day)
    old.mkdir(parents=True, exist_ok=True)
    (old / "evidence.wav").write_bytes(b"old")
    d.start_pass()
    if old.exists():
        return r.failed(f"the 8-day-old folder {old_day} survived a start",
                        f"archive: {sorted(p.name for p in d.archive_dir().iterdir())}")
    if not today.is_dir():
        return r.failed("today's folder was dropped with the expired one")
    return r.passed(f"expired {old_day} dropped; {today.name} kept")


def check_b5(d: Desk) -> Result:
    r = Result("B5", "a symlinked archive is refused, and destroys nothing")
    root = d.archive_dir()
    real = root.with_name(root.name + ".movedsaside")
    outside = d.root / "outside-target"
    probe = d.plant_dir(f"{PROBE_PREFIX}-symlink")
    d.backdate(probe)
    moved = False
    try:
        if root.is_symlink():
            return r.failed("the archive is already a symlink before the check")
        if root.exists():
            root.rename(real)
            moved = True
        outside.mkdir(parents=True, exist_ok=True)
        root.symlink_to(outside)
        d.start_pass()
        carried = list(outside.iterdir())
        if carried:
            return r.failed("reclaimed state was carried out of the bubble: "
                            f"{[p.name for p in carried]}")
        if not probe.exists():
            return r.failed("the stale entry was deleted instead of left in "
                            "place when the archive could not be made private")
        warned = any("could not make" in ln and "private" in ln
                     for ln in d.log_since_last_start().splitlines())
        evidence = ["nothing was carried out of the state dir; the probe is "
                    "still in place"]
        evidence.append("journal: warning present" if warned
                        else "journal: no refusal warning (the start was "
                             "quiet about it)")
        return r.passed(*evidence) if warned else r.failed(
            "the refusal happened but was never logged", *evidence)
    finally:
        try:
            if root.is_symlink():
                root.unlink()
        except OSError:
            pass
        if moved:
            real.rename(root)
        shutil.rmtree(outside, ignore_errors=True)


def check_c1(d: Desk) -> Result:
    r = Result("C1", "the `state:` line agrees with the disk")
    # the start is what makes the line carry THIS process's sweep record (a
    # client's reading says `sweep not run this process`), and the disk is read
    # back to back with it so both describe ONE moment
    d.start_pass()
    line = d.state_line()
    parsed = parse_state_line(line)
    if not line:
        return r.failed("no `state:` line at all")
    on_disk = d.scratch_names()
    if parsed["scratch_left"] is None:
        return r.failed("the line named neither a clean dir nor a count",
                        f"line: {line}")
    if parsed["scratch_left"] != len(on_disk):
        return r.failed(f"the line says {parsed['scratch_left']} "
                        f"scratch-shaped entries, the disk has {len(on_disk)}",
                        f"line: {line}", f"disk: {on_disk}")
    if parsed["entries"] is None or not parsed["size"]:
        return r.failed("the line carries no size/entry count", f"line: {line}")
    return r.passed(f"line: {line}",
                    f"disk: {len(on_disk)} scratch-shaped, "
                    f"{parsed['entries']} entries counted by the line")


def check_c2(d: Desk) -> Result:
    r = Result("C2", "the reclaim count in the line MATCHES the journal")
    probe = d.plant_dir(f"{PROBE_PREFIX}-pairing")
    probe_file = d.plant_file(PROBE_FILE)
    d.backdate(probe)
    d.backdate(probe_file)
    d.start_pass()
    sweeps = sweep_entries(d.log_since_last_start())
    parsed = parse_state_line(d.state_line())
    if not sweeps:
        return r.failed("no `swept` line in the journal for this start",
                        f"line: {d.state_line()}")
    journal_count = sum(count for count, _ in sweeps)
    if parsed["swept"] != journal_count:
        return r.failed(
            f"the line says {parsed['swept']} and the journal says "
            f"{journal_count} — the regression this retest exists for",
            f"line: {d.state_line()}", f"journal: swept {journal_count}")
    if parsed["swept"] != 2:
        return r.failed(f"expected the two planted probes to be reclaimed, "
                        f"the line says {parsed['swept']}",
                        f"line: {d.state_line()}")
    return r.passed(f"line: {d.state_line()}",
                    f"journal: swept {journal_count} (2 probes planted)")


def check_c3(d: Desk) -> Result:
    r = Result("C3", "the structured surface carries the same numbers")
    d.start_pass()             # same moment as C1's line, for the same reason
    out = d.hygiene_json()
    if not out:
        return r.failed("no `state_hygiene` JSON could be read")
    parsed = parse_state_line(d.state_line())
    if out.get("scratch_left") != parsed["scratch_left"]:
        return r.failed(f"JSON scratch_left {out.get('scratch_left')} vs line "
                        f"{parsed['scratch_left']}", json.dumps(out))
    if "last_sweep" not in out or "trend" not in out:
        return r.failed("the JSON is missing `last_sweep` or `trend`",
                        json.dumps(out))
    if d.mode == "systemd" and (out.get("last_sweep") or {}).get("at") is not None:
        return r.failed("a client process reported a sweep timestamp — it has "
                        "never run a start, so asking it is not side-effect "
                        "free", json.dumps(out))
    return r.passed(json.dumps(out, sort_keys=True))


def check_c4(d: Desk) -> Result:
    r = Result("C4", "the trend log: exactly one row per start, honest clause")
    log_path = d.state / "state-hygiene.jsonl"
    rows = []
    try:
        rows = [json.loads(ln) for ln in
                log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, ValueError) as exc:
        return r.failed(f"the trend log could not be read: {exc}")
    before = len(rows)
    d.start_pass()
    try:
        after = [json.loads(ln) for ln in
                 log_path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    except (OSError, ValueError) as exc:
        return r.failed(f"the trend log broke on the second start: {exc}")
    grew = len(after) - before
    if grew != 1:
        return r.failed(f"the log grew by {grew} rows for one start "
                        f"(expected exactly 1)", json.dumps(after[-2:]))
    clause = parse_state_line(d.state_line()).get("trend", "")
    if not clause:
        return r.failed("the line carries no trend clause",
                        f"line: {d.state_line()}")
    if not ("needs a week" in clause or "no readings yet" in clause
            or "day trend" in clause):
        return r.failed(f"an unrecognised trend clause: {clause}")
    return r.passed(f"rows {before} → {len(after)} for one start (exactly one "
                    f"row per start, 0600)",
                    f"clause: {clause}")


def check_c5(d: Desk) -> Result:
    """The week arithmetic, judged on a SYNTHETIC history in a throwaway root.

    Real history cannot be manufactured on the desk without lying in the
    user's own log, so this one check builds a week the honest way: a temp
    `XDG_STATE_HOME`, three rows, and the delivered code asked to render them.
    It runs in both back ends for the same reason — the arithmetic is the
    feature, and it must be proved against the copy that is deployed.
    """
    r = Result("C5", "the week-over-week delta (synthetic week, sandboxed)")
    root = Path(tempfile.mkdtemp(prefix="desk-retest-week-"))
    try:
        state = root / STATE_REL
        state.mkdir(parents=True, exist_ok=True)
        now = time.time()
        rows = [
            {"at": now - 9 * 86400, "date": "old", "scratch_left": 11,
             "size_bytes": 1_000_000_000, "entries": 52, "swept": 0},
            {"at": now - 3 * 86400, "date": "mid", "scratch_left": 0,
             "size_bytes": 1_500_000_000, "entries": 38, "swept": 11},
            {"at": now, "date": "now", "scratch_left": 0,
             "size_bytes": 2_500_000_000, "entries": 39, "swept": 0},
        ]
        (state / "state-hygiene.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8")
        env = dict(os.environ)
        env["XDG_STATE_HOME"] = str(root)
        env["XDG_CONFIG_HOME"] = str(root / "cfg")
        proc = subprocess.run([d.python, "-c", WEEK_PASS, str(d.bin_dir)],
                              capture_output=True, text=True, env=env,
                              timeout=180)
        line, trend = "", ""
        for out_line in proc.stdout.splitlines():
            if out_line.startswith("STATE_LINE "):
                line = out_line.split(" ", 1)[1]
            elif out_line.startswith("TREND_JSON "):
                trend = out_line.split(" ", 1)[1]
        # the 3-day-old row is INSIDE the window, so the 9-day-old one is the
        # baseline: 2.5 GB - 1.0 GB = +1.4 GB, and 39 - 52 = -13 entries
        if "7-day trend +1.4 GB, -13 entries" not in line:
            return r.failed(
                "the delta did not come from the oldest reading at least a "
                "week old", f"line: {line or '(none)'}",
                f"stderr: {proc.stderr.strip()[-200:]}")
        if '"since_days": 9.0' not in trend:
            return r.failed("the structured trend did not report the 9-day "
                            "span the delta came from",
                            f"trend: {trend or '(none)'}")
        return r.passed(f"line (synthetic week): {line}",
                        f"trend: {trend}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


CHECKS = (check_a5, check_b1, check_b2, check_b3, check_b4, check_b5,
          check_c1, check_c2, check_c3, check_c4, check_c5)
SKIP_UNDER_SIM = {"B1"}          # needs a real kill and real systemd


# -------------------------------------------------------------------- report
def render(results: list[Result], desk: Desk, started: str) -> str:
    """The paste-ready block: one line per item, the evidence beneath it, and
    the OWED line that names what only a person can close."""
    lines = [f"### §10 automated-half retest — {started} "
             f"({desk.mode} back end)", ""]
    for r in results:
        mark = {True: "✅", False: "❌", None: "⏭"}[r.ok]
        lines.append(f"- {mark} **{r.item}** {r.title}")
        for item in r.evidence:
            lines.append(f"      {item}")
        if r.ok is False:
            lines.append(f"      FAILED: {r.why}")
        elif r.ok is None:
            lines.append(f"      not run here: {r.why}")
    passed = sum(1 for r in results if r.ok is True)
    failed = [r.item for r in results if r.ok is False]
    skipped = [r.item for r in results if r.ok is None]
    lines += ["",
              f"automated: {passed}/{len(results)} passed"
              + (f", FAILED {' '.join(failed)}" if failed else "")
              + (f" (not run here: {' '.join(skipped)})" if skipped else ""),
              f"OWED at the desk (real voice — no substitute): "
              f"{' '.join(OWED_HUMAN)}"]
    if desk.notes:
        lines += ["", "notes:"] + [f"  - {n}" for n in desk.notes]
    return "\n".join(lines)


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base)


def build_desk(simulate: bool, python: str) -> Desk:
    if simulate:
        return SimDesk(Path(tempfile.mkdtemp(prefix="desk-retest-")), HERE,
                       python)
    return SystemdDesk(_state_root(), Path.home() / ".local/bin", python)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--simulate", action="store_true",
                        help="use a throwaway state dir and the checkout's own "
                             "code — no systemd, nothing on the desk disturbed")
    parser.add_argument("--only", default="",
                        help="comma-separated item ids to run (e.g. B1,C4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would run, change nothing")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    wanted = {item.strip().upper() for item in args.only.split(",") if item.strip()}
    checks = [c for c in CHECKS
              if not wanted or c.__name__.split("_")[-1].upper() in wanted]
    if not checks:
        sys.stderr.write(f"no checks match --only {args.only!r}\n")
        return 2
    started = _now()
    desk = build_desk(args.simulate, args.python)
    if args.dry_run:
        print(f"would run ({desk.mode}): "
              + " ".join(c.__name__.split("_")[-1].upper() for c in checks))
        print(f"state dir: {desk.state}")
        print(f"restarts:  one per check that needs a fresh start, paced to "
              f"the unit's own start limit")
        print(f"owed to a human afterwards: {' '.join(OWED_HUMAN)}")
        if args.simulate:
            shutil.rmtree(desk.root, ignore_errors=True)
        return 0

    if desk.mode == "systemd":
        desk.ensure_up()

    results = []
    try:
        for check in checks:
            item = check.__name__.split("_")[-1].upper()
            if desk.mode == "simulate" and item in SKIP_UNDER_SIM:
                results.append(Result(item, "desk-only").skipped(
                    f"needs the real unit (--simulate carries no systemd)"))
                continue
            result = check(desk)
            results.append(result)
            # each check owns its probes: clean them HERE, or the next check's
            # start reclaims the previous one's leftovers and its count is
            # wrong (found by --simulate, which shares one state dir)
            desk.cleanup()
            print(f"  {result.item}: "
                  + {True: "PASS", False: "FAIL", None: "skip"}[result.ok],
                  file=sys.stderr)
    finally:
        desk.cleanup()
    block = render(results, desk, started)
    red = any(r.ok is False for r in results)
    if args.simulate:
        # the throwaway root is this run's own litter — the suite sweeps its
        # sandbox homes for the same reason. A green run removes it; a red one
        # keeps it and says where, because that root is the only place a failed
        # check's leftovers can be looked at afterwards.
        if red:
            block += f"\n\nkept the throwaway root for a look: {desk.root}"
        else:
            shutil.rmtree(desk.root, ignore_errors=True)
    print(block)
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
