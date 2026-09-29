"""Online lookups: a probed search router and a page reader.

Two capabilities, and a rule that shapes both: **a backend is described by what
this process has OBSERVED, never by what it is assumed to be.** A channel that
answers `ok` because its command exists (or because it is "always available") is
how a dead daemon gets reported as reachable — this tree has that bug in the
ydotool probe and does not want a second one here. So the router records the
outcome of every backend it uses and `doctor` reports that record, with `untried`
for a backend nothing has asked yet.

Why a module and not five more tools
------------------------------------
The tool surface is generated from each method's signature and the fixed prompt
is tight, so the four domains live behind ONE tool (`web_search`) selected by a
deterministic, keyword-based route instead of four schemas the model has to
choose between. The reader is the one genuinely new tool (`read_page`).

The pieces
----------
* **Backends** are keyless: `searxng` (local, opt-in), `ddg` (Lite scrape, the
  general fallback), `stackexchange`, `hn`, `github`, `wikipedia`. Each returns
  `Result`s and may raise; the router records and names the failure.
* **The route** (`_route`) is regexes over the query, not model judgment:
  an error/API/install shape starts at Stack Exchange, a repo shape at GitHub,
  a news shape at the general pair. The route is the ORDER of a fallback walk,
  so a failing primary degrades to the next backend instead of to nothing.
* **One in-flight request per backend**, admitted through the shared
  `core/registry.py` helper — the cap is not hand-rolled (`_has_room` is the
  only place a capacity exists). A refused request is a named skip, not a
  silent drop.
* **A TTL cache** keyed by source+limit+query, so a repeated question makes no
  second request to anybody.
* **The reader** fetches on THIS machine first (`local fetch`) and uses the
  hosted reader (`Jina Reader (third-party)`) only when the local fetch yields
  nothing usable — and the text it returns says which one served it, because
  the address of a page is the user's business. Loopback, link-local, private
  and `.local` targets are refused: an assistant that reads a URL on request
  must not be talked into reading the router's admin page or a cloud metadata
  endpoint into its transcript.

Host seams
----------
`configure()` takes RESOLVERS, not values: a callable is called on every use, so
the host can pass `lambda: _http_get` and keep a monkeypatched function or a
reloaded setting live (the alternative — binding the function object once —
makes every test seam and every settings reload silently stale).
"""
from __future__ import annotations

import html as _html
import inspect
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.parse
from html.parser import HTMLParser
from typing import NamedTuple

from core import registry as _registry

__all__ = [
    "BACKENDS", "Result", "cache_clear", "configure", "doctor_lines",
    "last_problem", "read_page", "read_results", "reader_note", "search",
    "search_note", "world_allowed",
]

# ----------------------------------------------------------------- limits
SEARCH_TIMEOUT = 6.0        # per backend; a search must not stall a turn
SEARCH_LIMIT = 4            # results kept per backend
READ_TIMEOUT = 8.0
MIN_LOCAL_TEXT = 200        # below this the local fetch is judged thin
MIN_LOCAL_BYTES = 2000      # ...but only a BIG thin page is a JavaScript shell
READ_MAX_CHARS = 40_000     # what `read_page` will hand back at most
TOOL_MAX_CHARS = 6_000      # what a tool puts into the model's context per page
READ_TOP_MAX = 3
CACHE_TTL = 300.0
SEARXNG_PROBE_TIMEOUT = 0.3     # localhost: connect-or-refuse, no external traffic
JINA_READER = "https://r.jina.ai/"   # the hosted reader, used only as a fallback
VIA_LOCAL = "local fetch"
VIA_JINA = "Jina Reader (third-party)"
UA = "Mozilla/5.0 (X11; Linux x86_64) handsoff"

# ----------------------------------------------------------------- host seams
_HTTP_GET = None            # callable(url, timeout) -> bytes, or a resolver
_HTTP_IS_FN = True          # decided by configure(): see `_takes_a_url`
_SEARXNG_URL = None         # resolver or plain str ("" disables the local backend)
_LOG = None
# The READER's seam: callable(url, timeout) -> (body, Location or ""), one
# request, redirects NOT followed. See `_read_fetch` for why that is a
# different question from `_HTTP_GET` above.
_HTTP_HOP = None
_HTTP_HOP_IS_FN = True
# The hosted reader's switch: a resolver returning bool, or None. None means
# "no host wired this", which is REFUSED rather than allowed — the fallback
# sends the user's target address to a third party, so the safe reading of an
# absent seam is "off". Set by `configure(hosted_reader=...)`.
_HOSTED_READER = None
# The proactive world-events fetch (the host's morning briefing and world
# warning tick) is called by the host DIRECTLY, not through the tool belt, so
# the `web_access` permission the tools check never saw it. The gate is a
# RESOLVER like every other seam — read per use, so a live settings save takes
# effect on the next poll — and ABSENT means ALLOW, because the feature
# predates the seam and a host that never wired it must keep behaving. A WIRED
# gate that raises or answers anything but a bool is the opposite polarity: a
# permission check that blew up must not open the door. See `world_allowed`.
_WORLD_GATE = None


def _warn(msg: str, *args) -> None:
    if _LOG is None:
        return
    try:
        _LOG.warning(msg, *args)
    except Exception:       # a logger must never break a lookup
        pass


def _resolve(value):
    """A callable is a RESOLVER (called per use); anything else is the value."""
    return value() if callable(value) else value


def _takes_a_url(fn) -> bool:
    """True when `fn` is the FETCH ITSELF rather than a zero-argument resolver.

    `configure(http_get=...)` accepts both, because both are natural to write and
    guessing wrong is silent: a resolver passed as a function is called with no
    address (a confusing TypeError at the first lookup), and a function treated
    as a resolver does nothing at all. The signature decides, once, here.
    """
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD,
                                p.VAR_POSITIONAL)]
    except (TypeError, ValueError):
        return True         # a C callable: assume the URL-passing form
    return bool(params)


