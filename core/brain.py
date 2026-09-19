"""Dependency-injected Ollama and per-turn brain primitives."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import urllib.error
import urllib.request
from typing import Callable, MutableMapping


# Control-token leakage is a small, CLOSED set, and matching only that set is
# the point: the old filter dropped any sentence merely starting with '<', so
# legitimate replies like "<3" or "it's <5 minutes away" were silently
# swallowed before speech. It also only stripped CLOSED <think> blocks, so an
# unterminated one streamed the model's reasoning straight into TTS.
_LEAKED_MARKUP = re.compile(
    r"^\s*(?:</?(?:think|tool_calls?|im_start|im_end)\b|<\|)", re.IGNORECASE)


def is_leaked_markup(sentence: str) -> bool:
    """True only for control-token leakage — not for any sentence starting '<'."""
    return bool(_LEAKED_MARKUP.match(sentence or ""))


def strip_thinking(text: str) -> str:
    """Clean model output before it is sent to speech."""
    value = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    # an UNCLOSED block: drop it and everything after it, or the reasoning
    # text is spoken aloud
    value = re.sub(r"<think>.*\Z", "", value, flags=re.DOTALL)
    value = re.sub(r"^\s*\[TOOL_CALLS\][^\n]*(?:\n|$)", "", value,
                   flags=re.MULTILINE)
    return value.strip()


class TurnStream:
    """Result and queue ownership for one isolated model turn."""

    def __init__(self, generation: int, cancel: threading.Event,
                 sentence_q: "queue.Queue[str | None]") -> None:
        self.generation = generation
        self.cancel = cancel
        self.sentence_q = sentence_q
        self.result = None
        self.done = threading.Event()


def _read_http_error(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(error.read().decode("utf-8")).get("error", ""))
    except Exception:
        return str(error.reason)


def _defaults(base: str, model: str, num_ctx: int, tools: list[dict] | None,
             stream: bool, keep_alive: str | None) -> dict:
    payload = {
        "model": model,
        "messages": None,
        "stream": stream,
        "think": False,
        "keep_alive": keep_alive if keep_alive is not None
        else os.environ.get("HANDSOFF_KEEP_ALIVE", "1h"),
        "options": {"temperature": 0.3, "num_ctx": num_ctx},
    }
    if tools:
        payload["tools"] = tools
    return payload


_TAIL_SYSTEM_WARNED = False


def _messages_system_first(messages: list[dict],
                           logger: logging.Logger) -> list[dict]:
    """Fold any system message that is not the first one into a user turn.

    Ollama refuses the WHOLE request with HTTP 500 ("system message must be at
    the beginning") when a system message follows the first. Verified live
    against Ollama 0.32.13 and qwen3.8:27b: the same list that 500s there is
    accepted by gemma4:latest, so the failure looks like a broken brain on one
    model and works on another. A caller that appends a per-turn note as a
    system message would therefore kill every turn, so the one place every
    request passes through enforces the API's role rule instead of trusting
    each caller — and names the offending content once in the journal.
    """
    global _TAIL_SYSTEM_WARNED
    if not any(m.get("role") == "system" for m in messages[1:]):
        return messages
    head = []
    rest = list(messages)
    # at most one leading system message may stay: two of them is the same
    # shape that 500s (the renderer allows only the first)
    for index, message in enumerate(rest):
        if message.get("role") == "system":
            head.append(str(message.get("content") or ""))
            continue
        rest = rest[index:]
        break
    else:
        rest = []
    parts = [s for s in head[1:] if s]
    parts += [str(m.get("content") or "") for m in rest
              if m.get("role") == "system" and str(m.get("content") or "")]
    kept = [m for m in rest if m.get("role") != "system"]
    note = "\n\n".join(parts)
    if not note and len(head) < 2:
        return messages
    if note:
        target = next((i for i in range(len(kept) - 1, -1, -1)
                       if kept[i].get("role") == "user"), None)
        if target is None:
            kept.append({"role": "user", "content": note})
        else:
            kept[target] = {**kept[target],
                            "content": note + "\n\n"
                            + str(kept[target].get("content") or "")}
        if not _TAIL_SYSTEM_WARNED:
            _TAIL_SYSTEM_WARNED = True
            logger.warning(
                "model call had a system message after the first (Ollama "
                "answers HTTP 500); folded into the user turn: %s", note[:200])
    if not head:
        return kept
    merged = "\n\n".join(h for h in head if h)
    return [{"role": "system", "content": merged}] + kept


def ollama_available(*, base: str, guard: Callable[[], None],
                     urlopen: Callable = urllib.request.urlopen) -> bool:
    try:
        guard()
        with urlopen(base + "/api/tags", timeout=3) as response:
            json.loads(response.read().decode("utf-8"))
        return True
    except Exception:
        return False


def ollama_unload(*, base: str, model: str, guard: Callable[[], None],
                  logger: logging.Logger,
                  urlopen: Callable = urllib.request.urlopen,
                  timeout: float = 10.0) -> bool:
    """Ask Ollama to drop `model` from memory now, and say whether it answered.

    `keep_alive: 0` on a generate call is the documented unload, and using it
    here rather than a second API deliberately reuses the ONE field the chat
    path already sets: every turn asks for a long keep-alive, so the thing that
    takes it back is the same knob instead of a separate call that can drift
    from it. Unloading a model that is not loaded is not an error (Ollama
    answers 200 with `done_reason: unload`), which is what makes this safe to
    send on a timer without checking first.

    A failure is reported, never raised: the caller is an idle tick, and a
    stopped Ollama, a wrong host or an unreachable port all mean the same
    thing — no process is holding that memory to give back.
    """
    guard()
    payload = {"model": model, "keep_alive": 0}
    req = urllib.request.Request(
        base + "/api/generate", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            response.read()
        return True
    except Exception as exc:
        logger.debug("ollama unload of %s skipped (%s): %s",
                     model, type(exc).__name__, exc)
        return False


#: Below this many bytes, a `size`/`size_vram` report is not a model at all.
#: Guards the unit: a server that answered in MB instead of bytes would read as
#: a 0.006 GB model, and the policy would skip a release it should make for a
#: reason that is really a parsing accident.
_MIN_PLAUSIBLE_BYTES = 1_000_000


def _gib(value) -> float | None:
    """A byte count as GiB: 0.0 for a reported zero, None for anything else.

    The two are kept apart because they mean different things downstream: zero
    VRAM is a model that is genuinely all on the CPU, while an unreadable size
    is missing telemetry — and the release decision says so differently.
    """
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return 0.0
    if size < _MIN_PLAUSIBLE_BYTES:
        return None                 # not a model size: some other unit
    return size / float(1024 ** 3)


def ollama_resident(*, base: str, model: str, guard: Callable[[], None],
                    logger: logging.Logger,
                    urlopen: Callable = urllib.request.urlopen,
                    timeout: float = 3.0) -> dict | None:
    """What Ollama has loaded, and how much of it is actually on the card.

    `/api/ps` is the only endpoint that reports the SPLIT. A model larger than
    the GPU is served partly from system memory, and `size_vram` is then the
    fraction that really occupies the card — which is the number an idle release
    has to weigh, since a split model costs a full reload of its whole self to
    hand back that fraction.

    Returns None when the answer is UNKNOWN (unreachable, malformed, no
    models list) — deliberately distinct from `{"loaded": False}`, because the
    two lead to different decisions and only one of them is a measurement.
    """
    guard()
    req = urllib.request.Request(base + "/api/ps")
    try:
        with urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("ollama /api/ps probe failed (%s): %s",
                     type(exc).__name__, exc)
        return None
    entries = body.get("models") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        names = {str(entry.get(key) or "") for key in ("name", "model")}
        if model in names:
            return {"loaded": True,
                    "size": _gib(entry.get("size")),
                    "size_vram": _gib(entry.get("size_vram")),
                    "expires_at": str(entry.get("expires_at") or "")}
    return {"loaded": False, "size": None, "size_vram": None,
            "expires_at": ""}


def ollama_model_size_mb(*, base: str, model: str, guard: Callable[[], None],
                         logger: logging.Logger,
                         urlopen: Callable = urllib.request.urlopen,
                         timeout: float = 3.0) -> int | None:
    """How much card the configured model needs, as Ollama reports it (MiB).

    `/api/ps` can only price a model that is LOADED, and the question this
    answers is asked before a turn loads one: `/api/tags` lists every model with
    its blob size, which is what a full offload of it costs the card. A reader
    who needs the split (how much is on the card right now) wants
    `ollama_resident` instead; this is the footprint, not the residency.

    None means "unknown" — an unreachable server, a malformed body, or a model
    the server does not list — and never zero, because zero would read as "fits
    anywhere" to the arithmetic that consumes it. Sizes round UP: the point of
    the number is deciding whether a claim fits, and under-claiming the card's
    cost is the direction that asks another tenant to move for nothing.
    """
    guard()
    req = urllib.request.Request(base + "/api/tags")
    try:
        with urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("ollama /api/tags probe failed (%s): %s",
                     type(exc).__name__, exc)
        return None
    entries = body.get("models") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # The same name field and the same exact match `ollama_resident` uses,
        # so the two readers cannot disagree about which entry is the model.
        names = {str(entry.get(key) or "") for key in ("name", "model")}
        if model not in names:
            continue
        try:
            size = int(entry.get("size"))
        except (TypeError, ValueError):
            return None
        if size < _MIN_PLAUSIBLE_BYTES:
            return None             # not a model size: some other unit
        return -(-size // (1024 * 1024))
    return None


def ollama_release_verdict(resident: dict | None, *, reload_s, wait_s_per_gb,
                           model: str = "") -> dict:
    """Should an idle release hand this model's memory back? With the reason.

    The release is not free: the next question pays the model's whole load
    again, and the prompt-prefix cache with it. Measured on the machine this was
    written for, an 18 GB model that Ollama had split across CPU and GPU took
    218.9 s to come back while holding 9.7 GB of the card — 22.6 s of next-turn
    wait for every GB handed back. A model that fits in VRAM loads in seconds,
    so the same rule keeps releasing that one. Weighing the two quantities the
    machine actually reports (`size_vram`, and the reload the host measured)
    is what makes this a decision about THIS model rather than a policy about
    memory in general.

    Pure on purpose: every branch is reachable by feeding it a dict, so the
    truth table is a test rather than a description. `note` is the reason, in
    the words both the journal and the doctor print, and it always names the
    numbers the decision used.
    """
    # No budget (0, junk, absent) means "do not weigh the cost at all": the
    # exchange rate is the user's to set, and a value nobody can read must not
    # become a reason to hold memory the machine may need. The default lives in
    # the settings table, so there is one place it is written down.
    try:
        budget = max(0.0, float(wait_s_per_gb))
    except (TypeError, ValueError):
        budget = 0.0

    def verdict(release: bool, note: str, freed_gb=None, per_gb=None) -> dict:
        return {"release": bool(release), "note": note, "model": model,
                "freed_gb": freed_gb, "wait_per_gb": per_gb, "budget": budget}

    if resident is None:
        return verdict(True, "Ollama did not answer /api/ps, so the resident "
                             "state is unknown — releasing, as before")
    if not resident.get("loaded"):
        return verdict(False, "nothing resident in VRAM to give back")
    freed_gb = resident.get("size_vram")
    if freed_gb is None:
        return verdict(False, "the /api/ps entry carries no usable size_vram, "
                              "so what the unload would actually free is "
                              "unknown")
    if not freed_gb:
        return verdict(False, "resident on CPU only; unloading would free no "
                              "GPU memory")
    if reload_s is None:
        return verdict(True, f"{freed_gb:.1f} GB to give back, but the reload "
                              "is not measured yet — nothing to weigh it "
                              "against", freed_gb=freed_gb)
    try:
        cost = float(reload_s)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0.0:
        cost = 0.0
    per_gb = cost / freed_gb
    if budget <= 0.0:
        return verdict(True, f"cost is not weighed "
                              f"(llm_release_wait_s_per_gb 0) — releasing "
                              f"{freed_gb:.1f} GB", freed_gb=freed_gb,
                       per_gb=per_gb)
    if per_gb <= budget:
        return verdict(True, f"{freed_gb:.1f} GB back for a measured "
                              f"{cost:.0f} s reload ({per_gb:.1f} s/GB ≤ "
                              f"{budget:.0f})", freed_gb=freed_gb,
                       per_gb=per_gb)
    return verdict(False, f"{freed_gb:.1f} GB back would cost a measured "
                          f"{cost:.0f} s reload ({per_gb:.1f} s/GB > "
                          f"{budget:.0f}); raise llm_release_wait_s_per_gb to "
                          f"release anyway", freed_gb=freed_gb, per_gb=per_gb)


def ollama_chat(messages: list[dict], tools: list[dict] | None = None, *,
                base: str, model: str, num_ctx: int,
                guard: Callable[[], None], logger: logging.Logger,
                state: MutableMapping[str, bool] | None = None,
                urlopen: Callable = urllib.request.urlopen,
                keep_alive: str | None = None) -> dict:
    guard()
    payload = _defaults(base, model, num_ctx, tools, False, keep_alive)
    messages = _messages_system_first(messages, logger)
    payload["messages"] = messages
    req = urllib.request.Request(
        base + "/api/chat", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=300) as response:
            body = json.loads(response.read().decode("utf-8"))
        if tools and state is not None:
            state["tools_supported"] = True
        return body.get("message") or {}
    except urllib.error.HTTPError as error:
        detail = _read_http_error(error)
        if error.code == 400 and tools and "tool" in detail.lower():
            logger.warning("model %s does not support tools; continuing without", model)
            if state is not None:
                # Record the refusal: the caller reads this back so the next
                # turn does not pay the same failed round-trip. It was only
                # ever set to True, so a tool-less model re-probed on every
                # single turn.
                state["tools_supported"] = False
            return ollama_chat(messages, None, base=base, model=model,
                                num_ctx=num_ctx, guard=guard, logger=logger,
                                state=state, urlopen=urlopen,
                                keep_alive=keep_alive)
        if error.code == 404 and "model" in str(detail).lower():
            detail += f" — run: ollama pull {model}"
        raise RuntimeError(f"Ollama error {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"cannot reach Ollama at {base} ({error.reason}). "
            "Start it with: systemctl start ollama"
        ) from None


def ollama_chat_stream(messages: list[dict], q: "queue.Queue[str | None]",
                       cancel: threading.Event | None = None,
                       tools: list[dict] | None = None, *, base: str,
                       model: str, num_ctx: int, guard: Callable[[], None],
                       logger: logging.Logger,
                       state: MutableMapping[str, bool] | None = None,
                       urlopen: Callable = urllib.request.urlopen,
                       keep_alive: str | None = None) -> dict:
    """Stream chat content into q, ending it with exactly one terminator.

    EVERY step that can raise lives inside the `try`, `guard()` and the request
    build included. The `finally` is what puts the terminator on the queue, and
    the speaker blocks on that queue until it sees one — so a raise from before
    the `try` did not fail the turn, it HUNG the voice loop with no apology and
    no recovery, freeable only by a manual barge-in. The docstring promised
    "exactly one terminator"; this makes the promise unconditional rather than
    a claim that held only for the failures the author had in mind.
    """
    buf = ""
    full = ""
    tool_calls: list[dict] = []
    fallback = False
    try:
        guard()
        payload = _defaults(base, model, num_ctx, tools, True, keep_alive)
        messages = _messages_system_first(messages, logger)
        payload["messages"] = messages
        req = urllib.request.Request(
            base + "/api/chat", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=300) as response:
            for raw in response:
                if cancel is not None and cancel.is_set():
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                message = chunk.get("message") or {}
                if message.get("tool_calls"):
                    tool_calls.extend(message["tool_calls"])
                piece = message.get("content") or ""
                if not piece:
                    continue
                buf += piece
                full += piece
                while True:
                    match = re.search(r"[.!?…](\s|$)", buf)
                    if not match:
                        break
                    sentence, buf = buf[:match.end()], buf[match.end():]
                    sentence = strip_thinking(sentence).strip()
                    if sentence and not is_leaked_markup(sentence):
                        q.put(sentence)
        # Only flush what is still pending if the turn was NOT cancelled:
        # after a barge-in the tail was queued anyway and spoken over the user.
        if not (cancel is not None and cancel.is_set()):
            tail = strip_thinking(buf).strip()
            if tail and not is_leaked_markup(tail):
                q.put(tail)
    except urllib.error.HTTPError as error:
        detail = _read_http_error(error)
        if error.code == 400 and tools and "tool" in detail.lower():
            logger.warning("model %s does not support tools; continuing without", model)
            if state is not None:
                state["tools_supported"] = False   # see ollama_chat
            fallback = True
            return ollama_chat_stream(
                messages, q, cancel, None, base=base, model=model,
                num_ctx=num_ctx, guard=guard, logger=logger, state=state,
                urlopen=urlopen, keep_alive=keep_alive)
        logger.error("streaming chat HTTP error: %s", error)
        raise RuntimeError(f"Ollama error {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        # Same conversion the non-streaming path makes, and for the same
        # reason: a refused/unreachable Ollama is the common failure, and the
        # caller (`_brain_turn`) diagnoses a down brain from RuntimeError only.
        # Letting URLError escape meant a dead Ollama was reported to the user
        # as "my brain gave me an empty answer" while the real cause (with the
        # `systemctl start ollama` fix) went unspoken and the error surfaced as
        # an unhandled exception in the streamer thread.
        logger.error("streaming chat cannot reach %s: %s", base, error.reason)
        raise RuntimeError(
            f"cannot reach Ollama at {base} ({error.reason}). "
            "Start it with: systemctl start ollama"
        ) from None
    except Exception:
        logger.exception("streaming chat failed")
        raise
    finally:
        if not fallback:
            q.put(None)
    if tools and state is not None:
        state["tools_supported"] = True
    return {"tool_calls": tool_calls, "content": strip_thinking(full).strip()}
