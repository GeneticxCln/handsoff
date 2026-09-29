"""core/brain.py: the streaming contract, pinned where the user hears it.

`ollama_chat_stream` is the function that produces every sentence this app says
back. It runs on its own thread, hands words to a queue as they arrive, and the
speaker blocks on that queue until it sees a terminator nobody else can send —
so every property below is a property about a VOICE, not about a return value:

  * exactly one terminator, on every path, including the ones nobody thought
    about when the sentence was written (a refused connection, a server error,
    a guard that refuses the turn, a socket that dies mid-sentence, a retry
    that fails too);
  * no partial sentence is spoken before its full stop arrives, and no word the
    model sent is lost or spoken twice by the splitter;
  * a barge-in silences the unfinished tail and keeps what already played;
  * a model that refuses tools is retried ONCE without them, and that refusal
    is remembered rather than paid for again on every turn;
  * the two failure sentences name the server and the fix.

This is not the first suite to touch the function. `TestStreamingChat` in
`test_lifecycle.py` drives it against a real fake NDJSON server, and
`TestOllamaRefuses` in `test_fault_injection.py` drives the failure modes
through the app. What was missing was the function itself against a `urlopen`
it can be handed: the arms a live server does not reach in a passing run (the
tail flush, the tool-less retry, the HTTP 404, the 400-tool refusal) measured
uncovered at `core/brain.py` 86%, and the two defects below were found by
reading exactly those arms and then reproducing them.
"""
from __future__ import annotations

import io
import json
import logging
import queue
import threading
import time
import urllib.error

import pytest

from conftest import core_module

BASE = "http://127.0.0.1:11434"
MODEL = "test-model:7b"
LOG = logging.getLogger("handsoff.test.brain")

TOOLS = [{"type": "function", "function": {"name": "copy_text"}}]


@pytest.fixture()
def brain():
    """The sandboxed `core.brain`, resolved on first use (conftest.core_module)."""
    return core_module("brain")


class _Silent:
    """A logger that keeps a fold's one-time warning out of the test log."""

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


def _no_guard() -> None:
    """The default: this turn is allowed to happen."""


def _refuse_turn():
    raise RuntimeError("the guard refused this turn")


def _chunk(content: str = "", tool_calls=None) -> bytes:
    """One NDJSON line in the shape Ollama streams content in."""
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return (json.dumps({"message": message, "done": False}) + "\n").encode("utf-8")


def _sliced(text: str, count: int) -> list[str]:
    """`text` cut into `count` pieces that concatenate back to it exactly.

    Cut at ARBITRARY byte boundaries, which is the worst case for a sentence
    splitter: between a full stop and its space, inside a word, in the middle
    of a decimal. A stream arriving this way must still produce every word
    once, in order.
    """
    size = max(1, len(text) // count)
    return [text[i:i + size] for i in range(0, len(text), size)]


class _Stream:
    """A `urlopen` result the streamer can iterate, built from raw lines.

    `before(index)` runs just before each line is handed over, which is how a
    test stops the stream between two lines (to look at the queue while the
    model is still generating) or sets the cancel event mid-turn. `read()` is
    here so one double answers both: `ollama_chat` wants a single JSON
    document, `ollama_chat_stream` wants the lines.
    """

    def __init__(self, lines, before=None):
        self._lines = list(lines)
        self._before = before
        self.pulled: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for line in self._lines:
            if self._before is not None:
                self._before(len(self.pulled))
            self.pulled.append(line)
            yield line

    def read(self) -> bytes:
        return b"".join(self._lines)


def _response(*chunks, before=None):
    """A FRESH response object streaming these lines."""
    return _Stream(list(chunks), before=before)


def _ok_stream(*chunks, before=None):
    """A `urlopen` that streams these lines and then ends the response."""

    def urlopen(req, timeout=None):
        return _response(*chunks, before=before)

    return urlopen


def _json_body(obj):
    """A `urlopen` answering one JSON document — the `/api/ps` and `/api/tags` shape."""
    return _ok_stream(json.dumps(obj).encode("utf-8"))


def _http_error(code: int, detail: str, *, as_json: bool = True, reason: str = ""):
    """The exception a real server raises for a non-2xx status."""
    body = json.dumps({"error": detail}) if as_json else detail
    return urllib.error.HTTPError(BASE + "/api/chat", code,
                                  reason or detail, {},
                                  io.BytesIO(body.encode("utf-8")))


def _tool_refusal():
    """A FRESH 400 each call: a raised HTTPError's body is consumed once."""
    return _http_error(400, "this model does not support tools")


def _refused(req, timeout=None):
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def _mid_stream_disconnect():
    """A socket that dies between two chunks — what IncompleteRead really is."""
    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            yield _chunk("Half an answer. ")
            raise OSError(32, "Broken pipe")

        def read(self):
            return b""

    def urlopen(req, timeout=None):
        return _Boom()

    return urlopen


class _Once:
    """A FACTORY answer for `_Wire`: called for a fresh value on every request.

    Needed for a repeated failure — a raised `HTTPError` carries a body that
    is consumed on the first read, so answering with the same instance twice
    would silently test a different error the second time.
    """

    def __init__(self, make):
        self.make = make

    def __call__(self):
        return self.make()


def _answer(*chunks, before=None):
    """A `_Wire` answer that streams these lines."""
    return _Once(lambda: _response(*chunks, before=before))


class _Wire:
    """A `urlopen` double that records every payload it was handed.

    An answer is a response, an exception (raised), or an `_Once` factory.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.payloads: list = []

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data.decode("utf-8")))
        answer = self.answers[min(len(self.payloads) - 1, len(self.answers) - 1)]
        if isinstance(answer, _Once):
            answer = answer()
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _CountingCancel:
    """A stand-in for `threading.Event` that records how often it was asked.

    A `cancel` the recursion forgot to pass is otherwise invisible: a turn
    that was never interrupted behaves identically. This one counts the
    consultations the read loop made, which is the difference between a retry
    that can still be given up and one that cannot.
    """

    def __init__(self):
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return False


def _stream(brain, q, urlopen, *, cancel=None, tools=None, state=None,
            guard=_no_guard, keep_alive=None, messages=None):
    """Drive the real streamer with an injected `urlopen`."""
    return brain.ollama_chat_stream(
        messages if messages is not None
        else [{"role": "user", "content": "hi"}],
        q, cancel, tools, base=BASE, model=MODEL, num_ctx=8192, guard=guard,
        logger=LOG, state=state, urlopen=urlopen, keep_alive=keep_alive)


def _chat(brain, urlopen, *, tools=None, state=None, keep_alive=None,
          messages=None):
    """Drive the non-streaming call, which shares the fold and the readers."""
    return brain.ollama_chat(
        messages if messages is not None
        else [{"role": "user", "content": "hi"}],
        tools, base=BASE, model=MODEL, num_ctx=8192, guard=_no_guard,
        logger=LOG, state=state, urlopen=urlopen, keep_alive=keep_alive)


def _spoken(q) -> list:
    """Everything the queue carries except the terminator, in order."""
    return [item for item in list(q.queue) if item is not None]


def _normalised(text: str) -> str:
    """Whitespace collapsed — the splitter owns the gaps between sentences.

    What has to survive is the WORDS and their order. Whether a sentence ends
    with the space that followed its full stop in the byte stream is the
    splitter's business and cannot be pinned from outside it.
    """
    return " ".join(text.split())


# --------------------------------------------------------------- one terminator
# Every way this function can fail, measured one at a time. The table is the
# point: "exactly one terminator" was a docstring claim, and a claim only holds
# for the failures its author imagined. The `tools-refused-forever` row is the
# one nobody imagines — the retry itself fails, inside a `finally` that belongs
# to the frame which recursed.
_FAILURE_MODES = {
    "refused": {"urlopen": lambda: _refused, "raises": True},
    "http-500": {"urlopen": lambda: _Wire(_http_error(
        500, "the model crashed", as_json=False, reason="Internal Server Error")),
        "raises": True},
    "mid-stream-disconnect": {"urlopen": _mid_stream_disconnect,
                              "raises": True},
    "guard-refused": {"urlopen": lambda: _ok_stream(_chunk("Never.")),
                      "guard": _refuse_turn, "raises": True},
    "cancelled-before-the-first-line": {"urlopen": lambda: _ok_stream(
        _chunk("Never spoken.")), "cancelled": True, "raises": False},
    "tools-refused-forever": {"urlopen": lambda: _Wire(_Once(_tool_refusal)),
                              "tools": True, "raises": True},
    "no-content-at-all": {"urlopen": lambda: _ok_stream(), "raises": False},
    "only-blank-lines": {"urlopen": lambda: _ok_stream(b"\n", b"  \n"),
                         "raises": False},
}


class TestTheQueueAlwaysEnds:
    """The speaker blocks until a terminator arrives, and only the producer
    can send one. A missing terminator hangs the voice with no apology and no
    recovery; a second one is the docstring's promise being false."""

    @pytest.mark.parametrize("mode", sorted(_FAILURE_MODES))
    def test_every_failure_mode_ends_the_queue_with_exactly_one_terminator(
            self, brain, mode):
        spec = _FAILURE_MODES[mode]
        cancel = threading.Event()
        if spec.get("cancelled"):
            cancel.set()
        q: queue.Queue = queue.Queue()
        kwargs = dict(cancel=cancel, tools=TOOLS if spec.get("tools") else None,
                      state={}, guard=spec.get("guard", _no_guard))
        if spec["raises"]:
            with pytest.raises((RuntimeError, OSError)):
                _stream(brain, q, spec["urlopen"](), **kwargs)
        else:
            _stream(brain, q, spec["urlopen"](), **kwargs)
        items = list(q.queue)
        assert items.count(None) == 1, (
            f"{mode}: the sentence queue must be ended exactly once, not "
            f"{items.count(None)} times ({items})")
        assert items[-1] is None, (
            f"{mode}: the terminator must be LAST, after everything spoken "
            f"({items})")

    def test_a_cancelled_turn_says_nothing_at_all(self, brain):
        """Cancelled before the first line: not one word, and still ended.

        The barge-in is the user's, and the alternative — the tail of a turn
        they just interrupted being read aloud over them — is the defect the
        tail-flush guard exists for.
        """
        cancel = threading.Event()
        cancel.set()
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(_chunk("Never spoken.")),
                         cancel=cancel)
        assert list(q.queue) == [None], list(q.queue)
        assert result["content"] == ""

    def test_the_retry_after_a_tool_refusal_does_not_end_the_queue_twice(
            self, brain):
        """The recursion must not put a terminator of its own.

        `fallback = True` is the whole mechanism: the inner call ends the
        queue, and the frame that recursed must not end it again. Today's
        speaker stops at the first terminator and ignores the second, so this
        is the docstring's promise rather than a user-visible failure — which
        is exactly why it wants a test: the day something counts terminators (a
        turn summary, a queue drained by two consumers) this is the line that
        has to be right.
        """
        wire = _Wire(_Once(_tool_refusal), _answer(_chunk("Fine without tools.")))
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, wire, tools=TOOLS, state={})
        assert list(q.queue) == ["Fine without tools.", None], list(q.queue)
        assert result["content"] == "Fine without tools."

    def test_a_turn_that_never_produced_a_word_still_ends_the_queue(self, brain):
        """An empty answer is a turn the voice has nothing to say about."""
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(_chunk(""), _chunk("")))
        assert list(q.queue) == [None], list(q.queue)
        assert result == {"tool_calls": [], "content": ""}


