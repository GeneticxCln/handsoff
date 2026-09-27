"""No rule in this tree may exist in two places that can disagree.

Why this file exists. The four defects found while writing `test_brain.py` had
one shape: a rule written twice, the fix applied to one copy. The default turn
path got a `ollama pull` hint its sibling already had; the shipped no-core
fallback's copy of the streamer had neither that nor the `URLError` conversion
its own non-streaming arm had; the leaked-token filter dropped the whole
sentence a token opened. None of them was a hard bug. All of them were a
reader assuming that a fix to a name is a fix to the name, and the gap between
the two copies widening by exactly one line.

What a sweep can and cannot do, measured first, because a property test that
reports a defect where there is none is worse than no property test:

  * a same-NAME sweep is complete and imprecise — 55 module-and-method names
    in the production sources, of which 23 are `main`/`start`/`paintEvent`;
  * a "does it look like a call" check has BOTH error kinds — it missed
    `getattr(SCHEMA, "page_groups")` and flagged a local worker called `_run`
    as a mirror of `Assistant.run`;
  * the noise is all METHODS, and methods genuinely collide (`_read` is a
    settings-row reader in one place and a socket reader in another).

So the enumeration is restricted to MODULE-LEVEL definitions, where a name is a
module's own vocabulary rather than a method's, and the classification is a
judgement recorded here rather than inferred. What is mechanical is the part
that must be: the derivation, so a new copy cannot appear unclassified, and the
check each grade gets, chosen so that it cannot report a defect that is not
there.

Four grades, and the tree settles into them:

  MIRROR   a program re-exposes a core rule as `_name` (19 today). The mirror
           may add bookkeeping — a GPU touch, a repaint, an error wrapper — but
           it must still NAME the rule it mirrors. A mirror that grows its own
           logic stops naming it, and that is the moment there are two rules.
           No manifest: all 19 already hold, so this is a property of the tree
           rather than a list of promises.
  SEAM     one name, two files, the second re-exposing the first (11 today).
  COPY     one name, two files, two implementations of the same rule (1 today,
           `_http_get`). The check is that the two bodies are the SAME code:
           divergence is the defect, so it is the thing the check looks for.
  DISTINCT one name, two files, two different rules (7 today). The check is
           that the bodies really do differ — the claim is verified, not
           trusted — and that a discriminator is written down so the next
           reader does not read a fix to one as a fix to the other.

And the case the rest of this file cannot see: a rule copied into an
`except ImportError` branch, where delegation is impossible because the module
being delegated to is the one that is missing. Those are forced, so they are
enumerated and each names the test that holds it.
"""
from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

#: The shipped programs. A `_name` here next to a `name` in a core module is
#: the shape this file is about; a method of the same name is not.
PROGRAMS = ("handsoff.py", "handsoff-settings.py", "hardware.py")

#: `settings_schema.py` sits at the root but is a core module all the same.
CORE_MODULES = ("settings_schema.py",)


def _sources() -> list[Path]:
    paths = [p for p in sorted(ROOT.glob("*.py"))
             if not p.name.startswith(".")]
    paths += sorted((ROOT / "core").glob("*.py"))
    return [p for p in paths if p.exists()]


def _module_defs(path: Path) -> dict[str, ast.AST]:
    """The MODULE-LEVEL functions of one file. Methods are excluded on purpose.

    A method name is scoped to its class, so two classes answering to `_read`
    are not two copies of a rule; a module-level name is the module's own
    vocabulary, and two modules using the same one is a claim worth checking.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _by_file() -> dict[str, dict[str, ast.AST]]:
    return {p.name: _module_defs(p) for p in _sources()}


def _segment(path: Path, node: ast.AST) -> str:
    """The shipped source of one definition, sliced out of the file.

    Sliced rather than re-typed: a transcription can differ from the file
    without anyone noticing, and this whole file is about two pieces of source
    that are not the same. `end_lineno` is what makes a check see the whole
    body rather than the signature.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[node.lineno - 1:node.end_lineno])


