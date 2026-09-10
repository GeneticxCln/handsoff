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


def strip_thinking(text: str) -> str:
    """Clean model output before it is sent to speech."""
    value = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
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


def ollama_available(*, base: str, guard: Callable[[], None],
                     urlopen: Callable = urllib.request.urlopen) -> bool:
    try:
        guard()
        with urlopen(base + "/api/tags", timeout=3) as response:
            json.loads(response.read().decode("utf-8"))
        return True
    except Exception:
        return False


def ollama_chat(messages: list[dict], tools: list[dict] | None = None, *,
                base: str, model: str, num_ctx: int,
                guard: Callable[[], None], logger: logging.Logger,
                state: MutableMapping[str, bool] | None = None,
                urlopen: Callable = urllib.request.urlopen,
                keep_alive: str | None = None) -> dict:
    guard()
    payload = _defaults(base, model, num_ctx, tools, False, keep_alive)
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
    """Stream chat content into q, ending it with exactly one terminator."""
    guard()
    payload = _defaults(base, model, num_ctx, tools, True, keep_alive)
    payload["messages"] = messages
    req = urllib.request.Request(
        base + "/api/chat", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    buf = ""
    full = ""
    tool_calls: list[dict] = []
    fallback = False
    try:
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
                    if sentence and not sentence.startswith("<"):
                        q.put(sentence)
        tail = strip_thinking(buf).strip()
        if tail and not tail.startswith("<"):
            q.put(tail)
    except urllib.error.HTTPError as error:
        detail = _read_http_error(error)
        if error.code == 400 and tools and "tool" in detail.lower():
            logger.warning("model %s does not support tools; continuing without", model)
            fallback = True
            return ollama_chat_stream(
                messages, q, cancel, None, base=base, model=model,
                num_ctx=num_ctx, guard=guard, logger=logger, state=state,
                urlopen=urlopen, keep_alive=keep_alive)
        logger.error("streaming chat HTTP error: %s", error)
        raise RuntimeError(f"Ollama error {error.code}: {detail}") from None
    except Exception:
        logger.exception("streaming chat failed")
        raise
    finally:
        if not fallback:
            q.put(None)
    if tools and state is not None:
        state["tools_supported"] = True
    return {"tool_calls": tool_calls, "content": strip_thinking(full).strip()}