# ------------------------------------------------------------- the splitter
# The stream arrives in pieces that have nothing to do with sentences: "Sure."
# can land as three chunks, a decimal point is not a full stop, and a reply can
# end without one. What the user hears is the splitter's work, so the splitter
# is the part with no second chance — a dropped word is a sentence never said,
# and nothing in the log says so.

_LOSSLESS_CORPUS = (
    "One. Two. Three.",
    "Sure. Here is what I found.",
    "The answer is 3.14 and 42.",          # a decimal point is not a stop
    "Dr. Alvarez is in tomorrow.",        # nor is an initial
    "Wait… then we go.",                   # an ellipsis is
    "Version 2.0 shipped last week.",
    "No full stop at the end",             # and a reply may not have one
    "A question?\nAn answer!",
    "It's <5 minutes away and <3 to you.",
    "Café — s'il vous plaît — done.",
    "One sentence.Another without a space.",
    "First. Second. Third. Fourth. Fifth. Sixth. Seventh. Eighth. Ninth. Tenth.",
)


class TestTheSentenceSplitter:
    @pytest.mark.parametrize("reply", _LOSSLESS_CORPUS)
    def test_every_word_the_model_sent_is_spoken_exactly_once(self, brain, reply):
        """Nothing lost, nothing repeated, whatever the chunk boundaries are.

        The queue joined back together must EQUAL what the call returns as its
        content: that equality is the whole property. A splitter that dropped a
        chunk, spoke one twice, or flushed a tail it had already spoken breaks
        it — and the way it breaks is a user hearing a garbled answer with
        nothing in the journal. The stream is cut at arbitrary byte
        boundaries, which is the worst case and the only honest one.
        """
        q: queue.Queue = queue.Queue()
        chunks = [_chunk(piece) for piece in _sliced(reply, 7)]
        result = _stream(brain, q, _ok_stream(*chunks))
        spoken = _spoken(q)
        assert _normalised(" ".join(spoken)) == _normalised(reply), (
            f"the queue says {spoken}, the model said {reply!r}")
        assert _normalised(result["content"]) == _normalised(reply), (
            f"the turn's own content drifted from the stream: "
            f"{result['content']!r} vs {reply!r}")
        assert list(q.queue)[-1] is None

    _CHUNKING_CORPUS = _LOSSLESS_CORPUS + (
        "It's 18.5 degrees and 3.2 inches of rain.",
        "Visit example.com or a.b.c now.",
        "Really?! Yes... maybe. Okay!",
        "Price is $4.99. Tax is 0.5%.",
    )

    @pytest.mark.parametrize("reply", _CHUNKING_CORPUS)
    def test_the_sentences_do_not_depend_on_where_the_stream_was_cut(
            self, brain, reply):
        """The same reply, cut at EVERY position, is the same sentences.

        The stream arrives a token at a time and a tokenizer emits "." on its
        own ("It's 18", ".", "5 degrees"), so a full stop that is the last thing
        in the buffer cannot be told from a decimal point, a version number, a
        domain or the first half of "?!" / "...". The splitter used to take the
        end of the buffer as a boundary: the reply "It's 18.5 degrees" was
        spoken as "It's 18." and then "5 degrees", and which reply the user heard
        depended on the network. The word-conservation test above cannot see it
        (the words are all there); the SENTENCES are what differ.
        """
        def sentences(pieces):
            q: queue.Queue = queue.Queue()
            _stream(brain, q, _ok_stream(*[_chunk(p) for p in pieces]))
            return _spoken(q)

        whole = sentences([reply])
        diverged = [
            (i, sentences([reply[:i], reply[i:]]))
            for i in range(1, len(reply))
            if sentences([reply[:i], reply[i:]]) != whole]
        assert not diverged, (
            f"{reply!r} is {whole} whole, but cut in two it is {diverged[:3]}")
        # and one character at a time, the worst case a token stream can be
        assert sentences(list(reply)) == whole

    def test_a_decimal_split_across_tokens_is_spoken_whole(self, brain):
        q: queue.Queue = queue.Queue()
        _stream(brain, q, _ok_stream(*[
            _chunk(p) for p in ("It's 18", ".", "5 degrees today", ".")]))
        assert _spoken(q) == ["It's 18.5 degrees today."], _spoken(q)

    @staticmethod
    def _dies_after(*pieces, cancel=None):
        class _Dies:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                for piece in pieces:
                    yield _chunk(piece)
                if cancel is not None:
                    cancel.set()          # the barge-in lands as the socket dies
                raise OSError(104, "Connection reset by peer")
        return lambda req, timeout=None: _Dies()

    def test_a_stream_that_dies_after_a_finished_sentence_still_says_it(self, brain):
        """The splitter holds a full stop that is the last thing in the buffer
        until the next token's leading space settles it — so a stream that dies
        right there used to lose a sentence the model had finished, and the
        user heard the apology instead of the answer."""
        q: queue.Queue = queue.Queue()
        with pytest.raises(OSError):
            _stream(brain, q, self._dies_after("Berlin is in Germany", "."))
        assert list(q.queue) == ["Berlin is in Germany.", None], list(q.queue)

    def test_a_stream_that_dies_mid_sentence_says_no_fragment(self, brain):
        q: queue.Queue = queue.Queue()
        with pytest.raises(OSError):
            _stream(brain, q, self._dies_after("Berlin is in Germany. It has", " three"))
        assert list(q.queue) == ["Berlin is in Germany.", None], list(q.queue)

    def test_a_barge_in_then_a_dying_stream_says_nothing_more(self, brain):
        q: queue.Queue = queue.Queue()
        cancel = threading.Event()
        with pytest.raises(OSError):
            _stream(brain, q, self._dies_after("Berlin is in Germany", ".",
                                               cancel=cancel), cancel=cancel)
        assert list(q.queue) == [None], list(q.queue)

    def test_a_reply_that_ends_on_a_full_stop_is_still_spoken(self, brain):
        """The terminator that ends the stream has no whitespace after it; the
        end-of-stream flush is what says it."""
        q: queue.Queue = queue.Queue()
        _stream(brain, q, _ok_stream(_chunk("Done"), _chunk(".")))
        assert list(q.queue) == ["Done.", None], list(q.queue)

    def test_a_sentence_holding_several_chunks_is_spoken_once_it_is_whole(
            self, brain):
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("Sure. I "), _chunk("found three "), _chunk("things.")))
        assert _spoken(q) == ["Sure.", "I found three things."], _spoken(q)
        assert result["content"] == "Sure. I found three things."

    def test_no_partial_sentence_is_spoken_before_its_terminator_arrives(
            self, brain):
        """The first four words must not be read out on their own.

        The alternative is the failure streaming exists to avoid: every answer
        beginning with a fragment, then the rest arriving as a second
        utterance. Observed through a real thread and a real queue with the
        stream held between two lines, so what is asserted is the queue's
        state while the model is still generating — not the finished answer.
        """
        gate, reached = threading.Event(), threading.Event()

        def hold(index):
            if index == 1:                    # the streamer is asking for line 1
                reached.set()
                assert gate.wait(5), "the test never released the stream"

        q: queue.Queue = queue.Queue()
        box: dict = {}
        urlopen = _ok_stream(_chunk("Hello"), _chunk(" there."), before=hold)

        def run():
            box["result"] = _stream(brain, q, urlopen)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        assert reached.wait(5), "the streamer never asked for the second line"
        assert q.empty(), (
            "half a sentence was queued before its terminator arrived: %s"
            % list(q.queue))
        gate.set()
        worker.join(10)
        assert not worker.is_alive(), "the streamer is still running"
        assert list(q.queue) == ["Hello there.", None], list(q.queue)
        assert box["result"]["content"] == "Hello there."

    def test_the_unfinished_tail_is_spoken_when_the_turn_was_not_cancelled(
            self, brain):
        """A model that stops mid-sentence still gets its last words said.

        The tail flush is the only thing between a truncated stream and an
        answer that ends mid-clause; it is also the line a barge-in guard has
        to be able to switch off, so it is pinned in both directions — the
        next test is the other direction, on the SAME stream.
        """
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(_chunk("One. "),
                                              _chunk("and then some more")))
        assert list(q.queue) == ["One.", "and then some more", None], \
            list(q.queue)
        assert result["content"] == "One. and then some more"

    def test_a_barge_in_swallows_the_unfinished_tail_and_keeps_what_played(
            self, brain):
        """The SAME two lines, cancelled between them.

        After a barge-in the tail was queued anyway and read out over the user
        who was still speaking. What already played stays — those sentences
        are the answer the user had begun hearing — and the turn's own result
        stops where the voice did, so a reply the user never heard is not
        recorded as said.
        """
        cancel = threading.Event()
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("One. "), _chunk("and then some more"),
            before=lambda i: i == 1 and cancel.set()), cancel=cancel)
        assert list(q.queue) == ["One.", None], list(q.queue)
        assert result["content"] == "One.", (
            "the turn recorded text the voice never said")

    def test_a_thinking_block_never_reaches_speech_but_the_answer_does(
            self, brain):
        """Reasoning is dropped; the sentences around it are not.

        The filter is deliberately narrow — a reply that merely STARTS with
        '<' ("<3", "it's <5 minutes away") is legitimate and must survive — so
        both directions are pinned on one stream rather than trusting the
        pattern to be "obviously" right.
        """
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("<think>weighing the options</think>"),
            _chunk("Here is the answer. "),
            _chunk("<think>more thoughts</think>"),
            _chunk("And the rest.")))
        assert _spoken(q) == ["Here is the answer.", "And the rest."], _spoken(q)
        assert result["content"] == "Here is the answer. And the rest."

    def test_a_leaked_control_token_loses_only_its_own_line(self, brain):
        """The token is the leak. The sentence it was glued to is the answer.

        The filter dropped the WHOLE sentence a token started, so a stream
        whose first chunk carried `<|im_start|>assistant\\n` lost every word up
        to the next full stop: the reply started one sentence late, a hole
        exactly the size of the first thing the model says. Measured
        2026-09-27, by this file.
        """
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("<|im_start|>assistant\n"),
            _chunk("The reminder is set for 7. "),
            _chunk("<think>x</think>"),
            _chunk("It is <5 minutes away.")))
        assert _spoken(q) == ["The reminder is set for 7.",
                              "It is <5 minutes away."], _spoken(q)
        # The returned content is the RAW join, unfiltered, and deliberately
        # so: the filter is a SPEECH filter, and the content is what the turn
        # records. `test_regression.py` pins `strip_thinking` on it; the
        # control-token family is not stripped there and has not been — pinned
        # here so a future change to it is a decision rather than a surprise.
        assert result["content"].endswith(
            "The reminder is set for 7. It is <5 minutes away."), result

    def test_a_bare_control_token_is_still_dropped(self, brain):
        """The case the filter was written for: a line that is only a token
        has nothing to say, and saying it would read the markup aloud. This is
        the shape `test_regression.py` already pins for the classifier; here
        it is pinned for what the SPEAKER does with the verdict.

        `<think>` is deliberately NOT in the corpus. An unclosed think block is
        removed by `strip_thinking` together with everything after it, so the
        sentence it opened goes too — a different rule with a different
        reason, and the right one: there is no way to know where the model's
        reasoning stops, so nothing after it is known to be an answer.
        """
        for only_a_token in ("<|im_start|>", "<|im_end|>", "<tool_calls>",
                             "</tool_calls>", "[TOOL_CALLS] junk"):
            q: queue.Queue = queue.Queue()
            _stream(brain, q, _ok_stream(_chunk(only_a_token + "\n"),
                                         _chunk("The answer. "),
                                         _chunk("And the rest.")))
            assert _spoken(q) == ["The answer.", "And the rest."], \
                (only_a_token, _spoken(q))

    def test_blank_and_malformed_lines_are_skipped_not_fatal(self, brain):
        """A line that is not a chunk is a transport artefact, not an error.

        Ollama's NDJSON carries blank keep-alive lines and a proxy in front of
        it can add a heartbeat of its own. Refusing the turn over one makes a
        working brain offline; speaking the raw bytes is worse. Skipped is the
        only third option, and the sentences around them must still arrive.
        """
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            b"\n", b"   \n", b"not json at all\n", b'{"message": {"content"',
            b"\xff\xfe not utf-8\n",
            _chunk("Still here. "), b"\n", _chunk("Still talking.")))
        assert _spoken(q) == ["Still here.", "Still talking."], _spoken(q)
        assert result["content"] == "Still here. Still talking."
        assert list(q.queue)[-1] is None

    def test_a_chunk_with_no_message_key_is_skipped(self, brain):
        """Ollama's keep-alive frames carry no `message` at all. Reaching for
        one that is not there is an AttributeError on a live stream."""
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            b'{"done": true}\n', b'{"message": null}\n', _chunk("Still here.")))
        assert _spoken(q) == ["Still here."], _spoken(q)
        assert result["content"] == "Still here."