def configure(http_get=None, searxng_url=None, logger=None,
              http_get_hop=None, hosted_reader=None, world_gate=None) -> None:
    """Inject the host's seams. See the module docstring: pass resolvers.

    `http_get_hop` is the READER's seam: one request that does NOT follow
    redirects, returning `(body, Location or "")`. It exists because
    `http_get` follows them inside the host's urlopen, and a redirect is a new
    address that was never checked — see `_read_fetch`. The reader refuses to
    read through `http_get` alone: a one-shot fetch follows the whole redirect
    chain unchecked, which is the hole the seam exists to close.

    `hosted_reader` is a RESOLVER for the `hosted_reader` setting: True when the
    user has switched the third-party reader on. It gates the `r.jina.ai`
    fallback and nothing else, and it is a resolver (not a bound bool) for the
    same reason `searxng_url` is: the setting can be changed on a live settings
    save, and binding the value once would leave the fallback answering for a
    switch the user has since turned off. Absent (None) means the fallback is
    REFUSED, because the shipped default is off and a host that never wired the
    seam must not silently inherit an on-switch.

    `world_gate` is a RESOLVER answering True when the PROACTIVE world-events
    fetch (briefing, world warnings) holds the same permission the tools check;
    the host wires the `web_access` permission here so a direct call cannot
    bypass it. Absent means allowed — see `_WORLD_GATE` for the polarity.
    """
    global _HTTP_GET, _HTTP_IS_FN, _SEARXNG_URL, _LOG, _HTTP_HOP, _HTTP_HOP_IS_FN
    global _HOSTED_READER, _WORLD_GATE
    if http_get is not None:
        _HTTP_GET = http_get
        _HTTP_IS_FN = _takes_a_url(http_get)
    if http_get_hop is not None:
        _HTTP_HOP = http_get_hop
        _HTTP_HOP_IS_FN = _takes_a_url(http_get_hop)
    if searxng_url is not None:
        _SEARXNG_URL = searxng_url
    if hosted_reader is not None:
        _HOSTED_READER = hosted_reader
    if world_gate is not None:
        _WORLD_GATE = world_gate
    if logger is not None:
        _LOG = logger


def world_allowed() -> bool:
    """True when the proactive world-events fetch may ask the network.

    The call the host makes outside the tool belt — `_world_events`, feeding
    the morning briefing and the world-warning tick — checks here before any
    network I/O, so it holds the same `web_access` permission the tools do.
    Three readings, all of which mean "no" unless the gate answers an exact
    True: no seam wired is ALLOW (the feature predates the seam, and a host
    that never wired it must not lose it — that is the one reading that goes
    the other way), a resolver answering anything that is not True, and a
    resolver that raises. The last two matter because this is a permission: a
    settings dict mid-reload must not be the difference between the user's
    machine asking the news and staying quiet, and anything uncertain is OFF.
    """
    if _WORLD_GATE is None:
        return True
    try:
        return _resolve(_WORLD_GATE) is True
    except Exception:
        _warn("the world gate could not be read; refusing the world-events "
              "fetch")
        return False


def _http(url: str, timeout: float = SEARCH_TIMEOUT) -> bytes:
    """The host's fetch. ONE network seam for search AND reading, so patching
    `handsoff._http_get` in a test covers the whole feature."""
    target = _HTTP_GET
    if target is None:
        raise RuntimeError("no http_get injected")
    if not _HTTP_IS_FN:
        target = _resolve(target)    # a resolver: re-read the host's function
    if target is None:
        raise RuntimeError("no http_get injected")
    return target(url, timeout)


MAX_REDIRECTS = 5           # a chain longer than this is refused, not followed
#: The read's WHOLE walk — every hop's fetch and DNS — on one wall clock.
#: Each socket waits at most READ_TIMEOUT, and six hops of eight seconds each
#: plus a resolver apiece is a minute the user waits through per page, times
#: `read_top` pages: the total is what needs the bound, not the single read.
READ_DEADLINE_S = 30.0


