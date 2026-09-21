#!/usr/bin/env python3
"""Bake-off: can a System-1 decision engine pick the right tool family, fast?

WHY THIS EXISTS, AND WHY IT IS NOT WIRED INTO ANYTHING YET.

`handsoff` offers its whole tool belt to the model on every round — measured
2026-09-21 at 52 tools, 17 764 characters, ~4 441 tokens of a 32 768-token window
(48 tools and ~4 079 tokens on a config with the default-off families switched
off) — and lets a 12B model sort the request out from there. A typed-decision model (Laya) answers
a *choice* question over a fixed option set in one forward pass, ~50 ms on this
machine, which would let the turn narrow 48 schemas to a handful before the
prompt is built.

That is a real prize and it is also exactly the kind of prize this project has
been burned by assuming. So: measure first, wire only what measurably wins, and
keep the measurement reproducible and reviewable.

WHAT IT MEASURES, AND ON WHAT.

Two corpora, because they answer different questions:

* the AUTHORED set (`--authored`, the default) is written here, reviewed by a
  human, and covers every family with several phrasings — including the
  misheard, half-grammatical sentences a speech recogniser actually produces
  ("hey seifer tell me the way they're outside"). It measures whether the
  option set is even separable.
* the REAL set (`--real DIR`) is mined from the app's own `history.json`
  files: every user turn that the real 12B model answered with a tool call,
  labelled by the tool it actually chose. It is small (single digits today) and
  it is the only honest read on the real transcription distribution, so it is
  reported separately and never merged into the authored number.

Nothing from the real set is ever written back into this repository: private
utterances stay in the file they came from.

WHAT IT REPORTS.

Top-1 family accuracy, `top-k` recall from the model's own probabilities (the
advisory gate a router would actually use), the confusion between families, the
confidence distribution, per-decision latency, and — when a checkout is given —
the token arithmetic of narrowing the tool list to the winning family.

FAILURE IS DATA. `laya` is not a dependency of this project (torch is
deliberately an optional extra; the suite asserts it is never imported), so this
script is run BY HAND, under an interpreter that has it, and says so plainly
when it cannot run.

Usage:
    ~/.local/share/pipx/venvs/laya/bin/python ci/laya_bakeoff.py
    ... ci/laya_bakeoff.py --device cpu
    ... ci/laya_bakeoff.py --real ~/.config/handsoff
    ... ci/laya_bakeoff.py --checkout .        # adds the token arithmetic
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time

# --- the capability families a router would choose between ------------------
#
# Deliberately coarse: the unit is "which slice of the belt is worth offering",
# not the single right tool. Choosing a family wrongly must therefore be
# *survivable*, which is why the harness scores top-k and not only top-1.
FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "timers": (
        "set, list, cancel or snooze a timer, reminder or pomodoro",
        ("set_reminder", "list_reminders", "cancel_reminder", "snooze_reminder",
         "pomodoro"),
    ),
    "calendar": (
        "the user's own calendar, diary, meetings, or the current date and time",
        ("read_calendar", "calendar_month", "get_datetime"),
    ),
    "weather": (
        "the weather or forecast, and world news events",
        ("get_weather", "world_events"),
    ),
    "web": (
        "search the web, read a page, or look a fact up online",
        ("web_search", "read_page", "lookup_fact", "search_library"),
    ),
    "typing": (
        "put text into whatever has focus, press keys or a hotkey, copy and paste",
        ("type_text", "press_keys", "press_hotkey", "paste_text", "copy_text"),
    ),
    "windows": (
        "find, focus, move to a workspace, or close a window, and click or scroll"
        " on screen",
        ("focus_window", "close_window", "open_app", "workspace", "click_at",
         "click_element", "scroll", "screen_elements", "see_screen",
         "read_screen_text", "wait_for_window"),
    ),
    "media": (
        "play, pause, skip or change the volume of music and video",
        ("media_control", "media_play", "media_volume", "now_playing"),
    ),
    "system": (
        "run or start a shell command, check a job, kill a process, or report"
        " the machine's own health",
        ("run_command", "start_command", "job_status", "wait", "kill_process",
         "confirm_kill", "confirm_action", "handsoff_doctor",
         "niri_capabilities"),
    ),
    "files": (
        "read, edit or watch a file on disk",
        ("read_file", "edit_file", "watch_file", "watch_process"),
    ),
    "notifications": (
        "what notifications or messages arrived on this machine",
        ("notification_reader",),
    ),
    "desk": (
        "what an AI coding agent is doing in the Quantum Space desk, what its"
        " sessions are, or the tail of what one said",
        ("quant_space_status", "quant_space_sessions", "quant_space_read",
         "quant_space_check"),
    ),
    "none": ("just answer, or chat; no tool is needed", ()),
}

TOOL_FAMILY = {t: fam for fam, (_, tools) in FAMILIES.items() for t in tools}

# --- the authored set -------------------------------------------------------
#
# Every line is (utterance, family). Written to be *speech*, not prose: several
# carry the recogniser's own damage, because a router that only survives
# well-formed English has measured nothing about this app.
AUTHORED: list[tuple[str, str]] = [
    # timers
    ("set a timer for ten minutes", "timers"),
    ("remind me in one hour to take the bread out", "timers"),
    ("remind me in one second to drink water", "timers"),
    ("what reminders do I have", "timers"),
    ("cancel the timer", "timers"),
    ("give me five more minutes on that reminder", "timers"),
    ("start a twenty five minute pomodoro", "timers"),
    ("did I set anything for later", "timers"),
    # calendar
    ("what have I got on tomorrow morning", "calendar"),
    ("read me my calendar for today", "calendar"),
    ("when is my next meeting", "calendar"),
    ("what's the date", "calendar"),
    ("am I free on friday afternoon", "calendar"),
    ("show me the rest of the month", "calendar"),
    # weather
    ("what's the weather like outside", "weather"),
    ("hey seifer tell me the way they're outside", "weather"),   # real ASR
    ("is it going to rain tomorrow", "weather"),
    ("give me the world news", "weather"),
    ("anything happening in the world today", "weather"),
    ("weather in berlin this weekend", "weather"),
    # web
    ("how tall is mount everest", "web"),
    ("search the web for niri window rules", "web"),
    ("look up who invented the transistor", "web"),
    ("read me the top result", "web"),
    ("what does the arch wiki say about pipewire", "web"),
    ("and to say nothing bro, ask you to read the information from", "web"),  # real ASR
    # typing
    ("type exactly the ai takeover works then press enter", "typing"),
    ("something in the chat", "typing"),                          # real ASR
    ("paste that in", "typing"),
    ("press ctrl shift t", "typing"),
    ("write my address into this form", "typing"),
    # windows
    ("close this window for me", "windows"),
    ("focus my browser", "windows"),
    ("move this to the third workspace", "windows"),
    ("open a terminal", "windows"),
    ("click the save button", "windows"),
    ("scroll down a bit", "windows"),
    ("what is on my screen right now", "windows"),
    ("read the text in that window", "windows"),
    # media
    ("pause the music", "media"),
    ("stop the music", "media"),                                  # real
    ("turn the volume up", "media"),
    ("skip this track", "media"),
    ("what is playing", "media"),
    ("it's time for play music", "media"),                        # real ASR
    # system
    ("run the tests in this folder", "system"),
    ("what is the cpu temperature", "system"),
    ("how much memory is the machine using", "system"),
    ("kill that process", "system"),
    ("is the assistant healthy", "system"),
    ("start a long download in the background", "system"),
    ("are you still running that command", "system"),
    # files
    ("read the config file in my projects folder", "files"),
    ("add a line to the end of that script", "files"),
    ("tell me when that log changes", "files"),
    # notifications
    ("did anything pop up while I was away", "notifications"),
    ("read me my notifications", "notifications"),
    # none
    ("thanks that's all", "none"),
    ("what do you think about that", "none"),
    ("good morning", "none"),
    ("tell me a joke", "none"),
    ("who are you", "none"),
    ("that sounds right", "none"),

    # desk (the Quantum Space sessions — a family that was in the belt and in
    # no option at all until 2026-09-21: the token arithmetic was silently
    # pricing four tools as unroutable)
    ("what is claude doing", "desk"),
    ("what is the agent doing in the terminal", "desk"),
    ("read me the tail of what claude said", "desk"),
    ("which sessions are open in quantum space", "desk"),
    ("is quantum space running", "desk"),
    ("what did the coding agent just say", "desk"),
]


def questions(strict: bool = False) -> dict:
    """The typed questions: one choice over the families, one tool/no-tool bit.

    `strict` exists because the first run showed where the plain wording fails,
    and the failure is worth naming rather than smoothing over: with the plain
    set, Laya answered `none` — at confidence 1.00 — for requests that plainly
    need a tool ("look up who invented the transistor", "scroll down a bit",
    "how much memory is the machine using"). The option that absorbs those is
    `none`, so the strict variant says what `none` is NOT for. Whether that is
    a wording problem or a capability limit is exactly what running both tells
    us, and the answer decides whether this is worth wiring at all.
    """
    none_desc = FAMILIES["none"][0]
    instr = "Which single capability would answering this request need?"
    if strict:
        none_desc = ("pure conversation ONLY — a greeting, thanks, an opinion, a "
                     "joke, or a question about the assistant itself. NEVER "
                     "choose this for a request that needs information the "
                     "assistant does not already have, or that asks it to do "
                     "something on the machine.")
        instr = ("Which single capability must the assistant use to answer this? "
                 "If the request asks for something done — "
                 "look it up, read it, set it, type it, play it, open it, close "
                 "it, tell me the time or the weather or what is on my screen — "
                 "choose that capability. `none` is only for conversation with "
                 "nothing to act on.")
    return {
        "family": {
            "type": "choice",
            "instructions": instr,
            "criteria": {name: (none_desc if name == "none" else desc)
                         for name, (desc, _) in FAMILIES.items()},
        },
        "needs_tool": {
            "type": "noul",
            "instructions": ("Does answering this require running a tool or acting"
                             " on the machine, rather than only speaking?"),
        },
    }


def tool_token_costs(checkout: pathlib.Path) -> dict | None:
    """{family: tokens} for the schemas a narrowed list would carry.

    Best-effort and isolated on purpose: the checkout's `core/tools.py` is
    importable without the app (it ships standalone), but if this machine's
    environment refuses, the harness reports accuracy anyway and says the token
    arithmetic was skipped rather than dying.
    """
    try:
        sys.path.insert(0, str(checkout))
        import core.tools as core_tools  # type: ignore

        built = core_tools.build_tools()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"  (token arithmetic skipped: {type(exc).__name__}: {exc})")
        return None
    by_name = {t["function"]["name"]: t for t in built}
    all_tokens = len(json.dumps(built)) // 4
    out = {"__all__": all_tokens, "__offered__": len(built)}
    for fam, (_, tools) in FAMILIES.items():
        rows = [by_name[t] for t in tools if t in by_name]
        out[fam] = len(json.dumps(rows)) // 4
    missing = sorted(set(TOOL_FAMILY) - set(by_name))
    unmapped = sorted(set(by_name) - set(TOOL_FAMILY))
    out["__missing__"] = missing
    out["__unmapped__"] = unmapped
    return out


def mine_real(dirpath: pathlib.Path) -> list[tuple[str, str, str]]:
    """(utterance, family, source) from the app's own history files.

    The label is the tool the REAL 12B model chose for that turn — ground truth
    about intent, and the only corpus on this machine that carries the
    recogniser's own damage. Read-only: nothing is written back.
    """
    rows, seen = [], set()
    for path in sorted(dirpath.glob("history.json*")):
        try:
            hist = json.loads(path.read_text(encoding="utf-8") or "[]")
        except (OSError, ValueError):
            continue
        if not isinstance(hist, list):
            continue
        for i, msg in enumerate(hist):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            text = str(msg.get("content") or "").strip()
            tools: list[str] = []
            for nxt in hist[i + 1:]:
                if not isinstance(nxt, dict) or nxt.get("role") == "user":
                    break
                for call in nxt.get("tool_calls") or []:
                    name = (call.get("function") or {}).get("name")
                    if name:
                        tools.append(name)
            fams = {TOOL_FAMILY[t] for t in tools if t in TOOL_FAMILY}
            # One family, and it must own every tool the turn called: a turn
            # that spans two families is not a routing case, it is two.
            if text and len(fams) == 1 and len(tools) == len([t for t in tools
                                                             if t in TOOL_FAMILY]):
                fam = fams.pop()
                if (text, fam) not in seen:
                    seen.add((text, fam))
                    rows.append((text, fam, path.name))
    return rows


def run(agent, cases, label, log, strict: bool = False):
    # noqa: ANN001 - laya's Agent, by design
    """Score one corpus and print the table.

    `cases` may be (utterance, family) or (utterance, family, source): the real
    corpus carries the file it came from so a private utterance can be traced
    back to the run that produced it.
    """
    if not cases:
        print(f"\n=== {label}: no cases ===")
        return {}
    print(f"\n=== {label} ({len(cases)} cases) ===")
    print(f"  {'':<5}{'utterance':<52}{'picked':<16}{'want':<16}conf")
    hits = 0
    topk = collections.Counter()
    confusion: collections.Counter = collections.Counter()
    confs: list[float] = []
    times: list[float] = []
    for case in cases:
        text, want = case[0], case[1]
        t0 = time.perf_counter()
        answers = agent.predict({"utterance": text}, questions(strict))["answers"]
        times.append((time.perf_counter() - t0) * 1000)
        fam = answers["family"]
        picked = fam["choice"]
        conf = float(fam.get("confidence") or 0.0)
        probs = fam.get("probabilities") or {}
        confs.append(conf)
        hits += picked == want
        if picked != want:
            confusion[(want, picked)] += 1
        ranked = [k for k, _ in sorted(probs.items(), key=lambda kv: -kv[1])]
        for k in (1, 2, 3, 5):
            if want in ranked[:k]:
                topk[k] += 1
        mark = "ok " if picked == want else "MISS"
        print(f"  {mark}{'':<3}{text[:50]:<52}{picked:<16}{want:<16}{conf:.2f}")
        log.append({"set": label, "text": text, "want": want, "picked": picked,
                    "confidence": conf, "rank": (ranked.index(want) + 1
                                                 if want in ranked else None)})
    n = len(cases)
    times.sort()
    print(f"\n  top-1 accuracy : {hits}/{n} ({hits / n:.0%})")
    for k in (2, 3, 5):
        print(f"  recall@{k}      : {topk[k]}/{n} ({topk[k] / n:.0%})  "
              f"<- what an advisory router can actually use")
    print(f"  confidence     : p50 {sorted(confs)[n // 2]:.2f}, "
          f"max {max(confs):.2f} (its README suggests acting at 0.85)")
    print(f"  latency        : p50 {times[n // 2]:.0f} ms, max {times[-1]:.0f} ms")
    if confusion:
        print("  confused:")
        for (want, picked), count in confusion.most_common(8):
            print(f"    {want} -> {picked}  x{count}")
    return {"n": n, "hits": hits, "topk": dict(topk), "confs": confs,
            "times": times, "confusion": dict(confusion)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="cuda", choices=("cuda", "cpu"))
    parser.add_argument("--variant", default="both",
                        choices=("plain", "strict", "both"),
                        help="the option wording: `strict` spells out what "
                             "`none` is not for (see questions())")
    parser.add_argument("--real", type=pathlib.Path, default=None,
                        help="directory of history.json* files to mine (private)")
    parser.add_argument("--checkout", type=pathlib.Path, default=None,
                        help="a checkout, for the tool-schema token arithmetic")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="write the per-case log here (default: don't)")
    parser.add_argument("--checkpoint", type=pathlib.Path, default=None,
                        help="grade a LOCAL checkpoint directory (e.g. one this "
                             "repo's ci/laya_finetune.py wrote) instead of the "
                             "zero-shot model")
    parser.add_argument("--grown", type=pathlib.Path, default=None,
                        help="extra rows mined into a corpus JSONL "
                             "(ci/laya_corpus.py --grow), scored as their own set")
    parser.add_argument("--ignore-temperature", action="store_true",
                        help="drop the checkpoint's own temperature table, so "
                             "the ranking and the confidence can be told apart")
    args = parser.parse_args(argv[1:])

    try:
        import torch

        import laya
    except ImportError as exc:
        print(f"laya is not importable here ({exc}).")
        print("Run it with the interpreter that has it, e.g.")
        print("  ~/.local/share/pipx/venvs/laya/bin/python ci/laya_bakeoff.py")
        return 2

    device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        print("cuda was asked for and is not available; falling back to cpu")
        device = "cpu"
    print(f"laya {getattr(laya, '__version__', '?')} | device {device} | "
          f"families {len(FAMILIES)} | tools covered {len(TOOL_FAMILY)}")

    t0 = time.perf_counter()
    if args.checkpoint:
        agent = laya.load(str(args.checkpoint.resolve()), device=device)
        which = str(args.checkpoint)
    else:
        agent = laya.load("convaiinnovations/laya", subfolder="typed-decisions",
                          device=device)
        which = "convaiinnovations/laya (typed-decisions, zero-shot)"
    print(f"checkpoint loaded in {time.perf_counter() - t0:.1f}s"
          f"{' / %.0f MiB vram' % (torch.cuda.memory_allocated() / 2**20) if device == 'cuda' else ''}"
          f"\n  model: {which}")
    if args.ignore_temperature:
        # The checkpoint calibrates by OPTION COUNT (`temperature_by_options`),
        # and this task's option set lands in its widest bucket. Ranking is
        # unaffected by a positive scale, so this isolates what the table does
        # to the CONFIDENCE rather than to the decision.
        table = dict(getattr(agent, "temperature_by_options", {}) or {})
        agent.temperature_by_options = {k: 1.0 for k in table}
        print(f"  temperature table pinned to 1.0 (was: "
              f"{', '.join(f'{k}={v:.3g}' for k, v in table.items())})")

    variants = (["plain", "strict"] if args.variant == "both"
                else [args.variant])
    log: list[dict] = []
    authored = real = None
    for variant in variants:
        strict = variant == "strict"
        authored = run(agent, AUTHORED, f"authored set ({variant} wording)", log,
                       strict) or authored
        real = run(agent, mine_real(args.real) if args.real else [],
                   (f"real set ({args.real}, {variant})" if args.real
                    else "real set (none given)"), log, strict) or real
        if args.grown:
            grown: list[tuple[str, str, str]] = []
            for line in args.grown.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                text, fam = row.get("text"), row.get("family")
                if text and fam in FAMILIES:
                    grown.append((text, fam, str(row.get("source") or "grown")))
            run(agent, grown, f"grown corpus ({args.grown}, {variant} wording)",
                log, strict)

    if args.checkout:
        print(f"\n=== tool-schema arithmetic ({args.checkout}) ===")
        costs = tool_token_costs(args.checkout.resolve())
        if costs:
            print(f"  belt as shipped: {costs['__offered__']} tools, "
                  f"~{costs['__all__']:,} tokens of schema per round")
            for fam, (_, tools) in FAMILIES.items():
                if tools:
                    print(f"  {fam:<14} {len(tools):>2} tools  "
                          f"~{costs[fam]:>4,} tokens")
            if costs["__unmapped__"]:
                print(f"  NOT in any family: {', '.join(costs['__unmapped__'])}")
            if costs["__missing__"]:
                print(f"  in a family, absent from the belt: "
                      f"{', '.join(costs['__missing__'])}")
    if args.out:
        args.out.write_text(json.dumps(log, indent=1), encoding="utf-8")
        print(f"\nper-case log: {args.out}")
    if not (authored or real):
        print("\nnothing scored")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