class TestTheStreamingThinkFilter:
    """The streaming strip is STATEFUL where `strip_thinking` is not: the
    opener and closer of a think block land in different fragments, so the
    per-sentence strip saw plain text in every fragment between them and spoke
    the reasoning. `core.brain._speech_fragment` carries the in-think state
    across fragments; these tests pin the state machine at its edges."""

    def test_a_multisentence_think_block_is_dropped_across_fragments(
            self, brain):
        """The leak, exactly as it streamed: the opener arrives with the first
        reasoning sentence and the closer lands mid-sentence several fragments
        later, so the stateless per-fragment strip let the reasoning's tail
        through glued to the answer — 'so the answer is 5</think>The answer
        is 5.' was spoken. Measured 2026-09-28."""
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("<think>Let me work this out. First, 2 + 2. "),
            _chunk("so the answer is 5</think>The answer is 5. "),
            _chunk("Anything else?")))
        assert _spoken(q) == ["The answer is 5.", "Anything else?"], _spoken(q)
        # the turn's own content goes through the whole-reply strip, which
        # still removes the (complete) block: speech and record agree
        assert result["content"] == "The answer is 5. Anything else?"

    def test_a_single_sentence_think_block_is_still_dropped(self, brain):
        """Opener and closer in ONE fragment — the case the old per-sentence
        strip handled, and the state machine must not regress."""
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(_chunk("<think>hmm</think>So: 5.")))
        assert _spoken(q) == ["So: 5."], _spoken(q)
        assert result["content"] == "So: 5."

    def test_a_reply_without_think_blocks_is_untouched(self, brain):
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(_chunk("Plain. "),
                                              _chunk("Answer.")))
        assert _spoken(q) == ["Plain.", "Answer."], _spoken(q)
        assert result["content"] == "Plain. Answer."

    def test_a_stray_closer_without_an_opener_opens_and_closes_nothing(
            self, brain):
        """A '</think>' with no opener in sight is the model misbehaving, not
        a signal: nothing after it may be dropped (the state must not OPEN),
        and no sentence after it may be lost either. The words kept are the
        ones `strip_thinking` leaves on the blocking path — the two arms do
        not disagree about a reply neither of them can interpret."""
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("so the answer is 5</think>The answer is 5. "),
            _chunk("Next.")))
        assert _spoken(q) == ["so the answer is 5</think>The answer is 5.",
                              "Next."], _spoken(q)
        assert result["content"] == \
            "so the answer is 5</think>The answer is 5. Next."

    def test_a_stream_ending_inside_a_think_block_says_nothing_of_it(
            self, brain):
        """The tail flush is the leak's second door: the opener was dropped
        with an earlier fragment, so the stateless strip saw a tail with no
        markup in it and queued the reasoning as speech. Under the state
        machine the tail is still inside the block, and inside the block
        nothing is speakable."""
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("<think>Still deciding. "),
            _chunk("nearly there. "),
            _chunk("almost")))
        assert _spoken(q) == [], _spoken(q)
        assert result["content"] == ""


