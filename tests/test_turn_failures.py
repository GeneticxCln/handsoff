"""What the user HEARS when a turn fails, measured at the speaker.

Every other suite proves a failure was *detected*. This one measures the
consequence: the sentence that comes out of the speaker, captured where it
leaves for real — `tts_to_wav` (the first hand the text passes to) and
`_audio.play_wav` (the last). Nothing here stubs `_speak`: the real
`_brain_turn`, the real `_speak`, the real `_SentenceQueue`, the real worker
threads and the real HTTP seam all run, and the survey is what reaches
playback.

The survey was taken by injection, not by reading. Driving every boundary and
recording the line is the only way to know which failures are *spoken* at all,
because three of them are not: a model that answers with nothing, a speech
model that cannot be loaded, and a playback device that is gone all leave the
user in silence — no apology, no bubble state — and only the last of the three
says anything at all, in a desktop popup the user may never look at.

What the pass found, all reproduced before it was written down. Four of the
five are FIXED and pinned below; the fifth is pinned as it stands, because
what it should do instead is a product decision nobody has made.

* FIXED — the two arms did not agree on the sentence. The streaming arm (the
  default) spoke `RuntimeError: cannot reach Ollama at ...`; the non-streaming
  arm spoke `cannot reach Ollama at ...`. Same event, one of them silently
  dropping the exception class, which is what tells a reader whether the brain
  refused or the network broke. A `streaming_tts` toggle changed what a failure
  sounded like. The two now read the cause the same way and one test drives
  both.

* FIXED — a speech or playback failure is silent, and worse: the reply the
  user never heard was written to history, so the next turn's prompt contained
  a sentence the user had no memory of hearing. A turn whose answer could not
  be said now keeps its question and drops its answer.

* FIXED — a barge-in between synthesis and playback was recorded as a spoken
  reply in the non-streaming arm, and left the echo filter armed on it in
  BOTH. `_speak`'s own comment claims "spoke must mean the user HEARD it",
  and the claim held one step further on than the fix that made it looked.

* STILL OPEN, and pinned as measured: a stream that dies part-way through
  speaks the sentences it got AND the apology, back to back, and appends
  neither to memory. The user is told "my brain is offline" immediately after
  being told the answer.

* STILL OPEN, and pinned as measured: an empty model reply is silent in both
  arms. Not "Sorry, nothing came back" — nothing, and the bubble never leaves
  `thinking`.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import urllib.error
import wave

import pytest


# --------------------------------------------------------------- the seams

class _Response:
    """A urlopen response that yields NDJSON lines, or reads a JSON body."""

    def __init__(self, lines=(), body=b'{"message": {"content": ""}}'):
        self._lines = list(lines)
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._body


def _chunk(content=None, tools=None):
    message: dict = {}
    if content is not None:
        message["content"] = content
    if tools is not None:
        message["tool_calls"] = tools
    return json.dumps({"message": message}).encode("utf-8") + b"\n"


def _http_error(code: int, detail: str) -> urllib.error.HTTPError:
    """An HTTPError that answers `_read_http_error` the way Ollama's does."""
    error = urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", code,
                                   detail, {}, None)
    body = json.dumps({"error": detail}).encode("utf-8")
    error.read = lambda: body
    return error


def _refused(req):
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


class _Quiet:
    """A server that accepts the connection and then says nothing at all."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        raise StopIteration


def _raises(exc):
    def opener(req):
        raise exc
    return opener


def _never_leaves(exc):
    """The read loop raises an exception that is not an `Exception`."""
    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            raise exc
    return Boom()


# ------------------------------------------------------------------ driver

class Turn:
    """One driven turn: what reached the speaker, and what memory kept."""

    def __init__(self):
        self.said: list[str] = []      # handed to the synthesiser
        self.played: list[str] = []    # reached the speaker
        self.popups: list[str] = []    # desktop notifications
        self.states: list[str] = []    # bubble transitions
        self.requests = 0
        self.assistant = None

    @property
    def heard(self) -> str:
        """Exactly what the user's ears got, in order."""
        return " ".join(self.played)

    @property
    def history(self) -> list[dict]:
        return self.assistant._history

    @property
    def spoke(self) -> bool:
        return bool(self.assistant._turn_spoke)

    @property
    def echo_armed(self) -> list[str]:
        return list(self.assistant._recently_spoken)