def _accepts_connect_to(fn) -> bool:
    """True when the injected hop fetch can be handed the checked addresses.

    A seam that takes `connect_to` (or `**kwargs`) can dial the address the
    policy approved; one that cannot resolves the name a second time, which is
    the rebinding window left open. Detected rather than required, so a partial
    install or a test double still fetches — and is TOLD what it is giving up.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return True
    return "connect_to" in params


def _hop(url: str, timeout: float, connect_to=None) -> tuple:
    """ONE request, redirects NOT followed: (body, Location or "").

    `connect_to` is the address LIST the policy approved for THIS url (`(ip,
    port)` pairs), passed straight through so the transport can dial exactly
    what was checked instead of resolving the name again.
    """
    target = _HTTP_HOP
    if target is None:
        return None, ""
    if not _HTTP_HOP_IS_FN:
        target = _resolve(target)
    if target is None:
        return None, ""
    if connect_to and _accepts_connect_to(target):
        return target(url, timeout, connect_to=connect_to)
    if connect_to:
        _warn("the injected hop fetch cannot be pinned to the checked address "
              "for %s: it resolves the name a second time, and a rebinding "
              "name can answer differently than it did for the check", url)
    body, location = target(url, timeout)
    return body, str(location or "")


def _read_fetch(url: str, timeout: float, connect_to=None) -> tuple:
    """(body, final_url, problem): the reader's fetch, redirects walked HERE.

    `_public_url` checks the address ONCE, and a redirect is a NEW address that
    was never checked — which is the whole hole: a model-supplied URL can pass
    every rule and then answer `302 Location: http://169.254.169.254/…`, or point
    at a LAN address, and the page lands in the transcript as though it were
    public. `_http` cannot help, because the host's urlopen follows the chain
    before this module sees anything.

    So the chain is walked one hop at a time and every hop goes back through
    `_public_url`: URL policy lives here, and a redirect is just another
    untrusted address. Relative Locations are resolved against the URL that
    sent them, which is what a browser does and what a server means.

    The second hole is the one no check on the NAME can close: the checked
    address and the fetched address used to be two separate resolutions of
    the same name, and a name server is free to answer them differently (DNS
    rebinding — public for the policy, 127.0.0.1 for the socket). So every hop
    carries the addresses `_public_target` approved for it into the fetch.

    A host without the hop seam gets a REFUSAL, not a fetch. The old fallback —
    one shot through `http_get` — followed the whole chain unchecked and
    resolved the name a second time, which is exactly the hole the seam closes,
    so the fallback was the hole wearing a warning and is gone (audit
    2026-09-28). `connect_to` is the address list the caller already checked
    for the FIRST hop; every later hop is checked here, as before. The walk
    keeps two further rules a one-shot fetch cannot express: a read that
    STARTS as https is never downgraded to http on the way, and the whole walk
    runs on one wall-clock budget (`READ_DEADLINE_S`).
    """
    if _HTTP_HOP is None:
        # refusal: read_no_hop_seam
        return b"", url, ("no redirect-checking fetch is wired, so redirects "
                          f"cannot be checked for {url} — refused rather "
                          "than followed unchecked")
    started_https = urllib.parse.urlsplit(url).scheme.casefold() == "https"
    current, pins = url, connect_to
    started = time.monotonic()
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() - started > READ_DEADLINE_S:
            # refusal: read_timed_out
            return b"", current, (f"{url} was still following redirects after "
                                  f"{READ_DEADLINE_S:.0f}s — the read is "
                                  "abandoned rather than continued")
        body, location = _hop(current, timeout, connect_to=pins)
        if body is None:                     # the seam vanished mid-walk
            # refusal: read_hop_seam_vanished
            return b"", current, ("the redirect-checking fetch vanished "
                                  f"mid-walk for {url} — refused rather "
                                  "than read unchecked")
        if not location:
            return body, current, ""
        try:
            target = urllib.parse.urljoin(current, location)
        except ValueError:
            # refusal: redirect_to_an_unusable_address
            return b"", current, (f"{current} redirected to {location!r}, which "
                                  f"is not a usable address")
        try:
            downgraded = (urllib.parse.urlsplit(target).scheme.casefold()
                          == "http")
        except ValueError:
            downgraded = False               # `_public_target` names it instead
        clean, pins, problem = _public_target(target)
        if problem:
            # refusal: redirect_to_a_refused_address
            return b"", current, (f"{current} redirected to {target}, which is "
                                  f"not fetched: {problem}")
        # Checked AFTER the address policy on purpose: a hop that is both a
        # downgrade and a private address is refused for the ADDRESS, which is
        # the older rule and the one the tests name.
        if started_https and downgraded:
            # refusal: redirect_downgrades_to_http
            return b"", current, (f"{current} redirected to the unencrypted "
                                  f"{target} — refused rather than downgraded")
        current = clean
    # refusal: too_many_redirects
    return b"", current, (f"{url} redirected more than {MAX_REDIRECTS} times — "
                          f"refused rather than followed")


def _searxng_url() -> str:
    return str(_resolve(_SEARXNG_URL) or "").strip().rstrip("/")


def _hosted_reader_enabled() -> bool:
    """True when the user has switched the third-party reader on.

    Three readings, all of which mean "no" unless something says otherwise:
    no seam wired at all (a partial install, or a test that patched only the
    plain fetch), a resolver that returns something that is not True, and a
    resolver that raises. The last one matters because this is a privacy
    switch: a settings object that is mid-reload, or a resolver that trips
    over a half-written value, must not be the difference between the user's
    address staying on this machine and going to a third party. Anything
    uncertain is OFF, and the caller says so in a sentence naming the switch.
    """
    if _HOSTED_READER is None:
        return False
    try:
        return _resolve(_HOSTED_READER) is True
    except Exception:
        _warn("hosted_reader switch could not be read; treating it as OFF")
        return False


# ----------------------------------------------------------------- results
class Result(NamedTuple):
    """One search hit. `url` is "" when the backend's payload carried none."""

    title: str
    snippet: str
    url: str
    backend: str


# ------------------------------------------------------------------ backends
# name -> human label used in failures, so a message never says a bare key.
BACKENDS = ("searxng", "ddg", "stackexchange", "hn", "github", "wikipedia")
_LABEL = {
    "searxng": "SearXNG",
    "ddg": "DuckDuckGo",
    "stackexchange": "Stack Exchange",
    "hn": "Hacker News",
    "github": "GitHub",
    "wikipedia": "Wikipedia",
}