class TestTheStreamDeadline:
    """Finding 8 of the 2026-09-28 audit: every socket read waits at most
    300s, but a server that never finishes never trips a per-read bound —
    the reply as a whole now has a wall-clock budget (STREAM_DEADLINE_S),
    checked between chunks."""

    def test_a_stream_past_the_deadline_is_given_up_with_a_named_error(
            self, brain, monkeypatch):
        monkeypatch.setattr(brain, "STREAM_DEADLINE_S", 0.05)
        q: queue.Queue = queue.Queue()

        def drip(index):
            time.sleep(0.08)

        with pytest.raises(RuntimeError) as ei:
            _stream(brain, q, _ok_stream(_chunk("One. "), _chunk("Two. "),
                                         before=drip))
        assert "exceeded" in str(ei.value), str(ei.value)
        assert list(q.queue) == [None], (
            f"the abandoned turn must still end the queue: {list(q.queue)}")

    def test_a_fast_stream_is_not_touched_by_the_budget(self, brain):
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(_chunk("One. Two. ")))
        assert _spoken(q) == ["One.", "Two."], _spoken(q)
        assert result["content"] == "One. Two."


class TestTheToolCalls:
    def test_tool_calls_from_several_chunks_are_collected_in_order(self, brain):
        """`extend`, not assignment. A model may emit several calls in one
        turn, and dropping all but the last turns a two-tool turn into a
        one-tool turn that then answers half the question."""
        calls = [{"id": f"call_{i}", "function": {"name": "f", "arguments": {}}}
                 for i in range(3)]
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk(tool_calls=[calls[0]]),
            _chunk("Working on it. ", tool_calls=[calls[1]]),
            _chunk(tool_calls=[calls[2]]), _chunk("Done.")),
            tools=TOOLS, state={})
        assert [c["id"] for c in result["tool_calls"]] == \
            ["call_0", "call_1", "call_2"], result["tool_calls"]
        assert _spoken(q) == ["Working on it.", "Done."], _spoken(q)

    def test_a_chunk_with_a_tool_call_and_no_content_says_nothing(self, brain):
        """The first chunk of a tool-calling turn is usually empty. It must
        reach the tool loop and NOT the voice, or the assistant says nothing
        at all before calling a tool."""
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, _ok_stream(
            _chunk("", tool_calls=[{"id": "call_1",
                                    "function": {"name": "copy_text"}}]),
            _chunk("Copied it.")), tools=TOOLS, state={})
        assert result["tool_calls"][0]["function"]["name"] == "copy_text"
        assert _spoken(q) == ["Copied it."], _spoken(q)

    def test_a_turn_that_reports_tools_support_reports_it_only_on_success(
            self, brain):
        """`state` is the caller's memory of whether THIS model takes tools.

        It is written on a turn that carried tools, and on nothing else: a
        tool-less turn proved nothing either way, and a turn that never reached
        the model proved less than nothing.
        """
        state: dict = {}
        _stream(brain, queue.Queue(), _ok_stream(_chunk("Done.")), tools=TOOLS,
                state=state)
        assert state == {"tools_supported": True}, state

        no_tools: dict = {}
        _stream(brain, queue.Queue(), _ok_stream(_chunk("Done.")),
                tools=None, state=no_tools)
        assert no_tools == {}, (
            f"a tool-less turn wrote {no_tools}, and the caller reads this "
            "back to decide whether to send tools next turn")

    def test_a_turn_that_never_reached_the_model_records_nothing(self, brain):
        """The guard refusing the turn is the case that must record nothing:
        no request was made, so nothing was learned about the model.

        The contrast is deliberate. A turn cancelled AFTER the server accepted
        the request does record support, and correctly so: the 400 for an
        unsupported tools payload comes from the status line, before any
        content, so an accepted request really is the evidence.
        """
        state: dict = {}
        q: queue.Queue = queue.Queue()
        with pytest.raises(RuntimeError):
            _stream(brain, q, _ok_stream(_chunk("Never.")), guard=_refuse_turn,
                    tools=TOOLS, state=state)
        assert state == {}, state
        assert list(q.queue) == [None], list(q.queue)