def drive(H, monkeypatch, opener, *, streaming=True, tts_exc=None,
          play_exc=None, guard=None, cancel=None, cancel_after=None,
          tool_entry=None):
    """Run the real turn and capture what the speaker was handed.

    `cancel_after` fires `cancel` from inside the synthesiser, so a barge-in
    lands exactly where a user's would: between synthesis and playback.
    """
    turn = Turn()
    turn.cancel = cancel = cancel or threading.Event()
    real_deps = H._brain_deps          # the app's own factory, unread

    class Belt:
        _last_images: list = []
        _last_confirmation_offer = False
        _last_rearm_retry = False

        def _set_user_turn(self, gen):
            pass

    a = H.Assistant.__new__(H.Assistant)
    a._tools = Belt()
    a._gen = 1
    a._history = []
    a._history_lock = threading.Lock()
    a._history_epoch = 0
    a._turn_spoke = False
    a._turn_injected = ""
    a._models_ready = threading.Event()
    a._models_ready.set()
    a._recently_spoken = []
    a._last_spoken = None
    a._handsfree = False
    a._followup_until = 0.0
    a._failures_reported = set()
    a._conversation_for = lambda text: [
        {"role": "system", "content": "the system prompt"},
        {"role": "user", "content": text}]
    a._save_history = lambda: None
    if tool_entry is not None:
        a._tool_result_entry = tool_entry
    a._set = lambda gen, state: turn.states.append(state)
    a._is_closed = lambda: False
    os.makedirs(str(H.STATE_DIR), exist_ok=True)

    def synthesise(text, wav_path):
        if tts_exc is not None:
            raise tts_exc
        turn.said.append(text)
        if cancel_after is not None and len(turn.said) >= cancel_after:
            cancel.set()
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 800)

    def play(path, playback_cancel):
        if play_exc is not None:
            raise play_exc
        turn.played.append(turn.said[-1] if turn.said else "")

    monkeypatch.setattr(H, "tts_to_wav", synthesise)
    monkeypatch.setattr(H, "_audio", type("Audio", (), {
        "play_wav": staticmethod(play),
        "gpu_footprint_mb": staticmethod(lambda: {}),
        "yield_to_llm_verdict": staticmethod(
            lambda *a, **kw: {"yield": False}),
    }))
    monkeypatch.setattr(H, "notify", lambda text: turn.popups.append(text))
    monkeypatch.setitem(H.SETTINGS, "streaming_tts", streaming)

    def deps():
        d = real_deps()
        if guard is not None:
            d["guard"] = guard

        def open_url(request, timeout=None):
            turn.requests += 1
            return opener(request) if callable(opener) else opener
        d["urlopen"] = open_url
        return d

    monkeypatch.setattr(H, "_brain_deps", deps)
    H._BRAIN_STATE["tools_supported"] = True
    H.Assistant._brain_turn(a, "what time is it", 1, cancel)
    turn.assistant = a
    return turn


def _refusing_guard():
    raise RuntimeError("Ollama is configured on a non-loopback host")


def _reply(*sentences):
    return lambda req: _Response([_chunk(s) for s in sentences])


def _single(content):
    return lambda req: _Response(body=json.dumps(
        {"message": {"content": content}}).encode("utf-8"))


# ------------------------------------------------- the survey: what is said