def _clean(markup: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", markup or "")).strip()


def _unwrap(href: str) -> str:
    """A scraped href to a real URL: protocol-relative and DDG's redirector."""
    href = _html.unescape(str(href or "").strip())
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        try:
            query = urllib.parse.urlparse(href).query
            target = urllib.parse.parse_qs(query).get("uddg", [""])[0]
            if target:
                return target
        except Exception:
            return ""
    return href if re.match(r"^https?://", href, re.I) else ""


# DuckDuckGo Lite's markup: the class attribute is quoted with SINGLE quotes
# (`class='result-link'`) as of this writing, and the attributes are not in a
# fixed order. The parser that read only double quotes matched NOTHING on the
# live page and returned an empty list, which the tool reported as "no results"
# — so every web search quietly fell through to Wikipedia. Pinned by a fixture
# taken from the real response, and by one in the other quoting style.
_ANCHOR = re.compile(r"<a\b[^>]*>", re.I)
_HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
_RESULT_LINK = re.compile(r"class=[\"']result-link[\"']", re.I)
_RESULT_TITLE = re.compile(r"class=[\"']result-link[\"'][^>]*>(.*?)</a>", re.S | re.I)
_RESULT_SNIPPET = re.compile(r"class=[\"']result-snippet[\"'][^>]*>(.*?)</td>", re.S | re.I)
# DuckDuckGo's anti-bot page: an iframe/form posting to `anomaly.js` with
# `cc=botnet`. Named here rather than folded into the generic signatures so the
# failure says which engine turned us away and why.
_DDG_CHALLENGE = re.compile(r"anomaly\.js|challenge-form|cc=botnet", re.I)
# DuckDuckGo answers a query with no matches in a HEADING of its own
# ("<h2>No results.</h2>" — the shape the honest-empty fixture in
# `tests/test_web.py` is taken from). Matching the phrase anywhere in the page
# read a reshaped page whose SNIPPET text mentioned "no results" as an honest
# empty and hid the reshape; the marker is matched where the page puts it.
_DDG_EMPTY = re.compile(r"<h\d\b[^>]*>[^<]*\bno results\b", re.I)


def _result_hrefs(html_text: str) -> list:
    """Every result anchor's href, whatever the attribute order or quoting."""
    out = []
    for tag in _ANCHOR.findall(html_text):
        if not _RESULT_LINK.search(tag):
            continue
        match = _HREF.search(tag)
        out.append(_unwrap(match.group(1)) if match else "")
    return out


def ddg_search(query: str, limit: int = SEARCH_LIMIT) -> list:
    """DuckDuckGo Lite, scraped. The general fallback: keyless, no account.

    Three outcomes are told apart, because they are three different things:
    results, a page that says there are none, and a page that is not a result
    page at all. The third is the one that matters — DuckDuckGo answers a burst
    of queries from one address with a bot challenge (`anomaly.js`, `cc=botnet`,
    a `challenge-form`), and reporting THAT as "no results" is a lie the user
    cannot see through. It raises, so the router names it and doctor keeps it.
    """
    page = _http("https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query))
    text = page.decode("utf-8", "replace")
    if _DDG_CHALLENGE.search(text):
        label = _antibot(text)
        raise RuntimeError("DuckDuckGo served a bot challenge"
                           + (f" ({label[0]})" if label else "")
                           + " — try again later or run a local SearXNG")
    titles = _RESULT_TITLE.findall(text)
    if not titles:
        if _DDG_EMPTY.search(text):
            return []          # the site answering "nothing matched" — honest
        if "result-link" not in text:
            raise RuntimeError(
                f"unrecognised reply ({len(text)} bytes, no result markup) — "
                "the page may have changed shape")
    snippets = _RESULT_SNIPPET.findall(text)
    hrefs = _result_hrefs(text)
    out = []
    for i, title in enumerate(titles[:limit]):
        snippet = snippets[i] if i < len(snippets) else ""
        url = hrefs[i] if i < len(hrefs) else ""
        out.append(Result(_clean(title), _clean(snippet), url, "ddg"))
    return out


def wikipedia_search(query: str, limit: int = SEARCH_LIMIT) -> list:
    """The MediaWiki search API — stable facts, and a URL we can build exactly."""
    data = json.loads(_http(
        "https://en.wikipedia.org/w/api.php?action=query&list=search"
        "&format=json&srlimit=%d&srsearch=%s" % (limit, urllib.parse.quote(query))))
    out = []
    for hit in (data.get("query") or {}).get("search") or []:
        title = str(hit.get("title", ""))
        out.append(Result(title, _clean(str(hit.get("snippet", ""))),
                          "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
                              title.replace(" ", "_")), "wikipedia"))
    return out


def stackexchange_search(query: str, limit: int = SEARCH_LIMIT) -> list:
    """Stack Exchange: the backend for error text and API shapes.

    Reports no key and its own daily quota, which the caller records — a quota
    that is running out is visible BEFORE an empty answer is mistaken for
    "nothing exists".
    """
    url = ("https://api.stackexchange.com/2.3/search/advanced"
           "?order=desc&sort=relevance&site=stackoverflow&pagesize=%d&q=%s"
           % (limit, urllib.parse.quote(query)))
    data = json.loads(_http(url))
    if data.get("error_message"):
        raise RuntimeError(str(data["error_message"]))
    if data.get("backoff"):
        raise RuntimeError(f"rate limited (backoff {data['backoff']}s)")
    quota = ""
    if data.get("quota_max"):
        quota = f"quota {data.get('quota_remaining')}/{data['quota_max']}"
    _QUOTA["stackexchange"] = quota
    out = []
    for item in (data.get("items") or [])[:limit]:
        tags = ", ".join(item.get("tags") or [])
        score = item.get("score", 0)
        title = _clean(str(item.get("title", "")))
        out.append(Result(title, f"{tags} — score {score}", str(item.get("link", "")),
                          "stackexchange"))
    return out


def hn_search(query: str, limit: int = SEARCH_LIMIT) -> list:
    """HN via Algolia: keyless JSON, good for releases and discussion."""
    data = json.loads(_http(
        "https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=%d&query=%s"
        % (limit, urllib.parse.quote(query))))
    out = []
    for hit in (data.get("hits") or [])[:limit]:
        url = str(hit.get("url") or "")
        if not url:
            url = "https://news.ycombinator.com/item?id=" + str(hit.get("objectID", ""))
        snippet = _clean(str(hit.get("story_text") or "")) or (
            f"{hit.get('points', 0)} points, {hit.get('num_comments', 0)} comments")
        out.append(Result(_clean(str(hit.get("title", ""))), snippet, url, "hn"))
    return out


def github_search(query: str, limit: int = SEARCH_LIMIT) -> list:
    """Repository search. Unauthenticated: 10 requests a minute, no key."""
    data = json.loads(_http(
        "https://api.github.com/search/repositories?per_page=%d&q=%s"
        % (limit, urllib.parse.quote(query))))
    out = []
    for item in (data.get("items") or [])[:limit]:
        stars = item.get("stargazers_count", 0)
        out.append(Result(str(item.get("full_name", "")),
                          f"{item.get('description') or 'no description'} — "
                          f"{stars} stars",
                          str(item.get("html_url", "")), "github"))
    return out


def searxng_search(query: str, limit: int = SEARCH_LIMIT) -> list:
    """A local SearXNG, when one is running. Preferred for general queries: it
    aggregates the engines and the query never leaves this machine."""
    base = _searxng_url()
    if not base:
        raise RuntimeError("no SearXNG configured")
    url = (base + "/search?format=json&q=" + urllib.parse.quote(query))
    data = json.loads(_http(url, timeout=SEARCH_TIMEOUT))
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    out = []
    for item in (data.get("results") or [])[:limit]:
        out.append(Result(_clean(str(item.get("title", ""))),
                          _clean(str(item.get("content", ""))),
                          str(item.get("url", "")), "searxng"))
    return out


_SEARCH = {
    "searxng": searxng_search,
    "ddg": ddg_search,
    "stackexchange": stackexchange_search,
    "hn": hn_search,
    "github": github_search,
    "wikipedia": wikipedia_search,
}

# ------------------------------------------------------------------- routing
# The route is an ORDER, not a filter: every hit below is reachable, and the
# walk stops as soon as enough results exist. Regexes rather than a model
# judgment, because "which backend" must be testable and reproducible.
_TECH = re.compile(
    r"\b(error|traceback|exception|api|sdk|pip|install|cuda|gpu|driver|segfault|"
    r"docker|regex|import|traceback|bug|compile|library|version|package|"
    r"whisper|python|javascript|rust|sql|json|http|crash)\b", re.I)
_CODE = re.compile(
    r"\b(repo|repository|github|gitlab|changelog|release|framework|toolkit|"
    r"plugin|sdk|release notes)\b", re.I)
_NEWS = re.compile(
    r"\b(news|today|latest|current|now|breaking|price|score|weather|forecast|"
    r"stock|market|election)\b", re.I)
_ROUTES = (
    (_TECH, ("stackexchange", "hn", "github", "searxng", "ddg", "wikipedia")),
    (_CODE, ("github", "hn", "searxng", "ddg", "wikipedia")),
    (_NEWS, ("searxng", "ddg", "hn", "wikipedia")),
)
_GENERAL = ("searxng", "ddg", "wikipedia")


def _route(query: str) -> tuple:
    """The ordered backends for `query`. First match wins; general otherwise."""
    for pattern, order in _ROUTES:
        if pattern.search(query or ""):
            return order
    return _GENERAL


# ---------------------------------------------------------------- the record
# backend -> {"ok": bool, "at": float, "why": str}. Written whenever a backend
# is actually used, read by `search_note()`. Empty means "untried", which is
# what doctor says instead of inventing a healthy answer.
_SEEN: dict = {}
_QUOTA: dict = {}
_READER_SEEN: dict = {}     # "local"/"jina" -> {"ok", "at", "why"}
_RECORD_LOCK = threading.Lock()


def _record(store: dict, name: str, ok: bool, why: str = "") -> None:
    with _RECORD_LOCK:
        store[name] = {"ok": bool(ok), "why": why, "at": time.time()}


def failure_reasons() -> list:
    """The recorded reasons the search backends last failed, named.

    Consumers surface these verbatim: "offline?" was a GUESS, and the thing a
    user can act on is what actually happened ("a bot challenge"). Empty when
    nothing was recorded — a caller must then say so, not invent a cause.
    """
    with _RECORD_LOCK:
        return [f"{name}: {rec['why']}" for name, rec in _SEEN.items()
                if isinstance(rec, dict) and not rec.get("ok") and rec.get("why")]


# One in-flight request per backend, admitted by the shared registry helper —
# the project's rule is that no capacity is enforced by hand anywhere. A
# refusal here is a NAMED skip ("busy"), never a silent drop.
_CAPS = {name: _registry.BoundedRegistry(f"web-{name}", 1) for name in BACKENDS}


def _run(name: str, fn, query: str) -> list:
    """Run one backend under its cap, recording success OR the reason it failed."""
    reservation = _CAPS[name].reserve(key=name)
    if reservation is None:
        raise RuntimeError("busy — a request to this backend is already in flight")
    try:
        results = fn(query, SEARCH_LIMIT)
    except Exception as exc:
        _record(_SEEN, name, False, _reason(exc))
        _warn("web backend %s failed: %s", name, exc)
        raise
    finally:
        reservation.cancel()        # transient: the slot is free the moment we are
    _record(_SEEN, name, True)
    return results


def _reason(exc: Exception) -> str:
    """The shortest honest reason: a status if there is one, else what it SAID.

    The message matters for the cases with no status to report — a refused
    admission says "busy", a rate limit says "rate limited (backoff 30s)", a
    connection says "Connection refused". Falling back to the exception's type
    name for those would make every skip read `RuntimeError`, which is the same
    as saying nothing.
    """
    status = getattr(exc, "code", None)
    if status:
        return f"HTTP {status}"
    text = " ".join(str(exc).split())[:70]
    return text or type(exc).__name__


# ------------------------------------------------------------------- caching
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
# A long-running bubble sees a distinct query string per web_search call, so an
# uncapped cache grows without bound. 256 entries covers every realistic burst
# of repeat lookups; past that the OLDEST is dropped (a plain dict keeps
# insertion order, which is the eviction order).
_CACHE_MAX = 256


def cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _purge_locked(now: float) -> None:
    """Drop expired entries, then the oldest while over the cap. Locked."""
    for key in [k for k, (expires, _, _) in _CACHE.items() if expires <= now]:
        del _CACHE[key]
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))