class TestTheToolLessModel:
    """Ollama answers HTTP 400 for a tools payload a model cannot take. The
    turn has to survive it, and — the part that was wrong — the refusal has to
    be REMEMBERED: `state["tools_supported"]` was only ever set to True, so a
    tool-less model paid the same failed round trip on every single turn."""

    def test_a_model_that_refuses_tools_is_retried_once_without_them(self, brain):
        wire = _Wire(_Once(_tool_refusal), _answer(_chunk("Fine without tools.")))
        q: queue.Queue = queue.Queue()
        result = _stream(brain, q, wire, tools=TOOLS, state={})
        assert len(wire.payloads) == 2, (
            "expected one refused request and one retry, got "
            f"{len(wire.payloads)}")
        assert "tools" in wire.payloads[0], (
            "the first request must be the one that carries tools")
        assert "tools" not in wire.payloads[1], (
            "the retry sent the tools again — the payload the server had "
            "just refused")
        assert _spoken(q) == ["Fine without tools."], _spoken(q)
        assert result["tool_calls"] == []

    def test_the_refusal_is_recorded_and_the_successful_retry_does_not_undo_it(
            self, brain):
        """The invariant, and the one a later edit breaks first.

        The retry succeeds and carries no tools, so the `if tools and state is
        not None` line at the end of the frame does not run — which is what
        keeps the refusal. Set it True there and every later turn re-sends
        tools to a model that has just said it cannot take them, on every turn
        of every conversation, for the rest of the session.
        """
        wire = _Wire(_Once(_tool_refusal), _answer(_chunk("Fine without tools.")))
        state: dict = {}
        _stream(brain, queue.Queue(), wire, tools=TOOLS, state=state)
        assert state == {"tools_supported": False}, (
            f"after a tool refusal the caller's memory reads {state}")

    def test_a_model_that_refuses_tools_forever_asks_twice_and_then_gives_up(
            self, brain):
        """The retry carries no tools, so a second refusal cannot recurse.

        Without that, an always-refusing model recurses until Python's stack
        limit — one open request per level — and the turn dies as a
        RecursionError instead of naming the model that could not answer.
        """
        wire = _Wire(_Once(_tool_refusal))
        q: queue.Queue = queue.Queue()
        with pytest.raises(RuntimeError) as ei:
            _stream(brain, q, wire, tools=TOOLS, state={})
        assert len(wire.payloads) == 2, (
            f"an always-refusing model was asked {len(wire.payloads)} times")
        assert "does not support tools" in str(ei.value)
        assert list(q.queue) == [None], (
            f"the abandoned retry left the queue unended: {list(q.queue)}")

    def test_the_retry_carries_the_turns_own_parameters(self, brain):
        """Every argument the caller passed has to reach the retry too.

        `keep_alive` is the knob the idle release later takes back, so losing
        it here would change the memory policy for every tool-less turn.
        `guard` is the generation check that abandons a turn the user has
        moved on from — a retry that dropped it would keep a superseded turn
        alive on the wire — and `cancel` is what stops it again. All three are
        wired by hand at the recursive call site, which is why a counting
        stand-in for the cancel is the only way to see one go missing: a turn
        that was never interrupted behaves identically either way.
        """
        seen: list = []

        def urlopen(req, timeout=None):
            seen.append(json.loads(req.data.decode("utf-8")))
            if len(seen) == 1:
                raise _tool_refusal()
            return _response(_chunk("Done. "), _chunk("Really done."))

        guarded: list = []

        def guard():
            guarded.append(1)

        cancel = _CountingCancel()
        _stream(brain, queue.Queue(), urlopen, tools=TOOLS, guard=guard,
                cancel=cancel, keep_alive="90s")
        assert len(guarded) == 2, "the retry did not go through the caller's guard"
        assert seen[1]["keep_alive"] == "90s", (
            f"the retry asked for keep_alive={seen[1]['keep_alive']!r}")
        assert cancel.checks == 3, (
            f"the read loop consulted the cancel {cancel.checks} times — two "
            "lines plus the tail-flush guard. Zero is what a retry that "
            "dropped it looks like, and a retry that cannot be given up is "
            "the barge-in it was supposed to inherit")

    def test_the_warning_names_the_model_that_cannot_take_tools(self, brain,
                                                                caplog):
        wire = _Wire(_Once(_tool_refusal), _answer(_chunk("Done.")))
        with caplog.at_level(logging.WARNING, logger="handsoff.test.brain"):
            _stream(brain, queue.Queue(), wire, tools=TOOLS, state={})
        messages = [r.getMessage() for r in caplog.records]
        assert any(MODEL in m and "tools" in m for m in messages), messages


# ------------------------------------------------------- what a failure says
# `_brain_turn` diagnoses a down brain from RuntimeError alone, and the string
# inside that RuntimeError is what the user HEARS. Both sentences below are
# the whole difference between "fix it in ten seconds" and "read the journal".