def _body(path: Path, node: ast.AST) -> str:
    """The definition WITHOUT its `def` line.

    The signature carries the mirror's own name, so a check that searched the
    whole segment for that name would always pass — which is how the first
    version of the mirror check here came to accept a mirror that had stopped
    delegating entirely. What has to be searched is the code under the name.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[node.lineno:node.end_lineno])


def _fingerprint(node: ast.AST) -> str:
    """A hash of one definition's CODE, with its name and docstring removed.

    Name-insensitive on purpose: two copies of a rule are the same code under
    two names (`_http_get` and `_hf_hub_cache`'s siblings) as often as under
    one. Docstring-insensitive because prose is not the rule — and because the
    two copies of a rule are usually the one place where one of them has
    documentation and the other does not.
    """
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant) and isinstance(
            body[0].value.value, str):
        body = body[1:]
    try:
        clone = ast.FunctionDef(name="", args=node.args, body=body,
                                decorator_list=[], returns=None,
                                type_comment=None, type_params=[])
    except TypeError:                     # 3.12 and earlier take no type_params
        clone = ast.FunctionDef(name="", args=node.args, body=body,
                                decorator_list=[], returns=None,
                                type_comment=[])
    return hashlib.sha256(ast.dump(clone).encode()).hexdigest()[:12]


def _mirrors(mods) -> dict[str, list[tuple[str, str, ast.AST]]]:
    """A program's module-level `_name`, where a core module has `name`."""
    out: dict[str, list[tuple[str, str, ast.AST]]] = {}
    for pname, defs in mods.items():
        if pname not in PROGRAMS:
            continue
        for name, node in defs.items():
            if not name.startswith("_") or name.startswith("__"):
                continue
            owner = name.lstrip("_")
            owners = sorted(c for c, cdefs in mods.items()
                            if c != pname and c not in PROGRAMS
                            and owner in cdefs)
            for owner_file in owners:
                out.setdefault(f"{pname}:{name}", []).append(
                    (pname, owner_file, node))
    return out


def _shared_names(mods) -> dict[str, list[str]]:
    """One module-level name, defined in more than one file."""
    out: dict[str, list[str]] = {}
    for name in {n for defs in mods.values() for n in defs}:
        where = sorted(f for f, defs in mods.items() if name in defs)
        if len(where) > 1:
            out[name] = where
    return out


def _in_import_fallback(mods) -> list[tuple[str, str, int, ast.AST]]:
    """Every function defined inside an `except ImportError` branch.

    Not a name sweep: the BRANCH is the site. These definitions exist because
    the module they would delegate to could not be loaded, so "make it a
    mirror" is not available to them and each one has to be held some other
    way — which is why each names the test that holds it below. Methods count,
    because in a branch like `_LegacyBrain` every rule IS a method.
    """
    found = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for handler in getattr(node, "handlers", []) or []:
                if not (isinstance(handler.type, ast.Name)
                        and handler.type.id == "ImportError"):
                    continue
                found += [(path.name, dotted, node.lineno, node)
                          for dotted, node in _defs_under(handler)]
    return sorted(found, key=lambda row: (row[0], row[2], row[1]))


def _defs_under(node, prefix: str = "") -> list[tuple[str, ast.AST]]:
    """`(Dotted.name, def)` for every def under `node`, classes included."""
    out = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            out += _defs_under(child, f"{prefix}{child.name}.")
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((f"{prefix}{child.name}", child))
        else:
            out += _defs_under(child, prefix)
    return out


# ------------------------------------------------------------------ the mirrors


class TestAMirrorAlwaysNamesItsOwner:
    """The property with no manifest behind it, because the tree satisfies it.

    All nineteen hold today, which is what makes it a property rather than a
    list of promises: the check below is "a mirror mentions the rule it
    mirrors", and nothing in this file has to be updated for it to keep
    running. A mirror that grows its own logic stops mentioning the owner, and
    that is the moment one rule became two.
    """

    def test_every_program_mirror_names_the_core_rule_it_mirrors(self):
        mods = _by_file()
        mirrors = _mirrors(mods)
        assert mirrors, (
            "the mirror derivation found nothing — the sweep has stopped "
            "seeing the shape it exists for, and a guard that finds nothing "
            "passes")
        assert len(mirrors) >= 19, (
            f"only {len(mirrors)} mirrors found where there were 19; the "
            "derivation is not looking at the tree the manifest was written "
            "against")
        silent = {}
        for key, sites in sorted(mirrors.items()):
            pname, owner_file, node = sites[0]
            owner = node.name.lstrip("_")
            source = _body(ROOT / pname, node)
            # Loose on purpose: the delegation is not always a call. Four of
            # these go through `getattr(SCHEMA, "page_groups")` and one through
            # a renamed accessor, and a check that insisted on `.name(` would
            # report all five as defects. It must NOT search the `def` line
            # though — the mirror's own name is there, so every mirror would
            # pass and the check would be decoration.
            named = (owner in source
                     or Path(owner_file).stem in source)
            if not named:
                silent[key] = (pname, owner_file, node.lineno)
        assert not silent, (
            f"{len(silent)} mirror(s) no longer name the rule they mirror: "
            f"{silent}. A mirror may add bookkeeping around the call, but a "
            "mirror that does not mention the core rule is a second "
            "implementation of it — which is how the streaming defects "
            "happened, and nothing in the log says so")

    def test_the_mirror_set_holds_no_duplicate_owner(self):
        """One rule, one mirror per program.

        Two mirrors of one core rule are two seams, and two seams are two
        places for a fix to land — which is the situation this file exists to
        end. The derivation keeps every (program, owner-file) pair, so a second
        one shows up here rather than as a silent second seam.
        """
        mirrors = _mirrors(_by_file())
        pairs = [(pname, owner_file, node.name)
                 for sites in mirrors.values()
                 for pname, owner_file, node in sites]
        assert len(pairs) == len(set(pairs)), (
            f"a rule is mirrored twice: "
            f"{[p for p in pairs if pairs.count(p) > 1]}")


# ------------------------------------------------------------ the shared names

#: The manifest. One entry per name the sweep finds in two files, and the
#: derived set has to match it exactly: a name that appears, vanishes, or
#: changes files without an entry here is a failure, which is what stops this
#: file from being a snapshot of a tree nobody maintains.
SEAMS = {
    # name: (the site that re-exposes it, the owner it must name)
    "_stop_recorder_bounded": ("handsoff.py", "_audio"),
    "doctor_json": ("handsoff.py", "_core_doctor"),
    "run_doctor": ("handsoff.py", "_core_doctor"),
    "get_whisper": ("handsoff.py", "_audio"),
    "get_tts": ("handsoff.py", "_audio"),
    "transcribe": ("handsoff.py", "_audio"),
    "tts_to_wav": ("handsoff.py", "_audio"),
    "warm_tts": ("handsoff.py", "_audio"),
    "ollama_available": ("handsoff.py", "_brain"),
    "ollama_chat": ("handsoff.py", "_brain"),
    "ollama_chat_stream": ("handsoff.py", "_brain"),
}

COPIES = {
    # name: (the two files, why there are two)
    "_http_get": (("core/calendar.py", "handsoff.py"),
                  "the ICS fetch, duplicated when the calendar was extracted; "
                  "identical code today, and the check is that it stays "
                  "identical — a calendar reader and an app that both cap at "
                  "2 MB and set the user agent is one rule written twice"),
}

#: Where each classified name is allowed to live. The census asserts the
#: derived SET of names, which on its own lets a THIRD copy of a name slip in
#: under an entry written for two: appending a `get_whisper` to a third file
#: leaves the name set unchanged and the entry still "covers" it, while the
#: thing that file did is exactly the defect this file is for.
SITES = {
    "_dep": ("doctor.py", "tools.py"),
    "_http_get": ("calendar.py", "handsoff.py"),
    "_open_input": ("audio.py", "handsoff.py"),
    "_stop_recorder_bounded": ("audio.py", "handsoff.py"),
    "_wake_lines": ("doctor.py", "handsoff.py"),
    "configure": ("audio.py", "bubble.py", "web.py"),
    "doctor_json": ("doctor.py", "handsoff.py"),
    "get_tts": ("audio.py", "handsoff.py"),
    "get_whisper": ("audio.py", "handsoff.py"),
    "look_matching": ("settings.py", "settings_schema.py"),
    "main": ("handsoff-settings.py", "handsoff.py", "hardware.py"),
    "notify": ("bubble.py", "handsoff.py"),
    "ollama_available": ("brain.py", "handsoff.py"),
    "ollama_chat": ("brain.py", "handsoff.py"),
    "ollama_chat_stream": ("brain.py", "handsoff.py"),
    "run_doctor": ("doctor.py", "handsoff.py"),
    "set_dependencies": ("doctor.py", "tools.py"),
    "transcribe": ("audio.py", "handsoff.py"),
    "tts_to_wav": ("audio.py", "handsoff.py"),
    "warm_tts": ("audio.py", "handsoff.py"),
}

DISTINCT = {
    # name: what actually differs — the sentence a future reader needs, so a
    # fix to one side is never read as a fix to the other
    "_dep": "each core module's own dependency accessor, for its own context "
            "var: `doctor` installs a DoctorDeps, `tools` a ToolDeps, and "
            "neither can borrow the other's",
    "_open_input": "`core.audio` opens the device; `handsoff` opens it under "
                   "the mic lock, which is the rule that made "
                   "`_stop_recorder_bounded` necessary",
    "_wake_lines": "the host OBSERVES which channel can wake the bubble; "
                   "`core.doctor` RENDERS that observation. The dependency "
                   "points the other way, which is why the name is shared and "
                   "the rule is not",
    "configure": "one per core module, each configuring its own subsystem "
                 "(audio devices, the bubble's paint seam, the web backends). "
                 "A convention, not a shared rule: there is nothing for one to "
                 "delegate to",
    "look_matching": "`settings_schema` DERIVES the look from the five values; "
                     "`core.settings` wraps a matcher that may not be "
                     "installed and answers '' when it is not",
    "set_dependencies": "each core module installs a deps object into its own "
                        "contextvar, by design — the point of the seam is that "
                        "`tools` and `doctor` have separate ones",
    "notify": "`core.bubble.notify` is an inert placeholder the host replaces "
              "(it says so in its own docstring); `handsoff.notify` is the "
              "coalescing implementation",
    "main": "one entry point per shipped program: the bubble app, the settings "
            "window and the hardware probe are three programs, and none of "
            "them can be the other's",
}


class TestTheSharedNames:
    def test_every_shared_name_is_classified(self):
        """The enumeration is derived, so it cannot go stale behind this file."""
        shared = _shared_names(_by_file())
        assert shared, (
            "the shared-name sweep found nothing — it has stopped seeing the "
            "shape it exists for, and a guard that finds nothing passes")
        classified = set(SEAMS) | set(COPIES) | set(DISTINCT)
        missing = sorted(set(shared) - classified)
        assert not missing, (
            f"{missing} is defined in more than one file and is not classified "
            "here. Copying a rule is not free: say which of the four grades it "
            "is (MIRROR, SEAM, COPY, DISTINCT) and how it is held, or the next "
            "fix lands on one copy")
        gone = sorted(classified - set(shared))
        assert not gone, (
            f"{gone} is classified here but the tree no longer has it in two "
            "files — the entry is stale, and a stale manifest is how a guard "
            "stops guarding")
        # and not merely the same NAMES, but the same PLACES: a third copy of a
        # name with an entry written for two is the defect, and a name-set
        # comparison would wave it through
        for name, where in sorted(shared.items()):
            assert tuple(where) == SITES.get(name), (
                f"{name} is now defined in {where}, and this file says "
                f"{SITES.get(name)}. A copy in a new place is a second "
                "implementation or a third seam — say which, and pin it")

    @pytest.mark.parametrize("name", sorted(SEAMS))
    def test_a_seam_names_the_rule_it_re_exposes(self, name):
        """A seam may wrap the call; it may not replace it.

        Loose on purpose, for the reason the mirrors are: four mirrors delegate
        through `getattr` and one through a renamed accessor, and a check that
        insisted on a call would report those as defects.
        """
        pname, owner = SEAMS[name]
        mods = _by_file()
        assert name in mods[pname], f"{pname} no longer defines {name}"
        source = _body(ROOT / pname, mods[pname][name])
        assert owner in source, (
            f"{pname}:{name} no longer mentions {owner}. A seam is allowed to "
            "add bookkeeping around the call it re-exposes — a GPU touch, a "
            "repaint, an error wrapper, a reload-probe disarm — but one that "
            "does not name the rule it re-exposes is a second implementation "
            "of it, and a fix to one copy is a fix nobody else sees")

    @pytest.mark.parametrize("name", sorted(COPIES))
    def test_a_copy_is_still_the_same_code(self, name):
        """The check a COPY wants is equality, so divergence is the failure.

        No corpus, no hand-written inputs: the defect is "one copy was edited
        and the other was not", and hashing both bodies finds that without
        anyone having to decide which inputs matter. A docstring is not part of
        it — prose is not the rule, and one copy having documentation the other
        lacks is not a divergence.
        """
        (left, right), _why = COPIES[name]
        mods = _by_file()
        left_node = mods[Path(left).name][name]
        right_node = mods[Path(right).name][name]
        assert _fingerprint(left_node) == _fingerprint(right_node), (
            f"{left} and {right} both define {name} and their bodies now "
            "differ. If that is intended, it is not a copy any more: give it a "
            "name of its own and classify it, or delete one")

    @pytest.mark.parametrize("name", sorted(DISTINCT))
    def test_a_distinct_pair_really_is_two_rules(self, name):
        """The claim "these differ" is checked, not trusted.

        Without this the classification is a comment: two bodies that had
        quietly become identical would still be filed as DISTINCT, and the
        entry would be a permanent excuse for a copy nobody is looking at.
        """
        shared = _shared_names(_by_file())
        assert name in shared, f"{name} is no longer in two files"
        mods = _by_file()
        files = shared[name]
        prints = {_fingerprint(mods[f][name]) for f in files}
        assert len(prints) == len(files), (
            f"{name} is classified as two different rules across {files}, but "
            f"two of them are the same code ({len(prints)} distinct bodies "
            f"for {len(files)} files). That is a COPY wearing a DISTINCT "
            "entry")
        # a floor on the reason, because the entry is the only thing the next
        # reader has: "different" is not a discriminator, a file name is
        assert len(DISTINCT[name]) >= 40, (
            f"the DISTINCT entry for {name} ({DISTINCT[name]!r}) says less "
            "than a sentence — it is the note a future reader has instead of "
            "reading both bodies, so it has to name what differs")


# ------------------------------------------------------- the forced copies
# A rule copied into an `except ImportError` branch cannot delegate: the module
# it would delegate to is the one that failed to load. Each is listed with the
# test that holds it, and the test checks the reference rather than trusting
# it — a reference to a test that has stopped mentioning the rule is a rule
# nobody is holding any more.

BRANCH_COPIES = {
    ("handsoff.py", "_LegacyBrain.ollama_available"):
        ("tests/test_hardening.py::TestMissingBrainFallback", ""),
    ("handsoff.py", "_LegacyBrain.ollama_chat"):
        ("tests/test_hardening.py::TestMissingBrainFallback", ""),
    ("handsoff.py", "_LegacyBrain.ollama_chat_stream"):
        ("tests/test_hardening.py::TestMissingBrainFallback", ""),
    ("handsoff.py", "_MissingAudio.portaudio_in_use"):
        ("tests/test_hardening.py::TestMissingAudioFallback", ""),
    ("handsoff.py", "_MissingAudio.portaudio_busy"):
        ("tests/test_hardening.py::TestMissingAudioFallback", ""),
}

#: Also defined inside a branch, and not copies of a rule. Keyed by the whole
#: DOTTED definition, not by the class: a class-level entry would cover every
#: method anyone later adds to it, which is the opposite of what this file is
#: for — measured 2026-09-27, when `_LegacyTurnStream` as an entry swallowed a
#: mutation that gave the class a brand-new unheld `ollama_resident`.
BRANCH_OTHER = {
    ("handsoff.py", "_LegacyTurnStream.__init__"): "a constructor, one per "
        "class; this one carries the four attributes a turn reads off a "
        "TurnStream",
    ("handsoff.py", "_MissingAudio.configure"): "audio's own configure, "
        "standing in for a module that is not there",
    ("handsoff.py", "_MissingBubble.configure"): "the bubble's own configure, "
        "standing in for a module that is not there",
    ("handsoff.py", "_MissingAudio._missing"): "raises ImportError where the "
        "module-level one prints an install hint and exits — the two must "
        "differ, so they are DISTINCT rather than a copy",
    ("handsoff.py", "_MissingBubble._missing"): "as above, for the bubble",
    ("handsoff.py", "_MissingAudio.Recorder.__init__"): "the absent-module "
        "stand-in: it raises ImportError where the real Recorder opens a "
        "device, so the two MUST differ — a stand-in that behaved like the "
        "thing it replaces would take a branch that should be loud and make "
        "it quiet",
    ("handsoff.py", "_MissingBubble.BubbleWidget.__init__"): "as above, for "
        "the bubble's widget, which cannot paint without core/bubble.py",
}


class TestTheCompatibilityBranches:
    """The copies a name sweep cannot see, because the site is the BRANCH."""

    def test_every_definition_in_a_fallback_branch_is_accounted_for(self):
        found = [(f, n) for f, n, _l, _node in _in_import_fallback(_by_file())
                 if f == "handsoff.py"]

        def covered(name: str) -> bool:
            # EXACT match only. A class-level entry would cover every method
            # added to that class later, and "every method" is the thing this
            # census exists to enumerate.
            entries = [k[1] for k in BRANCH_COPIES] + [k[1] for k in BRANCH_OTHER]
            return name in entries

        missing = sorted(name for _f, name in found if not covered(name))
        assert not missing, (
            f"{missing} is defined inside an `except ImportError` branch and is "
            "not accounted for. A definition in that branch is a copy of a rule "
            "the tree could not load, so it cannot be a mirror and cannot be "
            "found by a name sweep — say which test holds it, or record it as "
            "something that is not a copy and why")

    @pytest.mark.parametrize("rule", sorted(rule for _f, rule in BRANCH_COPIES))
    def test_a_forced_copy_names_the_test_that_holds_it(self, rule):
        # keyed by RULE NAME, not by the tuple: pytest reads a tuple of
        # strings as several argument names, which fails before the test body
        key = next(k for k in BRANCH_COPIES if k[1] == rule)
        test_ref, _note = BRANCH_COPIES[key]
        path, _, class_name = test_ref.partition("::")
        test_file = ROOT / path
        assert test_file.exists(), f"{path} is gone; {rule} has no holder"
        source = test_file.read_text(encoding="utf-8")
        assert f"class {class_name}" in source, (
            f"{test_ref} no longer exists, so nothing holds {rule} any more")
        assert rule.split(".")[-1] in source, (
            f"{test_ref} no longer mentions {rule.split('.')[-1]}. This check "
            "is a reference, not a proof — it cannot tell a live assertion from "
            "a passing mention — but deleting the assertion deletes the name, "
            "which is the way a rule loses its holder without anyone deciding "
            "to let it")

    def test_the_branch_census_is_not_empty(self):
        """A derivation that suddenly finds nothing has stopped working.

        The `except ImportError` arms are the tree's own compatibility
        branches, and they are the one place a copy is unavoidable. If a
        refactor removed them the guard would go quiet rather than pass, which
        is the failure mode a census has to be checked for.
        """
        found = [name for _f, name, _l, _node in _in_import_fallback(_by_file())]
        rules = [rule for _f, rule in BRANCH_COPIES
                 if rule in found]
        assert len(rules) == len(BRANCH_COPIES), (
            f"only {len(rules)} of {len(BRANCH_COPIES)} classified branch "
            f"copies were found in the tree ({rules}). The census and the "
            "manifest disagree, and one of them is wrong")