def _cached(key: str):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None:
            return None
        expires, results, notes = hit
        if expires <= now:
            # DELETE on expiry, rather than leaving a dead entry behind: the
            # old shape returned None but kept the slot forever.
            del _CACHE[key]
            return None
        return results, notes


def _store(key: str, results: list, notes: list) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + CACHE_TTL, results, notes)
        _purge_locked(time.time())


# -------------------------------------------------------------------- search
def search(query: str, source: str = "auto", limit: int = SEARCH_LIMIT):
    """(results, notes) for `query`.

    `source` is a backend name or "auto" (the route). `notes` names every
    backend that was tried and did not deliver, so a caller can say WHY an
    answer is thin instead of reporting "no results" as if the web had none.
    Never raises: a source that cannot even be looked up comes back as a note.
    """
    query = str(query or "").strip()
    if not query:
        return [], ["nothing to search for"]
    try:
        limit = max(1, min(int(limit or SEARCH_LIMIT), 10))
    except (TypeError, ValueError):
        limit = SEARCH_LIMIT
    source = str(source or "auto").strip().lower()
    if source in ("", "auto", "any"):
        order = _route(query)
    elif source in _SEARCH:
        order = (source,)
    else:
        return [], [f"unknown source {source!r} — try one of {', '.join(BACKENDS)}"]
    key = f"{source}|{limit}|{query}"
    hit = _cached(key)
    if hit is not None:
        return hit
    results, notes = [], []
    for name in order:
        if len(results) >= limit:
            break
        try:
            found = _run(name, _SEARCH[name], query)
        except Exception as exc:
            notes.append(f"{name} failed ({_reason(exc)})")
            continue
        if not found:
            notes.append(f"{name} no results")
            continue
        results.extend(found[:limit - len(results)])
    if not results:
        tried = ", ".join(f"{n} ({_LABEL[n]}): {_why(notes, n)}" for n in order)
        notes = [f"nothing found — {tried}"]
    _store(key, results[:limit], notes)
    return results[:limit], notes