class TestTheFailureSpeaks:
    def test_a_refused_connection_names_the_server_and_how_to_start_it(self,
                                                                        brain):
        q: queue.Queue = queue.Queue()
        with pytest.raises(RuntimeError) as ei:
            _stream(brain, q, _refused)
        message = str(ei.value)
        assert BASE in message, message
        assert "systemctl start ollama" in message, (
            "the fix-it hint is missing: the user is told the brain is down "
            "and not how to bring it back")
        assert list(q.queue) == [None], list(q.queue)

    def test_a_refused_connection_is_named_in_the_journal_too(self, brain,
                                                               caplog):
        with caplog.at_level(logging.ERROR, logger="handsoff.test.brain"):
            with pytest.raises(RuntimeError):
                _stream(brain, queue.Queue(), _refused)
        assert any("cannot reach" in r.getMessage() for r in caplog.records), (
            [r.getMessage() for r in caplog.records])

    def test_a_missing_model_names_the_command_that_fixes_it(self, brain):
        """The DEFAULT turn path, since `streaming_tts` defaults on.

        Measured 2026-09-27: the non-streaming arm appended "— run: ollama pull
        <model>" to a 404 naming the model, and the streaming arm — the one
        every turn takes — did not. The user heard "Sorry, my brain is
        offline. RuntimeError: Ollama error 404: model 'x' not found, try
        pulling it first": the diagnosis, with no command, and the command
        only in the journal. Pointing `llm_model` at a tag the host does not
        have is a documented setting and an installer that could not pull is a
        documented path, so this is among the first errors a new user meets.
        """
        def urlopen(req, timeout=None):
            raise _http_error(404, f'model "{MODEL}" not found, try pulling it first')

        with pytest.raises(RuntimeError) as ei:
            _stream(brain, queue.Queue(), urlopen)
        assert f"run: ollama pull {MODEL}" in str(ei.value), (
            f"the streaming path does not name the fix: {ei.value}")

    def test_both_paths_say_the_same_thing_about_a_missing_model(self, brain):
        """The two arms are one function to the user and must not disagree.

        They are separate code, which is how they came to: the hint was added
        to one and not the other, and the other is the default.
        """
        def urlopen(req, timeout=None):
            raise _http_error(404, f'model "{MODEL}" not found')

        with pytest.raises(RuntimeError) as a:
            _stream(brain, queue.Queue(), urlopen)
        with pytest.raises(RuntimeError) as b:
            _chat(brain, urlopen)
        assert str(a.value) == str(b.value), (str(a.value), str(b.value))

    def test_a_404_that_does_not_mention_the_model_gets_no_pull_command(self,
                                                                       brain):
        """The hint is for a model that is not there, not for every 404.

        A 404 from a proxy or a wrong port would otherwise be answered with
        "run: ollama pull", which sends the user to fetch a model they already
        have in order to fix a routing problem.
        """
        def urlopen(req, timeout=None):
            raise _http_error(404, "no such endpoint")

        with pytest.raises(RuntimeError) as ei:
            _stream(brain, queue.Queue(), urlopen)
        assert "ollama pull" not in str(ei.value), str(ei.value)
        assert "no such endpoint" in str(ei.value), str(ei.value)

    def test_a_non_json_error_body_still_produces_a_readable_sentence(self,
                                                                     brain):
        """A reverse proxy in front of Ollama answers HTML. The detail then
        comes from the status line, and the user must still be told something
        an adult can read rather than a JSON decoder's complaint."""
        def urlopen(req, timeout=None):
            raise _http_error(502, "<html>Bad Gateway</html>", as_json=False,
                              reason="Bad Gateway")

        with pytest.raises(RuntimeError) as ei:
            _stream(brain, queue.Queue(), urlopen)
        message = str(ei.value)
        assert "502" in message, message
        assert "Bad Gateway" in message, message
        assert "json" not in message.lower(), (
            f"the user is being read a JSON decoder error: {message}")

    def test_a_failure_mid_stream_is_logged_with_its_traceback_and_re_raised(
            self, brain, caplog):
        """Not swallowed, and not reported as an empty answer.

        The `except Exception` arm is the last one the function has: an
        IncompleteRead, a socket timeout, an OOM-killed Ollama. Swallowing it
        ends the turn as "my brain gave me an empty answer" with nothing in
        the log; re-raising it without the traceback leaves the user with an
        apology and the reader with no cause.
        """
        q: queue.Queue = queue.Queue()
        with caplog.at_level(logging.ERROR, logger="handsoff.test.brain"):
            with pytest.raises(OSError):
                _stream(brain, q, _mid_stream_disconnect())
        assert _spoken(q) == ["Half an answer."], (
            "the lines that DID arrive must survive the failure — the user has "
            "already heard them")
        assert any(r.exc_info for r in caplog.records), (
            "the failure was logged without a traceback: %s"
            % [(r.getMessage(), r.exc_info) for r in caplog.records])
        assert list(q.queue)[-1] is None


class TestAnErrorInsideTheStream:
    """Ollama reports a failure that happens AFTER it sent the 200 (the runner
    died, the model ran out of memory) as a line of the stream — `{"error":
    "..."}` — not as an HTTP status. Nothing looked for it: the line has no
    `message`, so it was skipped, the reply ended as if the model had finished,
    and the turn was recorded and spoken as complete (measured 2026-09-29: a
    reply cut at "And then" was said and kept; an error before the first word
    came back as an empty answer with its cause discarded)."""

    ERROR = b'{"error": "llama runner process has terminated: exit status 2"}\n'

    def test_an_error_after_some_words_is_raised_not_swallowed(self, brain):
        q: queue.Queue = queue.Queue()
        with pytest.raises(RuntimeError) as ei:
            _stream(brain, q, _ok_stream(_chunk("The answer is 42. And then "),
                                         self.ERROR))
        assert "llama runner process has terminated" in str(ei.value)
        assert _spoken(q) == ["The answer is 42."], (
            "what arrived before the failure has been heard already, and the "
            "half sentence after it must not be spoken as if it were finished")
        assert list(q.queue)[-1] is None, "exactly one terminator, still"

    def test_an_error_before_the_first_word_names_its_cause(self, brain):
        q: queue.Queue = queue.Queue()
        with pytest.raises(RuntimeError) as ei:
            _stream(brain, q, _ok_stream(
                b'{"error": "model requires more system memory than is '
                b'available"}\n'))
        assert "more system memory" in str(ei.value)
        assert list(q.queue) == [None]

    def test_lines_that_are_not_objects_are_noise_not_a_crash(self, brain):
        q: queue.Queue = queue.Queue()
        out = _stream(brain, q, _ok_stream(b"null\n", b"[1, 2]\n", b'"text"\n',
                                           b"7\n", _chunk("Fine. ")))
        assert _spoken(q) == ["Fine."]
        assert out["content"] == "Fine."

    def test_an_empty_error_field_is_not_an_error(self, brain):
        """A chunk that carries `"error": ""` or null alongside a message."""
        q: queue.Queue = queue.Queue()
        line = (json.dumps({"message": {"role": "assistant", "content": "Ok. "},
                            "error": ""}) + "\n").encode("utf-8")
        assert _stream(brain, q, _ok_stream(line))["content"] == "Ok."


class TestTheRequestItSends:
    """What goes on the wire. A `stream` flag flipped, or a tools key that
    appears when there are no tools, changes the answer without changing
    anything the tests above can see."""

    def test_the_streaming_request_asks_for_a_stream_and_the_other_does_not(
            self, brain):
        streaming = _Wire(_answer(_chunk("Done.")))
        _stream(brain, queue.Queue(), streaming)
        assert streaming.payloads[0]["stream"] is True
        blocked = _Wire(_answer(b'{"message": {"content": "Done."}}'))
        _chat(brain, blocked)
        assert blocked.payloads[0]["stream"] is False, (
            "a blocking request that asks for a stream gets NDJSON back, and "
            "json.loads of the whole body is a JSONDecodeError")

    def test_no_empty_tools_array_is_ever_sent(self, brain):
        """An empty list is falsy, so the key is left out — which is what
        keeps a turn with no permitted tools from looking to the server like a
        turn that has them."""
        without = _Wire(_answer(_chunk("Done.")))
        _stream(brain, queue.Queue(), without, tools=[])
        assert "tools" not in without.payloads[0], sorted(without.payloads[0])
        with_tools = _Wire(_answer(_chunk("Done.")))
        _stream(brain, queue.Queue(), with_tools, tools=TOOLS)
        assert with_tools.payloads[0]["tools"] == TOOLS

    def test_the_options_and_the_think_flag_are_what_the_model_is_told(
            self, brain, monkeypatch):
        monkeypatch.delenv("HANDSOFF_KEEP_ALIVE", raising=False)
        wire = _Wire(_answer(_chunk("Done.")))
        _stream(brain, queue.Queue(), wire)
        payload = wire.payloads[0]
        assert payload["options"]["temperature"] == 0.3
        assert payload["options"]["num_ctx"] == 8192
        assert payload["think"] is False, (
            "think=True streams the reasoning, and the filter has to strip it "
            "after the fact instead of never asking for it")
        assert payload["model"] == MODEL
        assert payload["keep_alive"] == "1h"

    def test_keep_alive_can_be_overridden_per_call(self, brain):
        """The idle release weighs what the turn asked to keep, so a caller's
        explicit value has to win over the environment default — on both arms,
        since they each build their own payload."""
        wire = _Wire(_answer(_chunk("Done.")))
        _stream(brain, queue.Queue(), wire, keep_alive="5m")
        assert wire.payloads[0]["keep_alive"] == "5m"
        blocked = _Wire(_answer(b'{"message": {"content": "ok"}}'))
        _chat(brain, blocked, keep_alive="5m")
        assert blocked.payloads[0]["keep_alive"] == "5m"