@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")
class TestTheLineTheUserHears:
    """One test per failure mode, pinning the sentence verbatim.

    These are not paraphrases. A turn that fails is the moment the user most
    needs the app to say something exact and actionable, and the wording is
    the whole product surface — so each line here is the measured one, and a
    change to it has to be a deliberate edit to this file.
    """

    def test_a_guarded_endpoint_is_refused_before_anything_is_sent(self, H, monkeypatch):
        """The privacy guard runs before the request, so the refusal is total."""
        turn = drive(H, monkeypatch, _raises(AssertionError("a request was sent!")),
                     guard=_refusing_guard)
        assert turn.requests == 0, "the guard let a request through"
        assert turn.heard == (
            "Sorry, my brain is offline. RuntimeError: Ollama is configured on "
            "a non-loopback host"), turn.heard

    def test_a_refused_connection_names_the_server_and_the_fix(self, H, monkeypatch):
        """The common failure: Ollama is not running. It must say so, and say
        what to do about it, in one sentence."""
        turn = drive(H, monkeypatch, _refused)
        assert turn.heard == (
            "Sorry, my brain is offline. RuntimeError: cannot reach Ollama at "
            "http://127.0.0.1:11434 ([Errno 111] Connection refused). Start it "
            "with: systemctl start ollama"), turn.heard

    def test_a_missing_model_names_the_pull_command(self, H, monkeypatch):
        """A 404 is the second common failure and the easiest to fix.

        Both arms: the streaming arm only learned to append the pull command
        on 2026-09-27, and a test that drove only the default path could not
        have told the two apart — which is exactly how the non-streaming arm
        kept saying a thing the user could not act on for a release.
        """
        for streaming in (True, False):
            turn = drive(H, monkeypatch, _raises(_http_error(
                404, "model 'x' not found, try pulling it first")),
                streaming=streaming)
            assert turn.heard == (
                f"Sorry, my brain is offline. RuntimeError: Ollama error 404: "
                f"model 'x' not found, try pulling it first — run: ollama "
                f"pull {H.OLLAMA_MODEL}"), f"streaming={streaming}: {turn.heard}"

    def test_any_other_http_error_is_spoken_with_its_code(self, H, monkeypatch):
        for streaming in (True, False):
            turn = drive(H, monkeypatch,
                         _raises(_http_error(500, "internal error")),
                         streaming=streaming)
            assert turn.heard == (
                "Sorry, my brain is offline. RuntimeError: Ollama error 500: "
                "internal error"), f"streaming={streaming}: {turn.heard}"

    def test_both_arms_speak_the_same_sentence(self, H, monkeypatch):
        """The arms were reconciled here, and the agreement is the guard.

        The streaming arm always read the cause out of a
        `f"{type(e).__name__}: {e}"` string; the non-streaming arm
        interpolated the exception alone, so the same refused connection spoke
        `RuntimeError: cannot reach Ollama at ...` on the default path and
        `cannot reach Ollama at ...` on the other. The class is the difference
        between "the brain refused" and "the network broke", and one arm
        threw it away — so a `streaming_tts` toggle silently changed what a
        failure sounded like.

        Both arms are driven here rather than one, because a test that only
        pinned the default path would have passed on the original tree: the
        divergence was invisible from either side alone.
        """
        streamed = drive(H, monkeypatch, _refused, streaming=True)
        blocked = drive(H, monkeypatch, _refused, streaming=False)
        assert streamed.heard.startswith(
            "Sorry, my brain is offline. RuntimeError: cannot reach Ollama at "), \
            streamed.heard
        assert blocked.heard == streamed.heard, (
            "the two arms drifted apart again:\n"
            f"  streaming      : {streamed.heard}\n"
            f"  non-streaming  : {blocked.heard}")

    def test_a_read_that_dies_mid_stream_is_named_by_its_own_type(self, H, monkeypatch):
        """An IncompleteRead is not a RuntimeError, and the worker catches
        everything, so the cause reaches the user intact — in both arms, which
        now read the cause the same way."""
        for streaming in (True, False):
            turn = drive(H, monkeypatch, _raises(
                http.client.IncompleteRead(b"partial")), streaming=streaming)
            assert turn.heard == (
                "Sorry, my brain is offline. IncompleteRead: "
                "IncompleteRead(7 bytes read)"), \
                f"streaming={streaming}: {turn.heard}"

    def test_an_exception_that_is_not_an_exception_claims_an_empty_answer(
            self, H, monkeypatch):
        """MEASURED: the one line that does not tell the truth.

        `_run_stream` catches `Exception`. A `BaseException` — which is what
        an interrupted read or a shutdown signal actually is — skips it, kills
        the worker with `turn.result` still None, and the turn reports the
        empty-answer apology. The user is told the model returned nothing when
        in fact the request died, and nothing anywhere says what it was.
        """
        # The worker dies unhandled. `threading.excepthook` is where a
        # production thread's death is observable, so the test watches it
        # there rather than tolerating pytest's escalation at teardown.
        died: list[BaseException] = []
        real_hook = threading.excepthook

        def watch(args):
            died.append(args.exc_value)
        threading.excepthook = watch
        try:
            turn = drive(H, monkeypatch, lambda req: _never_leaves(
                KeyboardInterrupt("interrupted inside the read loop")))
        finally:
            threading.excepthook = real_hook

        assert [type(e).__name__ for e in died] == ["KeyboardInterrupt"], \
            "the worker did not die on the exception that skipped its handler"
        assert turn.heard == "Sorry, my brain gave me an empty answer.", \
            turn.heard
        assert turn.popups == [], "the real cause was reported nowhere"

    def test_an_empty_reply_is_silent(self, H, monkeypatch):
        """MEASURED: not an apology — nothing at all, in either arm.

        A model that returns no content and asks for no tool is a real answer
        shape (a tool-less model on a tool-shaped question). The turn logs a
        warning, appends nothing, and the user is left in silence with the
        bubble still thinking. Pinning the silence is the point: it is the
        shape a fix has to be measured against, and until there is one this is
        the contract.
        """
        for streaming in (True, False):
            opener = (_reply("") if streaming else _single(""))
            turn = drive(H, monkeypatch, opener, streaming=streaming)
            assert turn.heard == "", (
                f"streaming={streaming} spoke: {turn.heard!r}")
            assert turn.states == [], (
                "the bubble never showed it was answering")
            assert turn.history == [{"role": "user",
                                     "content": "what time is it"}], turn.history

    def test_a_stream_that_dies_part_way_says_it_cut_off_not_that_it_is_offline(
            self, H, monkeypatch):
        """MEASURED 2026-09-27, FIXED 2026-09-28. Kept in the survey because the
        measurement is the point: the two sentences used to contradict each
        other, out loud, in that order.

        Sentences already queued are spoken as they arrive, so by the time the
        failure lands the user has the answer in their ear. The error branch
        then spoke the full "my brain is offline <cause>" anyway, which is not
        a better apology — it is a false statement about a brain that was
        demonstrably working moments earlier. The turn now says what happened
        to it instead. `TestTheStreamThatDiedHalfway` drives the same failure
        and covers the other half, where nothing was spoken and the full
        apology with its cause is still the right thing to say.
        """
        def half(req):
            class Partial:
                def __enter__(self):
                    return self

                def __exit__(self, *e):
                    return False

                def __iter__(self):
                    yield _chunk("The time is ")
                    yield _chunk("half past four. ")
                    raise urllib.error.URLError("connection reset by peer")
            return Partial()

        turn = drive(H, monkeypatch, half)
        assert turn.played == [
            "The time is half past four.",
            "Sorry — my brain cut off there.",
        ], turn.played
        assert turn.history == [], (
            "a turn that died half-way must leave neither half in the next "
            f"prompt: {turn.history}")

    def test_a_model_without_tools_is_retried_and_still_answers(self, H, monkeypatch):
        """The 400 that is not a failure: tools are dropped and the turn works."""
        calls = {"n": 0}

        def refuse_then_answer(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(400, "this model does not support tools")
            return _Response([_chunk("Half past four.")])

        turn = drive(H, monkeypatch, refuse_then_answer)
        assert turn.heard == "Half past four.", turn.heard
        assert turn.requests == 2, "the retry never happened"
        assert H._BRAIN_STATE["tools_supported"] is False, (
            "the refusal was not remembered, so every turn pays for it again")

    def test_a_speech_model_that_cannot_load_is_silent_and_says_so_once(
            self, H, monkeypatch):
        """No voice: nothing is spoken, and a popup is the only word on it."""
        for streaming in (True, False):
            opener = (_reply("A whole reply.") if streaming
                      else _single("A whole reply."))
            turn = drive(H, monkeypatch, opener, streaming=streaming,
                         tts_exc=FileNotFoundError("no piper voice"))
            assert turn.heard == "", turn.heard
            assert turn.spoke is False, "an unspoken reply was recorded as spoken"
            assert turn.echo_armed == [], (
                f"streaming={streaming} would echo-filter the user against "
                f"{turn.echo_armed}")
            assert turn.popups and "could not speak" in turn.popups[0], \
                turn.popups

    def test_a_dead_playback_device_is_silent_and_says_so(self, H, monkeypatch):
        for streaming in (True, False):
            opener = (_reply("A whole reply.") if streaming
                      else _single("A whole reply."))
            turn = drive(H, monkeypatch, opener, streaming=streaming,
                         play_exc=OSError("no audio device"))
            assert turn.heard == "", turn.heard
            assert turn.spoke is False
            assert turn.popups and "could not speak" in turn.popups[0], \
                turn.popups

    def test_a_barge_in_stops_the_speaker_at_the_next_sentence(self, H, monkeypatch):
        """The working case, so the guards below are not bought by silence."""
        # tokens as a tokenizer emits them: the next sentence's first token
        # carries its leading space
        turn = drive(H, monkeypatch, _reply("One.", " Two.", " Three."),
                     cancel_after=2)
        assert turn.said == ["One.", "Two."], turn.said
        assert turn.played == ["One."], (
            "a sentence synthesized after the barge-in was played over the "
            f"user: {turn.played}")
        assert turn.spoke is True, "the sentence that did play was not recorded"


class TestTheTurnNobodyHeard:
    """The two places the bubble believes a reply it never delivered.

    Both were found by the survey above, both are measured, and both are the
    failure `_speak`'s own comment says it fixed — "spoke must mean the user
    HEARD it" — reachable one step further on than the fix looked.
    """

    def test_a_barge_in_during_synthesis_is_not_recorded_as_spoken(
            self, H, monkeypatch):
        """The non-streaming arm skips playback on cancel and then claims the
        line was spoken.

        `_speak` checks `cancel` between synthesis and playback, and the
        streaming arm `return`s there — which is why that arm is right. The
        non-streaming arm just does not call the speaker, falls out of the
        `try` normally, and runs the `else:` that marks the reply spoken. The
        three things that flag then read true of a line the user never heard:
        `_turn_spoke` (which opens the follow-up window on nothing), `_last_spoken`
        (which the restart note reads), and the echo filter (which drops the
        user's next words because they look like a repeat of a sentence that
        was never said).
        """
        turn = drive(H, monkeypatch, _single("A whole reply."),
                     streaming=False, cancel_after=1)
        assert turn.played == [], "nothing was played; the barge-in landed"
        assert turn.spoke is False, (
            "a reply that was never played is recorded as spoken")
        assert turn.assistant._last_spoken in (None, ""), (
            f"an unheard line is kept as the last thing said: "
            f"{turn.assistant._last_spoken!r}")
        assert turn.echo_armed == [], (
            f"the echo filter is armed on {turn.echo_armed}, which the user "
            "never heard and so will not repeat")
        assert turn.assistant._followup_until == 0.0, (
            "the follow-up window opened on a line that was never spoken")

    def test_the_streaming_arm_is_right_about_the_same_barge_in(
            self, H, monkeypatch):
        """The counterweight: the guard above is one arm, not a new rule that
        silences every cancelled reply."""
        turn = drive(H, monkeypatch, _reply("One.", " Two."), cancel_after=2)
        assert turn.said == ["One.", "Two."], turn.said
        assert turn.played == ["One."], turn.played
        assert turn.spoke is True
        assert turn.echo_armed == ["One."], turn.echo_armed

    @pytest.mark.parametrize("broken", ["tts", "playback"])
    @pytest.mark.parametrize("streaming", [True, False])
    def test_a_reply_that_was_never_heard_is_not_remembered_as_answered(
            self, H, monkeypatch, broken, streaming):
        """The history claims a sentence the user has no memory of hearing.

        `_speak` reports the failure and correctly leaves `_turn_spoke` false
        and the echo filter disarmed — but `_brain_turn` went on to publish the
        assistant turn regardless, because the publish was keyed on the model's
        answer rather than on whether the answer was spoken. So the next turn's
        prompt contained "the assistant said X" for a turn where the user heard
        nothing but a popup, and the model carried on from it: asking about a
        reply that was never made, or repeating a sentence the user had no
        reason to have heard.

        Both arms and both failure points, because the fix is a flag raised in
        `_speak` and the two arms raise it at two different sites: a guard
        tested on one arm would have left the other arm's site unwitnessed, and
        the streaming arm is the default turn.
        """
        exc = (FileNotFoundError("no piper voice") if broken == "tts"
               else OSError("no audio device"))
        kwargs = {"tts_exc" if broken == "tts" else "play_exc": exc}
        opener = (_reply("The kettle is on.") if streaming
                  else _single("The kettle is on."))
        turn = drive(H, monkeypatch, opener, streaming=streaming, **kwargs)
        assert turn.played == [], "the user heard nothing"
        assert turn.spoke is False
        assert turn.history == [{"role": "user", "content": "what time is it"}], (
            f"broken={broken} streaming={streaming}: the bubble remembers "
            f"answering a turn the user never heard answered: {turn.history}")


class _DiesMidStream:
    """A stream that delivers `content` and then the connection drops.

    The chunks are yielded one at a time rather than listed, because the whole
    point is that the producer raises AFTER the consumer has already taken a
    sentence — the interleaving is the defect, so a response that fails on the
    first `next()` would never reproduce it.
    """

    def __init__(self, content):
        self._pending = [_chunk(content)] if content is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._pending:
            return self._pending.pop(0)
        raise http.client.IncompleteRead(b"the stream stopped")


def _dies_after(content):
    return lambda req: _DiesMidStream(content)


class TestTheStreamThatDiedHalfway:
    """The last defect the turn-failure survey left open, fixed 2026-09-28.

    Streaming TTS speaks each sentence as it arrives, so a stream that dies
    part-way has ALREADY been heard by the time the producer's exception
    reaches the error branch. That branch spoke the full apology regardless,
    so the user was told the answer and then, immediately, told the brain was
    offline — two sentences that cannot both be true, said out loud in order.
    """

    def test_the_apology_does_not_contradict_what_was_just_said(self, H, monkeypatch):
        turn = drive(H, monkeypatch, _dies_after("Berlin is in Germany."),
                     streaming=True)
        said = list(turn.said)
        assert "Berlin is in Germany." in said, (
            f"the sentence that arrived was never spoken, so this is not the "
            f"case under test: {said}")
        assert not any("my brain is offline" in s for s in said), (
            f"the user was told the answer and then told the brain was "
            f"offline: {said}")
        assert any("cut off" in s for s in said), (
            f"the turn should say what actually happened to it: {said}")

    def test_a_stream_that_died_before_a_word_still_gets_the_full_apology(self, H, monkeypatch):
        """The other half, and the reason the fix is a branch and not a change.

        Nothing was said, so there is nothing to have cut off, and "my brain is
        offline" with the cause is the right thing to say. A fix that replaced
        the apology outright would take this away from the user.
        """
        turn = drive(H, monkeypatch, _dies_after(None), streaming=True)
        said = list(turn.said)
        assert not any("cut off" in s for s in said), (
            f"nothing was said, so nothing was cut off: {said}")
        assert any("my brain is offline" in s for s in said), (
            f"a stream that failed outright must still be reported with its "
            f"cause: {said}")

    def test_the_dropped_turn_publishes_neither_half(self, H, monkeypatch):
        """What history keeps, and why it is the coherent choice.

        The error branch returns before the publish block, so the question
        goes unpublished along with the partial answer. Recording the question
        alone is the shape that confuses the next turn: the model would be
        asked something it has no reply for. Pinned because the alternative —
        publishing the partial answer — looks like the obvious fix and is not.
        """
        turn = drive(H, monkeypatch, _dies_after("Berlin is in Germany."),
                     streaming=True)
        assert turn.history == [], (
            f"a turn that died half-way must not leave half of itself in the "
            f"prompt: {turn.history}")


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")
class TestAnUnheardAnswerLeavesACoherentHistory:
    """A turn whose answer could not be said keeps its question and drops the
    answer. The answer was dropped by ROLE — every assistant message — so a turn
    that used a tool lost the assistant message carrying `tool_calls` and kept
    the `role:tool` reply to it: a result with no call before it, published into
    every later request (the shape `_seal_tool_calls` exists to prevent)."""

    CALLS = [{"function": {"name": "set_timer", "arguments": {}}}]

    def _tool_turn(self, H, monkeypatch, **kw):
        rounds = iter([_Response([_chunk(tools=self.CALLS)]),
                       _Response([_chunk("Timer set.")])])
        return drive(
            H, monkeypatch, lambda req: next(rounds),
            tool_entry=lambda tc: {"role": "tool", "tool_name": "set_timer",
                                   "content": "timer set for 5 minutes"}, **kw)

    def test_a_tool_result_keeps_the_call_it_answers(self, H, monkeypatch):
        turn = self._tool_turn(H, monkeypatch, tts_exc=RuntimeError("no voice"))
        assert [m["role"] for m in turn.history] == ["user", "assistant", "tool"], \
            turn.history
        call = turn.history[1]
        assert call.get("tool_calls") == self.CALLS
        assert call["content"] == "", "text that rode with the call was never heard"
        # the invariant itself: no tool message without a call directly before it
        for i, m in enumerate(turn.history):
            if m["role"] == "tool":
                j = i - 1
                while j >= 0 and turn.history[j]["role"] == "tool":
                    j -= 1
                assert j >= 0 and turn.history[j].get("tool_calls"), (
                    f"orphaned tool result at {i}: {turn.history}")

    def test_the_spoken_answer_is_still_dropped(self, H, monkeypatch):
        turn = self._tool_turn(H, monkeypatch, tts_exc=RuntimeError("no voice"))
        assert not any(m["role"] == "assistant" and not m.get("tool_calls")
                       for m in turn.history), turn.history

    def test_a_turn_that_was_heard_publishes_everything(self, H, monkeypatch):
        turn = self._tool_turn(H, monkeypatch)
        assert [m["role"] for m in turn.history] == [
            "user", "assistant", "tool", "assistant"], turn.history
        assert turn.history[-1]["content"] == "Timer set."


class TestAFailureIsReportedOncePerCause:
    """`_report_once` notifies the person once per distinct cause. The cause was
    the exception's whole text, so one that named a scratch path or a number made
    every occurrence a NEW cause: a popup per reply, and a set of seen causes
    that grew for the life of the process."""

    def _assistant(self, H, monkeypatch):
        a = H.Assistant.__new__(H.Assistant)
        a._failures_reported = set()
        a._is_closed = lambda: False
        popups: list = []
        monkeypatch.setattr(H, "notify", popups.append)
        return a, popups

    def test_a_scratch_path_that_changes_every_time_is_one_cause(self, H, monkeypatch):
        a, popups = self._assistant(H, monkeypatch)
        for name in ("tmpk3j2x9", "tmpq1w2e3r", "tmp8h7g6f5"):
            a._report_speech_failure(FileNotFoundError(
                2, "No such file or directory",
                f"/home/u/.local/state/handsoff/{name}/tts.wav"))
        assert len(popups) == 1, popups

    def test_a_number_that_changes_every_time_is_one_cause(self, H, monkeypatch):
        a, popups = self._assistant(H, monkeypatch)
        for mib in (20, 24, 512):
            a._report_speech_failure(RuntimeError(
                f"CUDA out of memory. Tried to allocate {mib}.00 MiB"))
        assert len(popups) == 1, popups

    def test_different_causes_are_still_different(self, H, monkeypatch):
        a, popups = self._assistant(H, monkeypatch)
        a._report_speech_failure(FileNotFoundError(2, "No such file", "/a/b/c"))
        a._report_speech_failure(PermissionError(13, "Permission denied", "/a/b/c"))
        a._report_speech_failure(RuntimeError("device busy"))
        a._report_speech_failure(RuntimeError("device gone"))
        assert len(popups) == 4, popups

    def test_the_set_of_seen_causes_is_bounded(self, H, monkeypatch):
        a, popups = self._assistant(H, monkeypatch)
        for i in range(H._FAILURES_REPORTED_MAX + 40):
            a._report_once("k", RuntimeError(f"cause-{'x' * i}"), "m")
        assert len(a._failures_reported) == H._FAILURES_REPORTED_MAX
        assert len(popups) == H._FAILURES_REPORTED_MAX