def _why(notes: list, name: str) -> str:
    for note in notes:
        if note.startswith(name + " "):
            return note[len(name) + 1:]
    return "no results"


def format_results(results: list, notes: list, query: str) -> str:
    """The model-facing rendering, shared by the tool and by tests."""
    if not results:
        return "ERROR: " + ("; ".join(notes) if notes else f"no results for {query!r}")
    lines = [f"Results for {query!r} ({results[0].backend}, {len(results)} shown):"]
    for item in results:
        lines.append(f"- {item.title}: {item.snippet[:220]}")
        if item.url:
            lines.append(f"  {item.url}")
    if notes:
        # Named even on success: a backend that failed this time is the reason a
        # second search may answer differently.
        lines.append("note: " + "; ".join(notes))
    return "\n".join(lines)


# ------------------------------------------------------------------- reading
class _Text(HTMLParser):
    """HTML -> text. Stdlib only: a third-party parser is a dependency this
    feature does not need, and the output only has to be readable."""

    _SKIP = {"script", "style", "noscript", "svg", "head", "template", "iframe"}
    _BREAK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4",
              "h5", "h6", "section", "article", "blockquote", "pre", "ul", "ol",
              "table", "header", "footer", "nav", "form", "main", "aside", "dl",
              "dt", "dd", "figure", "figcaption", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Readable text from HTML: entities decoded, media dropped, blanks collapsed."""
    parser = _Text()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:       # a malformed page is not an error, just a short one
        _warn("html parse failed", exc_info=True)
    raw = "".join(parser.parts)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in raw.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


# Signatures of a page that is NOT the page. Each carries the words it matched,
# because "the site blocked us" is only useful with the reason attached.
_ANTIBOT = (
    ("just a moment", "Cloudflare challenge"),
    ("checking your browser", "browser verification"),
    ("verifying your browser", "browser verification"),
    ("attention required! | cloudflare", "Cloudflare block"),
    ("javascript is required", "JavaScript required"),
    ("enable javascript", "JavaScript required"),
    ("blocked by network security", "network security block"),
    ("log in to your reddit account", "login wall"),
    ("returned error 403", "upstream 403"),
    ("are you a robot", "bot check"),
    ("access denied", "access denied"),
)
_CACHED_SNAPSHOT = "cached snapshot"


def _antibot(text: str) -> tuple:
    """(label, phrase) when `text` is a block page, else ()."""
    sample = (text or "")[:4000].casefold()
    for phrase, label in _ANTIBOT:
        if phrase in sample:
            return label, phrase
    return ()


# Two ranges Python 3.14's flags no longer cover (measured 2026-09-28:
# `ip_address('100.64.0.1').is_private` is False and `'192.0.0.9'.is_global`
# is True there), so they are checked explicitly: CGNAT / overlay-VPN space
# (Tailscale and every carrier-grade NAT hands out 100.64.0.0/10) and the
# IETF protocol-assignments block, which a name server can point a fetch at
# just as usefully as 169.254.169.254.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_IETF_PROTOCOL = ipaddress.ip_network("192.0.0.0/24")


def _is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return True
    return ip.version == 4 and (ip in _CGNAT or ip in _IETF_PROTOCOL)


#: How long a name may take to resolve before the reader gives up on it.
#: `socket.getaddrinfo` has no timeout of its own, and this runs inside a turn
#: the user is waiting on, so a name server that never answers must not hold the
#: reply hostage.
_DNS_TIMEOUT_S = 5.0

#: A wedged resolver leaves its worker thread behind per attempt (the C call
#: owns the thread and cannot be cancelled), so the in-flight count is CAPPED:
#: past `_DNS_THREADS_MAX` the name is failed immediately rather than piling
#: one more daemon thread onto the same wedge. No pool, no reuse — only a lid.
_DNS_THREADS_MAX = 8
_DNS_THREADS = 0
_DNS_THREADS_LOCK = threading.Lock()


def _resolve_host(host: str, port) -> tuple:
    """(getaddrinfo result, problem): a name's addresses, BOUNDED in time.

    Nothing here judges the addresses — `_public_target` does — because the
    result is also what gets PINNED for the fetch, and a judgement made on a
    different reading than the connection would be the very race this pair
    exists to remove.

    The wait is bounded by a worker thread the C call owns and cannot cancel,
    so a wedged resolver leaves one daemon thread behind per attempt; the
    timeout is reported rather than hidden, because "this name never answered"
    is a different fact from "this name has no address". Those threads are
    capped (`_DNS_THREADS_MAX`): a resolver wedged past the lid answers as a
    resolution failure immediately, instead of leaking a thread per fresh
    attempt until the process drowns in them.
    """
    global _DNS_THREADS
    with _DNS_THREADS_LOCK:
        if _DNS_THREADS >= _DNS_THREADS_MAX:
            return [], f"{host} could not be resolved (resolver busy)"
        _DNS_THREADS += 1
    box: dict = {}

    def _call() -> None:
        try:
            box["infos"] = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except Exception as exc:
            box["error"] = exc

    try:
        th = threading.Thread(target=_call, name="dns-resolve", daemon=True)
        th.start()
        th.join(_DNS_TIMEOUT_S)
    finally:
        with _DNS_THREADS_LOCK:
            _DNS_THREADS -= 1
    if th.is_alive():
        return [], f"{host} could not be resolved within {_DNS_TIMEOUT_S:.0f}s"
    if "error" in box:
        return [], f"{host} cannot be resolved ({type(box['error']).__name__})"
    return list(box.get("infos") or []), ""


def _public_target(url: str) -> tuple:
    """(url, pinned addresses, problem): a public http(s) address, and WHICH.

    The reader is asked for addresses by a language model, so the address is
    untrusted input: this machine's own services, the LAN, link-local metadata
    endpoints and mDNS names are refused rather than fetched into a transcript.
    A name that RESOLVES to such an address is refused too, which is what makes
    this a check on the destination and not on the spelling.

    The addresses come back WITH the verdict because checking a name and then
    fetching it by name is a race a name server can win: the same name can
    answer with a public address for this check and with 127.0.0.1 (or
    169.254.169.254) a moment later, when the transport resolves it again — DNS
    rebinding, and the transport is the only place that can close it. So the
    caller hands these exact addresses to the fetch (`connect_to` in the hop
    seam), which connects to them while still validating the certificate against
    the NAME: the checked address and the connected address are then the same
    one by construction rather than by timing.
    """
    raw = str(url or "").strip()
    if not raw:
        # refusal: ssrf_no_address
        return "", [], "no address given"
    if not re.match(r"^https?://", raw, re.I):
        # refusal: ssrf_not_an_http_address
        return "", [], f"{raw!r} is not an http(s) address"
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        # refusal: ssrf_not_a_usable_address
        return "", [], f"{raw!r} is not a usable address"
    host = (parts.hostname or "").strip()
    if not host:
        # refusal: ssrf_no_host
        return "", [], "the address has no host"
    if parts.username or parts.password:
        # refusal: ssrf_address_carries_credentials
        return "", [], "addresses with credentials are not fetched"
    if _is_private_ip(host):
        # refusal: ssrf_private_network
        return "", [], f"{host} is on this machine or a private network"
    if (host.casefold() == "localhost" or host.casefold().endswith(".local")
            or host.casefold().endswith(".internal")
            or host.casefold().endswith(".home.arpa") or "." not in host):
        # refusal: ssrf_not_a_public_host
        return "", [], f"{host} is not a public host"
    # `urlsplit` defers the port check to the `.port` property, which RAISES on
    # anything outside 0-65535 or non-numeric — and this is a model-supplied
    # address, so a bad port has to be a refusal with a sentence like every
    # other bad address, not an exception out of `read_page` (which the tool
    # wrapper could only turn into a generic "could not read that page").
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        # refusal: ssrf_port_is_not_a_number
        return "", [], (f"{raw!r} has a port that is not a number between 0 "
                         f"and 65535")
    infos, problem = _resolve_host(host, port)
    if problem:
        return "", [], problem
    if not infos:
        # refusal: ssrf_host_has_no_address
        return "", [], f"{host} has no address"
    for info in infos:
        if _is_private_ip(str(info[4][0])):
            # refusal: ssrf_name_resolves_private
            return "", [], (f"{host} resolves to a private address "
                            f"({info[4][0]})")
    pins = [(str(info[4][0]), info[4][1]) for info in infos]
    return urllib.parse.urlunsplit(parts), pins, ""


def _public_url(url: str) -> tuple:
    """(url, problem) — `_public_target` without the pinned addresses.

    For callers that only need the verdict: sending the user's address to a
    third-party reader, say, which fetches it from elsewhere and so has no
    socket here to pin.
    """
    clean, _pins, problem = _public_target(url)
    return clean, problem


def read_page(url: str, max_chars: int = READ_MAX_CHARS) -> tuple:
    """(text, via, problem) for `url`.

    Local fetch first; the hosted reader only when the page cannot be read
    locally (nothing usable, or a block page) — and `text` starts with the
    backend that served it, so a third-party fetch is never invisible. `text` is
    "" on failure and `problem` says why, in one sentence a caller can speak.
    """
    clean, pins, problem = _public_target(url)
    if problem:
        return "", "", problem
    try:
        max_chars = max(500, min(int(max_chars or READ_MAX_CHARS), READ_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = READ_MAX_CHARS
    cached = ""
    local_why = "nothing usable"
    try:
        # The redirect-checking fetch, not `_http`: the address was validated,
        # and so must every hop the server sends us to (see `_read_fetch`).
        raw, _final, hop_problem = _read_fetch(clean, READ_TIMEOUT,
                                               connect_to=pins)
        if hop_problem:
            _record(_READER_SEEN, "local", False, hop_problem)
            return "", "", hop_problem
        local = html_to_text(raw.decode("utf-8", "replace"))
        # A page is "useless locally" when the site blocked us, or when there is
        # a LOT of HTML and almost no text — that is a JavaScript shell. A page
        # that is simply SHORT is short: sending it to a third-party reader would
        # put the address on someone else's server for nothing (example.com did
        # exactly that until this check), and it is the user's address.
        thin_shell = len(local) < MIN_LOCAL_TEXT and len(raw) > MIN_LOCAL_BYTES
        if not thin_shell and len(local) >= 1 and not _antibot(local):
            _record(_READER_SEEN, "local", True)
            return _prefixed(local, VIA_LOCAL, max_chars), VIA_LOCAL, ""
        blocked_locally = _antibot(local)
        if blocked_locally:
            local_why = f"blocked by the site ({blocked_locally[0]})"
        elif not local.strip():
            local_why = f"no text in {len(raw)} bytes of markup"
        else:
            local_why = f"only {len(local)} characters of text in {len(raw)} bytes"
        _record(_READER_SEEN, "local", False, local_why)
    except Exception as exc:
        local_why = f"fetch failed ({_reason(exc)})"
        _record(_READER_SEEN, "local", False, local_why)
        _warn("local fetch of %s failed: %s", clean, exc)
    # Fallback: the hosted reader. Gated, because this is where the user's
    # TARGET ADDRESS — not their search text — leaves the machine, and the
    # audit of 2026-09-27 found it riding on the same checkbox as a search
    # query, under a label that described neither. The refusal is a sentence
    # naming the switch, in the same shape as every other refusal here, because
    # a silent empty result would read as "that page has no text".
    #
    # A RESOLVER, read per call: the user can turn this on in the settings app
    # while the bubble is running, and the next read must honour it.
    if not _hosted_reader_enabled():
        # refusal: read_page_hosted_reader_switched_off
        return "", "", (f"nothing readable at {clean} — local fetch: "
                        f"{local_why}. The third-party reader (r.jina.ai) is "
                        f"switched off in handsoff settings; turn on 'Third-"
                        f"party page reader' to allow it.")
    try:
        # Same walk for the fallback: the reader's own hops are addresses too,
        # and a hosted reader is not a licence to follow one into the LAN.
        raw, _final, hop_problem = _read_fetch(JINA_READER + clean, READ_TIMEOUT)
        if hop_problem:
            _record(_READER_SEEN, "jina", False, hop_problem)
            # refusal: read_page_reader_hop_refused
            return "", "", (f"{hop_problem}; local fetch: {local_why}")
        body = raw.decode("utf-8", "replace")
    except Exception as exc:
        _record(_READER_SEEN, "jina", False, _reason(exc))
        # refusal: read_page_nothing_readable
        return "", "", (f"nothing readable at {clean} — local fetch: {local_why}; "
                         f"the third-party reader failed too ({_reason(exc)})")
    blocked = _antibot(body)
    if blocked:
        _record(_READER_SEEN, "jina", False, blocked[0])
        # refusal: read_page_blocked_by_the_site
        return "", "", (f"the site refuses automated readers ({blocked[0]}); "
                         f"local fetch: {local_why}")
    _record(_READER_SEEN, "jina", True)
    if _CACHED_SNAPSHOT in body[:2000].casefold():
        cached = " — a cached snapshot, so it may be out of date"
    label = VIA_JINA + cached
    return _prefixed(body, label, max_chars), label, ""


def _prefixed(text: str, via: str, max_chars: int) -> str:
    body = text[:max_chars]
    if len(text) > max_chars:
        body += f"\n[truncated — {len(text) - max_chars} more characters]"
    return f"via {via}\n{body}"


def read_results(results: list, count: int = 0, max_chars: int = TOOL_MAX_CHARS):
    """(blocks, notes) fetching the top `count` results that carry a URL.

    Kept here rather than in the tool so the reader's own rules — the public-URL
    check, the disclosure line, the truncation — are the ones used on every path
    that reads a page.
    """
    try:
        count = max(0, min(int(count or 0), READ_TOP_MAX))
    except (TypeError, ValueError):
        return [], ["read_top must be a number"]
    blocks, notes = [], []
    for item in results:
        if len(blocks) >= count:
            break
        if not item.url:
            continue
        text, via, problem = read_page(item.url, max_chars=max_chars)
        if problem:
            notes.append(f"could not read {item.url}: {problem}")
            continue
        blocks.append(f"--- page {len(blocks) + 1}: {item.url} ({via})\n{text}")
    if count and not blocks and not notes:
        notes.append("no result carried a URL to read")
    return blocks, notes


# ------------------------------------------------------------------ reporting
def _ago(at: float, now: float) -> str:
    secs = max(0, int(now - at))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def _searxng_alive(url: str) -> bool:
    """TCP connect to the configured instance. Cheap, LOCAL, and a real answer:
    a port that is not listening is how "SearXNG is running" would otherwise be
    assumed from a setting that merely names it."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not host:
            return False
        with socket.create_connection((host, port), timeout=SEARXNG_PROBE_TIMEOUT):
            return True
    except Exception:
        return False