class TestTheSystemFirstFold:
    """Ollama 0.32 refuses the WHOLE request with HTTP 500 ("system message
    must be at the beginning") when a system message follows the first —
    measured live against qwen3.8:27b, which 500s where gemma4:latest does
    not, so the breakage reads as "broken brain on one model".

    The one place every request passes through enforces the role rule instead
    of trusting each caller. These are the arms that decide what it does with
    a list no caller is supposed to build.
    """

    def test_a_list_of_nothing_but_system_messages_becomes_one_and_a_user(
            self, brain):
        """The `for…else` fallthrough: every message was a system message.

        Handing three system messages to a renderer that allows one leading
        system message is the 500 this function exists to prevent, and the
        user turn is where the model can actually read them.
        """
        folded = brain._messages_system_first(
            [{"role": "system", "content": "MAIN"},
             {"role": "system", "content": "Facts you remember"},
             {"role": "system", "content": "Live hardware note"}],
            _Silent())
        assert [m["role"] for m in folded] == ["system", "user"], folded
        assert "Facts you remember" in folded[1]["content"]
        assert "Live hardware note" in folded[1]["content"]
        assert "MAIN" in folded[0]["content"]

    def test_a_note_with_no_user_turn_to_fold_into_is_made_one(self, brain):
        """Mid-tool-loop there can be no user message after the note, so the
        content needs a home of its own — the branch that APPENDS a user turn
        rather than merging into one, and the one that would otherwise drop
        the note on the floor."""
        folded = brain._messages_system_first(
            [{"role": "system", "content": "MAIN"},
             {"role": "tool", "tool_name": "t", "content": "result"},
             {"role": "system", "content": "Live hardware note"}],
            _Silent())
        assert [m["role"] for m in folded] == ["system", "tool", "user"], folded
        assert "Live hardware note" in folded[-1]["content"]
        assert folded[1]["content"] == "result", (
            "the tool result was disturbed by the fold")

    def test_a_list_with_no_leading_system_message_gets_none_added(self, brain):
        """`if not head: return kept` — the fold adds a user turn, not a
        system one. Inventing a leading system message here would put the
        assistant's own prompt ahead of a conversation that deliberately had
        none."""
        folded = brain._messages_system_first(
            [{"role": "user", "content": "hi"},
             {"role": "system", "content": "Facts"}], _Silent())
        assert [m["role"] for m in folded] == ["user"], folded
        assert folded[0]["content"] == "Facts\n\nhi"

    @pytest.mark.parametrize("messages,expected_roles", [
        ([{"role": "user", "content": "hi"},
          {"role": "system", "content": ""}], ["user"]),
        ([{"role": "system", "content": "MAIN"},
          {"role": "user", "content": "hi"},
          {"role": "system", "content": ""}], ["system", "user"]),
    ])
    def test_a_tail_note_with_no_content_is_dropped_rather_than_kept(
            self, brain, messages, expected_roles):
        """An empty system message is still a system message in a position the
        renderer refuses, and there is nothing in it for the model to read.

        It used to be returned untouched — the one list this function handed
        back that its own docstring forbids. Measured 2026-09-27: a turn whose
        conversation carried an empty re-injected note reached the server and
        came back "Ollama error 500: system message must be at the beginning",
        the whole turn lost to a message that said nothing.
        """
        folded = brain._messages_system_first(messages, _Silent())
        assert [m["role"] for m in folded] == expected_roles, folded
        assert not any(m.get("role") == "system" for m in folded[1:]), folded

    @pytest.mark.parametrize("messages", [
        [{"role": "user", "content": "hi"}],
        [{"role": "system", "content": "MAIN"}, {"role": "user", "content": "hi"}],
        [{"role": "user", "content": "hi"}, {"role": "system", "content": "note"}],
        [{"role": "system", "content": "MAIN"}, {"role": "user", "content": "hi"},
         {"role": "system", "content": "note"}],
        [{"role": "system", "content": "MAIN"}, {"role": "user", "content": "hi"},
         {"role": "system", "content": "note"}, {"role": "user", "content": "q"}],
        [{"role": "system", "content": "MAIN"}, {"role": "user", "content": "hi"},
         {"role": "system", "content": ""}],
        [{"role": "user", "content": "hi"}, {"role": "system", "content": ""}],
        [{"role": "system", "content": "A"}, {"role": "system", "content": "B"}],
        [{"role": "system", "content": "A"}, {"role": "system", "content": ""},
         {"role": "user", "content": "hi"}],
        [{"role": "system", "content": ""}, {"role": "user", "content": "hi"},
         {"role": "assistant", "content": "a"}, {"role": "user", "content": "q"}],
        [{"role": "user", "content": "hi"}, {"role": "tool", "content": "r"},
         {"role": "system", "content": "note"}],
    ])
    def test_a_folded_list_is_never_the_shape_that_gets_refused(self, brain,
                                                                messages):
        """The contract, as a property over the shapes a caller can build.

        In goes a list; out comes roles that satisfy the rule Ollama 0.32
        enforces — at most one system message, and only in first place. This
        is the claim the docstring makes, and it is checkable rather than
        arguable: a list the fold returns with a system message after the first
        is a list that 500s. Nothing the caller said may be lost on the way
        either, so the fold cannot be "fixed" by dropping what it dislikes.
        """
        folded = brain._messages_system_first(messages, _Silent())
        offenders = [i for i, m in enumerate(folded[1:], start=1)
                     if m.get("role") == "system"]
        assert not offenders, (
            f"{messages} came back as {folded} — a system message at "
            f"{offenders} is what Ollama answers with HTTP 500")
        assert [m["role"] for m in folded].count("system") <= 1, folded
        for m in messages:
            content = str(m.get("content") or "")
            if m.get("role") in ("user", "tool", "assistant") and content:
                assert any(content in str(f.get("content") or "")
                           for f in folded), (
                    f"{content!r} was lost by the fold: {folded}")

    def test_the_streaming_path_folds_the_same_way_the_blocking_one_does(
            self, brain):
        """Both arms call it, so a change to one that forgets the other is
        invisible until a turn happens to take the other path."""
        messages = [{"role": "system", "content": "MAIN"},
                    {"role": "user", "content": "hi"},
                    {"role": "system", "content": "Facts"},
                    {"role": "user", "content": "q"}]
        streamed = _Wire(_answer(_chunk("Fine.")))
        blocked = _Wire(_answer(b'{"message": {"content": "Fine."}}'))
        _stream(brain, queue.Queue(), streamed, messages=list(messages))
        _chat(brain, blocked, messages=list(messages))
        assert streamed.payloads[0]["messages"] == blocked.payloads[0]["messages"]
        assert [m["role"] for m in streamed.payloads[0]["messages"]] == \
            ["system", "user", "user"]


class TestTheReadinessProbe:
    """`ollama_available` is asked before every startup line and by the doctor.
    It is a boolean about a MACHINE, so the answer it can get wrong in the
    dangerous direction is "up"."""

    def test_a_probe_that_answered_reports_up(self, brain):
        wire = _json_body({"models": []})
        assert brain.ollama_available(base=BASE, guard=_no_guard,
                                      urlopen=wire) is True

    @pytest.mark.parametrize("broken", ["unreachable", "forbidden",
                                        "not-json", "unreadable", "guard-refused"])
    def test_a_probe_that_cannot_ask_reports_down_and_never_raises(self, brain,
                                                                   broken):
        """Five ways the question cannot be answered, one answer.

        False, never an exception: the caller is a startup line and a doctor
        section, and a raise there is a traceback on the desktop instead of
        the sentence saying the brain is offline. And False on a body that is
        not JSON — a wrong port served by something else answers 200 with HTML,
        and reading that as "up" gets a user a turn that then fails on every
        single utterance.
        """

        class _Unreadable:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise OSError("the connection dropped mid-read")

        def urlopen(url, timeout=None):
            if broken == "unreachable":
                raise urllib.error.URLError(
                    ConnectionRefusedError(111, "Connection refused"))
            if broken == "forbidden":
                raise urllib.error.HTTPError(url, 403, "Forbidden", {},
                                             io.BytesIO(b"{}"))
            if broken == "not-json":
                return _Stream([b"<html>a captive portal</html>"])
            return _Unreadable()

        guard = _refuse_turn if broken == "guard-refused" else _no_guard
        assert brain.ollama_available(base=BASE, guard=guard,
                                      urlopen=urlopen) is False, broken


class TestTheReadersDefend:
    """The two `/api` readers and the verdict they feed. All three answer a
    question about a machine that may be reporting something other than what
    the reader expects, and all three are asked on a timer rather than in
    front of the user — so a raise is a broken tick and a wrong number is a
    wrong policy decision nobody sees."""

    def test_a_size_nobody_could_read_is_unknown_rather_than_a_number(self,
                                                                     brain):
        """`None` is not `0.0`.

        Zero VRAM is a model that is genuinely all on the CPU; an unreadable
        size is missing telemetry, and the release decision says so
        differently. Collapsing them either evicts a model that is not on the
        card, or holds one that is.
        """
        for value in (None, "", "abc", {}, [], object(), True):
            assert brain._gib(value) is None, value
        assert brain._gib(0) == 0.0
        assert brain._gib(-5) == 0.0, "a negative size is not a model either"
        assert brain._gib("500") is None, (
            "500 bytes is not a model; believing it would price 0.0000005 GB")
        assert brain._gib(5_000_000_000) == pytest.approx(4.6566, rel=1e-4)
        assert brain._gib("5000000000") == pytest.approx(4.6566, rel=1e-4)

    def test_a_models_list_holding_something_else_still_finds_the_model(self,
                                                                      brain):
        """A list entry that is not a dict is skipped, not crashed on.

        `{"models": ["qwen3:8b", {...}]}` is what a server that changed its
        answer shape would send and what a half-parsed body can look like. The
        entry that IS a dict still has to be found: the alternative to skipping
        a bad entry is answering "nothing resident" about a model holding
        nine gigabytes of the card.
        """
        body = {"models": ["a bare string", None, 7, {
            "name": MODEL, "size": 8_000_000_000, "size_vram": 4_000_000_000}]}
        resident = brain.ollama_resident(base=BASE, model=MODEL,
                                         guard=_no_guard, logger=LOG,
                                         urlopen=_json_body(body))
        assert resident is not None and resident["loaded"] is True, resident
        assert resident["size_vram"] == pytest.approx(3.7253, rel=1e-3), resident
        size_mb = brain.ollama_model_size_mb(base=BASE, model=MODEL,
                                             guard=_no_guard, logger=LOG,
                                             urlopen=_json_body(body))
        assert size_mb is not None and size_mb > 0, size_mb

    def test_a_list_of_nothing_but_junk_is_not_a_measurement(self, brain):
        for body in ({"models": ["a", "b"]}, {"models": [None, None]}):
            resident = brain.ollama_resident(base=BASE, model=MODEL,
                                             guard=_no_guard, logger=LOG,
                                             urlopen=_json_body(body))
            assert resident == {"loaded": False, "size": None, "size_vram": None,
                                "expires_at": ""}, body
            assert brain.ollama_model_size_mb(
                base=BASE, model=MODEL, guard=_no_guard, logger=LOG,
                urlopen=_json_body(body)) is None, body

    def test_a_reload_nobody_could_read_is_free_rather_than_a_hold(self, brain):
        """A cost that cannot be read is zero, which RELEASES.

        That is the safe direction and it is deliberate: a parsing accident
        must never hold a model's memory for the rest of the session, and the
        exchange rate is the user's to set. `wait_s_per_gb` unreadable is
        pinned in `test_idle_release`; `reload_s` is the same shape one line
        below and was not measured at all.
        """
        resident = {"loaded": True, "size": 18.0, "size_vram": 9.7,
                    "expires_at": ""}
        for unreadable in ("soon", [], {}, object()):
            verdict = brain.ollama_release_verdict(
                resident, reload_s=unreadable, wait_s_per_gb=20)
            assert verdict["release"] is True, unreadable
            assert verdict["wait_per_gb"] == 0.0, (
                f"{unreadable!r} was read as a cost: {verdict}")
        unmeasured = brain.ollama_release_verdict(resident, reload_s=None,
                                                  wait_s_per_gb=20)
        assert unmeasured["release"] is True
        assert "not measured" in unmeasured["note"], unmeasured
        for unreadable in ("often", None, [], {}, object()):
            verdict = brain.ollama_release_verdict(
                resident, reload_s=218.9, wait_s_per_gb=unreadable)
            assert verdict["release"] is True, unreadable
            assert verdict["budget"] == 0.0, verdict


class TestTheBlockingChat:
    """`ollama_chat` is the non-default path (`streaming_tts` off) and the one
    the no-core fallback's own copy mirrors. It shares the fold and the
    readers, so what is left to pin is its own arms."""

    def test_a_successful_turn_with_tools_records_that_the_model_has_them(
            self, brain):
        state: dict = {}
        message = _chat(brain, _Wire(_answer(
            b'{"message": {"content": "ok"}}')), tools=TOOLS, state=state)
        assert message["content"] == "ok"
        assert state == {"tools_supported": True}, state

    def test_a_turn_without_tools_records_nothing(self, brain):
        state: dict = {}
        _chat(brain, _Wire(_answer(b'{"message": {"content": "ok"}}')),
              tools=None, state=state)
        assert state == {}, state

    def test_a_model_that_refuses_tools_is_retried_without_them(self, brain):
        wire = _Wire(_Once(_tool_refusal),
                     _answer(b'{"message": {"content": "ok"}}'))
        state: dict = {}
        message = _chat(brain, wire, tools=TOOLS, state=state)
        assert "tools" in wire.payloads[0]
        assert "tools" not in wire.payloads[1], (
            "the retry sent the payload the server had just refused")
        assert message["content"] == "ok"
        assert state == {"tools_supported": False}, (
            f"a tool-less model was re-probed on every turn: {state}")

    def test_an_empty_message_is_an_empty_dict_not_a_crash(self, brain):
        """`body.get("message") or {}` — a body carrying no message gives the
        caller an empty dict to ask about, not an AttributeError on None."""
        for body in (b"{}", b'{"message": null}', b'{"message": {}}'):
            assert _chat(brain, _Wire(_answer(body))) == {}, body

    def test_an_unreachable_server_is_named_with_its_fix(self, brain):
        with pytest.raises(RuntimeError) as ei:
            _chat(brain, _refused)
        assert f"cannot reach Ollama at {BASE}" in str(ei.value)
        assert "systemctl start ollama" in str(ei.value)

    def test_a_404_naming_the_model_says_so(self, brain):
        def urlopen(req, timeout=None):
            raise _http_error(404, f'model "{MODEL}" not found')

        with pytest.raises(RuntimeError) as ei:
            _chat(brain, urlopen)
        assert f"run: ollama pull {MODEL}" in str(ei.value), str(ei.value)