def search_note(now: float | None = None) -> str:
    """One doctor line: what this process has OBSERVED, per backend."""
    now = time.time() if now is None else now
    parts = []
    for name in BACKENDS:
        if name == "searxng":
            url = _searxng_url()
            if not url:
                parts.append("searxng off")
            else:
                parts.append("searxng ok" if _searxng_alive(url) else "searxng not running")
            continue
        seen = _SEEN.get(name)
        if not seen:
            parts.append(f"{name} untried")
        elif seen["ok"]:
            quota = _QUOTA.get(name) or ""
            parts.append(f"{name} ok ({_ago(seen['at'], now)}"
                         + (f", {quota}" if quota else "") + ")")
        else:
            parts.append(f"{name} FAILED ({seen['why']}, {_ago(seen['at'], now)})")
    return ", ".join(parts)


def reader_note(now: float | None = None) -> str:
    """One doctor line: which reader served pages, and how it went."""
    now = time.time() if now is None else now
    out = []
    local = _READER_SEEN.get("local")
    if not local:
        out.append("local fetch untried")
    elif local["ok"]:
        out.append(f"local fetch ok ({_ago(local['at'], now)})")
    else:
        out.append(f"local fetch FAILED ({local['why']}, {_ago(local['at'], now)})")
    jina = _READER_SEEN.get("jina")
    if not jina:
        out.append("Jina fallback unused")
    elif jina["ok"]:
        out.append(f"Jina fallback used ({_ago(jina['at'], now)})")
    else:
        out.append(f"Jina fallback refused ({jina['why']}, {_ago(jina['at'], now)})")
    return ", ".join(out)


def last_problem(name: str) -> str:
    """Why `name` last produced nothing, or "" when its last answer worked.

    The record `doctor_lines` reads is the only place that knows WHY a backend
    is quiet, so a caller that has to report a failure asks here instead of
    guessing at a cause two layers down. "Offline?" was such a guess: the news
    tool printed it while DuckDuckGo was in fact refusing with a bot challenge
    — a wrong diagnosis the user cannot act on.
    """
    entry = _SEEN.get(str(name))
    if not entry or entry.get("ok"):
        return ""
    return str(entry.get("why") or "")


def doctor_lines() -> list:
    """The lines `doctor` splices in. Two, one per capability."""
    return [f"search: {search_note()}", f"reader: {reader_note()}"]
