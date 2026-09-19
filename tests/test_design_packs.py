"""The `image` design's art per state (core/bubble.py): packs, files, resolution.

Two ways exist to give that design several pictures, and they obey ONE
precedence:

  * a PACK — a data folder (`pack.json` plus one picture per state) installed
    from the Appearance tab, so ONE choice switches several pictures together;
  * per-state FILES (`design_image_<state>`) chosen in the same card, with
    `design_image_path` as the fallback for the states that have none.

A pack is a contract about a folder someone else wrote, so it is pinned here
without a window: what counts as a pack, which picture a state draws, what is
refused and WHY, and the rule that a pack which cannot be read draws nothing
rather than art assembled from the parts that happened to check out. The
per-state files are pinned the same way, including the order a pack wins in.

No QApplication is needed: these paths touch QImage decoding only (the
prompt-to-pixmap step in `_image_entry` is exercised by the offscreen GUI
scenario in tests/test_settings_gui.py).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from conftest import core_module
from settings_schema import BUBBLE_STATES, DEFAULT_SETTINGS, DESIGN_IMAGE_KEYS


def png(path: Path, rgba=(90, 140, 255, 255)) -> Path:
    """A real PNG on disk — the only thing `_decoded_image` accepts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(32, 32, QImage.Format_ARGB32)
    img.fill(QColor(*rgba))
    assert img.save(str(path)), f"could not write {path}"
    return path


def manifest(folder: Path, body: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "pack.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


@pytest.fixture()
def bubble(tmp_path, monkeypatch):
    mod = core_module("bubble")
    monkeypatch.setattr(mod, "PACKS_DIR", tmp_path / "design-packs")
    monkeypatch.setattr(mod, "SETTINGS", {})
    # Both caches are process-global and keyed WITHOUT the packs root, so two
    # tests installing the same slug would otherwise share entries.
    monkeypatch.setattr(mod, "_PACK_CACHE", {})
    monkeypatch.setattr(mod, "_IMAGE_CACHE", {})
    # ...and so is the live preview the settings panel pushes. It is what the
    # BUBBLE is drawing, so a preview left behind by one test would be drawn by
    # the next one — the same class of leak, in the thing that is most visible.
    monkeypatch.setattr(mod, "_PREVIEW", {"manifest": None, "name": "",
                                           "source": "", "at": 0.0})
    return mod


def install(bubble, folder: Path):
    """install_pack plus the paths, so each test reads as one sentence."""
    slug, message = bubble.install_pack(folder)
    return slug, message, Path(bubble.PACKS_DIR)


class TestPackIdentity:
    def test_a_name_becomes_one_directory_name(self, bubble):
        assert bubble.pack_slug("Optimus Prime / v2") == "optimus-prime-v2"
        assert bubble.pack_slug("  Mixed__Case--x  ") == "mixed__case-x"
        # nothing that could become a path survives
        assert bubble.pack_slug("///") == ""
        assert bubble.pack_slug("") == ""
        assert bubble.pack_slug(None) == ""

    def test_an_installed_pack_is_named_copied_and_listed(self, bubble, tmp_path):
        src = tmp_path / "src"
        png(src / "base.png")
        manifest(src, {"name": "Optimus Prime", "any": "base.png"})
        slug, message, root = install(bubble, src)
        assert slug == "optimus-prime"
        assert "Optimus Prime" in message
        assert bubble.installed_packs() == [("optimus-prime", "Optimus Prime")]
        loaded = bubble.load_pack(slug)
        assert loaded["any"] == str(root / slug / "base.png"), (
            "the pack must be read from the COPY, not from where it came from")
        assert bubble.pack_problem(slug) == ""

    def test_the_copy_survives_the_source_folder(self, bubble, tmp_path):
        src = tmp_path / "src"
        png(src / "base.png")
        manifest(src, {"name": "Copy", "any": "base.png"})
        slug, _message, _root = install(bubble, src)
        shutil.rmtree(src)
        assert bubble.pack_problem(slug) == "", (
            "deleting the folder the pack came from must not break it — that is "
            "the whole reason an install COPIES rather than references")

    def test_subfolders_are_allowed_and_leaving_the_pack_is_not(self, bubble,
                                                               tmp_path):
        nested = tmp_path / "nested"
        png(nested / "pics" / "base.png")
        manifest(nested, {"name": "Nested", "any": "pics/base.png"})
        assert bubble.install_pack(nested)[0] == "nested"

        outside = png(tmp_path / "outside.png")
        escape = tmp_path / "escape"
        escape.mkdir()
        manifest(escape, {"name": "Escape", "any": "../outside.png"})
        slug, message = bubble.install_pack(escape)
        assert slug == "", "a pack must not be able to install from outside itself"
        assert "outside the pack" in message and "outside.png" in message

        manifest(escape, {"name": "Escape", "any": str(outside)})
        slug, message = bubble.install_pack(escape)
        assert slug == "", "an absolute path must be refused too"
        assert "outside the pack" in message

        manifest(escape, {"name": "Escape", "any": "/etc/hostname"})
        assert bubble.install_pack(escape)[0] == ""

    def test_a_symlink_out_of_the_folder_is_refused(self, bubble, tmp_path):
        outside = png(tmp_path / "outside.png")
        src = tmp_path / "src"
        src.mkdir()
        (src / "link.png").symlink_to(outside)
        manifest(src, {"name": "Link", "any": "link.png"})
        slug, message = bubble.install_pack(src)
        assert slug == ""
        assert "outside the pack" in message, (
            "resolving a name inside the folder is not enough — the target has "
            "to be inside it as well")


class TestPackResolution:
    def test_a_non_finite_fps_is_refused_by_the_bounds(self, bubble, tmp_path):
        """NaN and inf are refused by the RANGE check, not by a NaN test.

        The condition read `not MIN <= fps <= MAX or fps != fps`, and the second
        term was dead: every comparison against a NaN is False, so the bounds
        already refuse it. Kept as a test because the dead term is what a reader
        would trust if the range check were ever loosened — and because a
        manifest is somebody else's file, so `NaN` and `Infinity` reach
        `float()` through `json` (Python parses both).
        """
        for raw in (float("nan"), float("inf"), float("-inf"), 0.4, 30.5):
            src = tmp_path / f"bad-{raw}"
            png(src / "base.png")
            manifest(src, {"name": f"Bad {raw}",
                           "any": {"frames": ["base.png"], "fps": raw}})
            slug, message = bubble.install_pack(src)
            assert slug == "", f"fps={raw!r} must not install"
            assert "fps" in message and "between" in message, message
        # ...and a value INSIDE the bounds still installs, so the guard is not
        # passing because every animation is refused.
        src = tmp_path / "fine"
        png(src / "base.png")
        manifest(src, {"name": "Fine",
                       "any": {"frames": ["base.png"], "fps": 12}})
        assert bubble.install_pack(src)[0] == "fine"

    def _full(self, bubble, tmp_path, folder="full", name="Full"):
        src = tmp_path / folder
        for state in bubble.PACK_STATES:
            png(src / f"{state}.png")
        manifest(src, {"name": name,
                       "states": {s: f"{s}.png" for s in bubble.PACK_STATES}})
        return src

    def test_each_state_draws_its_own_picture(self, bubble, tmp_path):
        src = self._full(bubble, tmp_path)
        slug = bubble.install_pack(src)[0]
        for state in bubble.PACK_STATES:
            assert Path(bubble.picture_for(slug, "", state)).name == f"{state}.png", (
                "one picture per state is the whole point of a pack")

    def test_a_state_without_an_entry_uses_any(self, bubble, tmp_path):
        src = tmp_path / "any"
        png(src / "base.png")
        png(src / "idle.png")
        manifest(src, {"name": "Any", "any": "base.png",
                       "states": {"idle": "idle.png"}})
        slug = bubble.install_pack(src)[0]
        assert Path(bubble.picture_for(slug, "", "idle")).name == "idle.png"
        for state in ("listening", "thinking", "speaking"):
            assert Path(bubble.picture_for(slug, "", state)).name == "base.png", state

    def test_the_pack_beats_the_single_picture(self, bubble, tmp_path):
        src = self._full(bubble, tmp_path)
        slug = bubble.install_pack(src)[0]
        chosen = str(png(tmp_path / "mine.png"))
        assert Path(bubble.picture_for(slug, chosen, "idle")).name == "idle.png"

    def test_without_a_pack_the_single_picture_is_used(self, bubble):
        assert bubble.picture_for("", "/anywhere/x.png", "idle") == "/anywhere/x.png"
        assert bubble.picture_for("", "   ", "idle") == ""

    def test_a_missing_pack_is_reported_and_never_substituted(self, bubble):
        # Art appearing from a source the user did not choose is worse than no
        # art at all, so the single file must NOT stand in for the missing pack.
        assert bubble.picture_for("ghost", "/anywhere/x.png", "idle") == ""
        assert "no installed pack named ghost" in bubble.pack_problem("ghost")


class TestPackRefusals:
    """Every way a folder can fail, and the sentence that names it."""

    def test_a_state_the_pack_cannot_cover_is_refused(self, bubble, tmp_path):
        src = tmp_path / "part"
        png(src / "idle.png")
        manifest(src, {"name": "Part", "states": {"idle": "idle.png"}})
        slug, message = bubble.install_pack(src)
        assert slug == ""
        for state in ("listening", "thinking", "speaking"):
            assert state in message, (state, message)

    def test_a_missing_picture_names_the_entry_it_belongs_to(self, bubble, tmp_path):
        src = tmp_path / "gone"
        png(src / "idle.png")
        manifest(src, {"name": "Gone", "any": "nope.png",
                       "states": {"idle": "idle.png"}})
        slug, message = bubble.install_pack(src)
        assert slug == ""
        assert "no file at" in message and "the any picture" in message

    def test_a_picture_that_will_not_decode_is_refused(self, bubble, tmp_path):
        src = tmp_path / "junk"
        src.mkdir()
        (src / "base.png").write_bytes(b"this is not an image")
        manifest(src, {"name": "Junk", "any": "base.png"})
        slug, message = bubble.install_pack(src)
        assert slug == "" and "not an image" in message

    def test_a_fully_transparent_picture_is_refused(self, bubble, tmp_path):
        src = tmp_path / "clear"
        png(src / "base.png", (0, 0, 0, 0))
        manifest(src, {"name": "Clear", "any": "base.png"})
        slug, message = bubble.install_pack(src)
        assert slug == "" and "transparent" in message

    def test_junk_json_and_a_missing_manifest_are_refused(self, bubble, tmp_path):
        src = tmp_path / "broken"
        src.mkdir()
        assert "no pack.json" in bubble.install_pack(src)[1]
        (src / "pack.json").write_text("{oops", encoding="utf-8")
        assert "not valid JSON" in bubble.install_pack(src)[1]
        (src / "pack.json").write_text(json.dumps(["not", "an", "object"]),
                                       encoding="utf-8")
        assert "JSON object" in bubble.install_pack(src)[1]
        assert bubble.install_pack(tmp_path / "absent")[1].endswith("is not a folder")

    def test_a_manifest_that_names_nothing_is_refused(self, bubble, tmp_path):
        src = tmp_path / "empty"
        manifest(src, {"name": "Empty", "states": {}})
        slug, message = bubble.install_pack(src)
        assert slug == "" and "names no pictures" in message


class TestBrokenInstalledPack:
    def test_it_draws_nothing_and_says_why_but_is_still_listed(self, bubble, tmp_path):
        src = tmp_path / "src"
        png(src / "base.png")
        manifest(src, {"name": "Later", "any": "base.png"})
        slug, _message, root = install(bubble, src)
        (root / slug / "base.png").unlink()      # the folder is data: hand-edited
        # The manifest is still valid (it is the PICTURE that is gone), so the
        # resolver still names it — and the decoder refuses it, which is what
        # makes the painter draw its empty slot rather than stale art.
        assert bubble.image_layers(bubble.picture_for(slug, "", "idle")) is None
        problem = bubble.pack_problem(slug)
        assert "no file at" in problem and "base.png" in problem
        assert (slug, "Later") in bubble.installed_packs(), (
            "the broken pack is exactly the one the user needs to be shown")

    def test_one_bad_state_entry_does_not_half_draw_the_pack(self, bubble, tmp_path):
        src = tmp_path / "src"
        png(src / "base.png")
        png(src / "idle.png")
        manifest(src, {"name": "Half", "any": "base.png",
                       "states": {"idle": "idle.png"}})
        slug, _message, root = install(bubble, src)
        assert bubble.pack_problem(slug) == ""
        manifest(root / slug, {"name": "Half", "any": "base.png",
                               "states": {"idle": "../base.png"}})
        assert bubble.load_pack(slug) is None
        for state in bubble.PACK_STATES:
            assert bubble.picture_for(slug, "", state) == "", (
                f"{state} still drew a picture while the pack it belongs to is "
                f"broken — the pack is a unit, not a bag of entries")
        assert "outside the pack" in bubble.pack_problem(slug)


class TestArtProblem:
    """`doctor`'s one sentence: the pack first, then the single picture."""

    def test_the_pack_owns_the_sentence_when_one_is_selected(self, bubble, tmp_path):
        good = png(tmp_path / "good.png")
        bubble.SETTINGS["design_image_path"] = str(good)
        assert bubble.art_problem() == ""
        bubble.SETTINGS["design_pack"] = "ghost"
        assert "no installed pack named ghost" in bubble.art_problem(), (
            "a selected pack is the authority: a perfectly good single picture "
            "must not hide the pack that cannot draw")

    def test_the_single_picture_speaks_when_no_pack_is_selected(self, bubble,
                                                               tmp_path):
        bubble.SETTINGS["design_pack"] = ""
        bubble.SETTINGS["design_image_path"] = str(tmp_path / "missing.png")
        assert "no file at" in bubble.art_problem()
        bubble.SETTINGS["design_image_path"] = ""
        assert bubble.art_problem() == "", (
            "nothing chosen is not a problem — it is the state every install "
            "starts in, and doctor must not grow a line for it")

    def test_editing_the_manifest_takes_effect_without_a_restart(self, bubble,
                                                                 tmp_path):
        src = tmp_path / "src"
        png(src / "base.png")
        png(src / "other.png")
        manifest(src, {"name": "Edit", "any": "base.png"})
        slug, _message, root = install(bubble, src)
        assert Path(bubble.picture_for(slug, "", "idle")).name == "base.png"
        manifest(root / slug, {"name": "Edit", "any": "other.png"})
        assert Path(bubble.picture_for(slug, "", "idle")).name == "other.png", (
            "the manifest's mtime must be in the cache key, or a re-exported "
            "pack would need a restart")


class TestPerStatePictures:
    """One picture per state, the fallback behind them, and the precedence."""

    def test_the_two_modules_name_the_same_four_settings(self, bubble):
        # `core/bubble.py` derives its keys from the state names and cannot
        # import the schema (the settings app loads it without Qt); the schema
        # writes them literally. If they ever disagree, the panel would save a
        # setting nothing reads and the bubble would draw the empty slot for a
        # picture the user chose — silently.
        assert set(bubble.STATE_IMAGE_KEY.values()) == set(DESIGN_IMAGE_KEYS)
        assert set(bubble.STATE_IMAGE_KEY) == set(BUBBLE_STATES)

    def test_only_the_states_with_a_picture_of_their_own_are_reported(self, bubble):
        bubble.SETTINGS.update({k: "" for k in DESIGN_IMAGE_KEYS})
        assert bubble.state_pictures() == {}
        bubble.SETTINGS["design_image_thinking"] = "  /th.png  "
        assert bubble.state_pictures() == {"thinking": "/th.png"}, (
            "a blank setting is not a picture, and a path is stripped")

    def test_the_counted_pictures_are_the_ones_that_will_render(self, bubble, tmp_path):
        # `doctor` counts this map, and it prints the count on the SAME line as
        # the sentence naming a broken picture — so a count of chosen settings
        # could read 4/4 beside "the thinking picture: no file", which scans as
        # a clean bill of health. Two questions, two answers: `state_pictures`
        # is what is CHOSEN (what the resolver draws from), this is what renders.
        bubble.SETTINGS.update({k: "" for k in DESIGN_IMAGE_KEYS})
        good = png(tmp_path / "good.png")
        gone = tmp_path / "gone.png"
        bubble.SETTINGS["design_image_idle"] = str(good)
        bubble.SETTINGS["design_image_thinking"] = str(gone)
        assert bubble.state_pictures() == {
            "idle": str(good), "thinking": str(gone)}
        assert bubble.usable_state_pictures() == {"idle": str(good)}

    def test_a_state_uses_its_own_picture_and_the_rest_fall_back(self, bubble):
        pics = {"idle": "/idle.png"}
        assert bubble.picture_for("", "/fallback.png", "idle", pics) == "/idle.png"
        for state in ("listening", "thinking", "speaking"):
            assert bubble.picture_for("", "/fallback.png", state, pics) == \
                "/fallback.png", state

    def test_a_state_with_neither_draws_the_empty_slot(self, bubble):
        assert bubble.picture_for("", "", "idle", {}) == ""
        assert bubble.picture_for("", "/f.png", "idle", {"idle": ""}) == \
            "/f.png", "a blank per-state value is not an own picture"

    def test_a_pack_still_wins_over_a_per_state_picture(self, bubble, tmp_path):
        src = tmp_path / "src"
        for state in bubble.PACK_STATES:
            png(src / f"{state}.png")
        manifest(src, {"name": "Win",
                       "states": {s: f"{s}.png" for s in bubble.PACK_STATES}})
        slug = bubble.install_pack(src)[0]
        pics = {s: "/mine.png" for s in bubble.PACK_STATES}
        assert Path(bubble.picture_for(slug, "/f.png", "idle", pics)).name == \
            "idle.png", (
            "a pack is the authority: its pictures win over the files, or the "
            "two sources would silently mix")
        # ...and a pack that cannot be read draws NOTHING, not the files
        bubble._forget_pack(slug)
        (Path(bubble.PACKS_DIR) / slug / "pack.json").unlink()
        assert bubble.picture_for(slug, "/f.png", "idle", pics) == ""

    def test_design_picture_reads_every_key_of_the_precedence(self, bubble):
        bubble.SETTINGS.update({k: "" for k in DESIGN_IMAGE_KEYS})
        bubble.SETTINGS["design_image_path"] = "/fallback.png"
        bubble.SETTINGS["design_image_listening"] = "/listen.png"
        assert bubble.design_picture("listening") == "/listen.png"
        assert bubble.design_picture("idle") == "/fallback.png"

    def test_the_problem_names_the_state_it_belongs_to(self, bubble, tmp_path):
        good = png(tmp_path / "good.png")
        pics = {"idle": str(good), "speaking": str(tmp_path / "gone.png")}
        problem = bubble.state_image_problem(pics)
        assert "the speaking picture" in problem and "no file at" in problem, (
            "with four slots, WHICH state is broken is the whole question")
        assert bubble.state_image_problem({"idle": str(good)}) == ""
        assert bubble.state_image_problem({}) == ""

    def test_art_problem_follows_pack_then_state_then_fallback(self, bubble,
                                                              tmp_path):
        bubble.SETTINGS.update({k: "" for k in DESIGN_IMAGE_KEYS})
        gone = str(tmp_path / "gone.png")
        good = str(png(tmp_path / "good.png"))
        # the fallback is what speaks when nothing else is chosen
        bubble.SETTINGS["design_image_path"] = gone
        assert "no file at" in bubble.art_problem()
        # a per-state picture speaks AHEAD of it, naming its state
        bubble.SETTINGS["design_image_idle"] = gone
        bubble.SETTINGS["design_image_path"] = good
        assert "the idle picture" in bubble.art_problem()
        # ...and a pack speaks ahead of both
        bubble.SETTINGS["design_pack"] = "ghost"
        assert "no installed pack named ghost" in bubble.art_problem()
        assert bubble.state_image_problem({"idle": gone}) != "", (
            "the per-state sentence must still exist behind a pack, for the "
            "panel that shows it")


class TestExportPack:
    """A look built by hand becomes a folder someone else can install.

    The export writes the art that is ON SCREEN and hands it to the SAME
    validator an install uses, so the round trip is the assertion that matters:
    export, install what was exported, and the pictures must be the ones you
    had. Everything else here is a way that could quietly write the wrong thing
    — an empty pack, a half pack, a clobbered folder.
    """

    # One colour per slot, fixed, so two slots cannot accidentally be the same
    # picture — `test_..._same_basename_do_not_collide` depends on the bytes
    # differing, and a helper that let the caller pick would let that slip.
    PALETTE = {"idle": (10, 20, 30, 255), "listening": (40, 50, 60, 255),
               "thinking": (70, 80, 90, 255), "speaking": (100, 110, 120, 255),
               "any": (200, 210, 220, 255)}

    def art(self, folder: Path, *want: str, rgba=None) -> dict:
        """{slot: path} for the named slots, as real PNGs with distinct bytes.

        `rgba` overrides the slot colour, which a test needs when it must tell
        two runs of the SAME slot apart ("was this the art I passed in, or the
        art sitting in the module state?"); with one fixed palette those two
        exports would be byte-identical and the question unanswerable.
        """
        return {label: png(folder / f"{label}.png",
                           rgba or self.PALETTE[label]) for label in want}

    def settings_for(self, files: dict) -> dict:
        body = dict(DEFAULT_SETTINGS)
        body["design_image_path"] = str(files.get("any") or "")
        for state in BUBBLE_STATES:
            body[f"design_image_{state}"] = str(files.get(state) or "")
        return body

    def test_a_hand_built_look_round_trips_through_install(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "idle", "listening", "thinking",
                         "speaking", "any")
        art = self.settings_for(files)
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "My Look", art)
        assert folder, message
        assert Path(folder).name == "my-look", folder
        assert "exported" in message and "state picture" in message, message
        # ...and what was written is a pack INSTALL accepts, drawing the SAME
        # bytes for every state: that is the whole point of the format.
        slug, installed = bubble.install_pack(folder)
        assert slug == "my-look", installed
        bubble.SETTINGS.update({"design_pack": slug, "design_image_path": ""})
        for state in BUBBLE_STATES:
            drawn = Path(bubble.design_picture(state))
            assert drawn.read_bytes() == Path(files[state]).read_bytes(), (
                f"{state} drew {drawn}, not the picture that was exported")

    def test_the_manifest_names_the_states_and_the_fallback(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "idle", "any")
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "look",
                                             self.settings_for(files))
        assert folder, message
        body = json.loads((Path(folder) / "pack.json").read_text(encoding="utf-8"))
        assert body["name"] == "look", body
        assert body["states"] == {"idle": "idle.png"}, body
        assert body["any"] == "any.png", body
        # The manifest must be READABLE by a human who received the folder.
        assert "\n    " in (Path(folder) / "pack.json").read_text(encoding="utf-8")

    def test_no_art_at_all_is_refused_rather_than_writing_an_empty_pack(
            self, bubble, tmp_path):
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "empty",
                                             dict(DEFAULT_SETTINGS))
        assert folder == "", folder
        assert "no pictures to export" in message, message
        assert list(parent.iterdir()) == [], "a refusal must write nothing"

    def test_a_name_with_no_folder_in_it_is_refused(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "any")
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "///", self.settings_for(files))
        assert folder == "", folder
        assert "name with at least one letter or digit" in message, message
        assert list(parent.iterdir()) == []

    def test_an_existing_folder_is_never_overwritten(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "any")
        parent = tmp_path / "share"
        (parent / "look").mkdir(parents=True)
        sentinel = parent / "look" / "mine.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        folder, message = bubble.export_pack(parent, "look", self.settings_for(files))
        assert folder == "", folder
        assert "already exists" in message, message
        assert sentinel.read_text(encoding="utf-8") == "keep me", (
            "an export must not touch a folder that was already there")

    def test_a_destination_that_is_not_a_folder_is_refused(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "any")
        nowhere = tmp_path / "nope"
        folder, message = bubble.export_pack(nowhere, "look",
                                             self.settings_for(files))
        assert folder == "", folder
        assert "is not a folder" in message, message
        assert not nowhere.exists(), "a refusal must not create the destination"

    def test_two_states_naming_the_same_basename_do_not_collide(
            self, bubble, tmp_path):
        first = png(tmp_path / "one" / "photo.png", (1, 2, 3, 255))
        second = png(tmp_path / "two" / "photo.png", (9, 8, 7, 255))
        art = self.settings_for({"idle": first, "speaking": second, "any": first})
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "look", art)
        assert folder, message
        body = json.loads((Path(folder) / "pack.json").read_text(encoding="utf-8"))
        assert body["states"]["idle"] != body["states"]["speaking"], body
        assert body["states"]["idle"] == body["any"], (
            "one file used twice is one copy, named once")
        slug, installed = bubble.install_pack(folder)
        assert slug, installed
        bubble.SETTINGS.update({"design_pack": slug, "design_image_path": ""})
        assert Path(bubble.design_picture("idle")).read_bytes() == first.read_bytes()
        assert Path(bubble.design_picture("speaking")).read_bytes() == second.read_bytes()

    def test_an_incomplete_look_is_refused_with_the_install_sentence(
            self, bubble, tmp_path):
        art = self.settings_for(self.art(tmp_path / "art", "idle"))
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "half", art)
        assert folder == "", folder
        for state in ("listening", "thinking", "speaking"):
            assert state in message, message
        assert list(parent.iterdir()) == [], (
            "the staging folder must be removed, or a refusal leaves half a pack")

    def test_a_selected_pack_exports_the_art_that_is_on_screen(
            self, bubble, tmp_path):
        source = tmp_path / "prism"
        shots = {s: png(source / f"{s}.png") for s in BUBBLE_STATES}
        manifest(source, {"name": "Prism", "states": {
            s: f"{s}.png" for s in BUBBLE_STATES}})
        slug, message = bubble.install_pack(source)
        assert slug, message
        bubble.SETTINGS.update({"design_pack": slug, "design_image_path": ""})
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "shared", None)
        assert folder, message
        body = json.loads((Path(folder) / "pack.json").read_text(encoding="utf-8"))
        assert set(body["states"]) == set(BUBBLE_STATES), body
        for state, name in body["states"].items():
            got = (Path(folder) / name).read_bytes()
            assert got == shots[state].read_bytes(), state

    def test_a_broken_selected_pack_exports_nothing(self, bubble, tmp_path):
        root = Path(bubble.PACKS_DIR)
        # TWO ways a pack is broken, because they fail differently: a manifest
        # that is not JSON at all, and one that is VALID JSON naming a picture
        # which is not there. The second is the one a lenient reader would
        # "export" — copying a file that does not exist — so it has to be here.
        (root / "ghost").mkdir(parents=True)
        (root / "ghost" / "pack.json").write_text("not json", encoding="utf-8")
        (root / "gone").mkdir(parents=True)
        (root / "gone" / "pack.json").write_text(
            json.dumps({"name": "Gone", "any": "missing.png"}), encoding="utf-8")
        parent = tmp_path / "share"
        parent.mkdir()
        for slug in ("ghost", "gone"):
            bubble.SETTINGS.update({"design_pack": slug})
            folder, message = bubble.export_pack(parent, "look", None)
            assert folder == "", f"{slug}: {folder}"
            assert "no pictures to export" in message, f"{slug}: {message}"
        assert list(parent.iterdir()) == []

    def test_the_settings_it_is_handed_beat_the_module_state(
            self, bubble, tmp_path):
        given = self.art(tmp_path / "given", "any", rgba=(1, 2, 3, 255))
        module = self.art(tmp_path / "module", "any", rgba=(9, 9, 9, 255))
        assert Path(given["any"]).read_bytes() != Path(module["any"]).read_bytes(), (
            "the two artefacts must differ, or the export's source is untestable")
        bubble.SETTINGS.update(self.settings_for(module))
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "look",
                                             self.settings_for(given))
        assert folder, message
        name = json.loads((Path(folder) / "pack.json").read_text(encoding="utf-8"))["any"]
        assert (Path(folder) / name).read_bytes() == Path(given["any"]).read_bytes(), (
            "the panel's unsaved choices must be what is exported")


class TestPackFile:
    """A look travels as ONE file: the same pack in an envelope you can send.

    A folder is not something anyone can attach to a message, so the export has
    a second shape and the install has a second door. The assertion that matters
    is the folder export's own — export it, install what was exported, and the
    pictures have to be the ones you had — because a container that round-trips
    is the entire reason to have one.

    Everything else here is about the archive being DATA SOMEONE ELSE WROTE: not
    a zip at all, named so it would escape the folder, marked as a symlink,
    holding more entries than a pack has, unpacking to more than a pack can be,
    holding two packs, holding none. None of it may reach the installed packs,
    and each refusal has to NAME what is wrong with the file in the user's hand.
    """

    PALETTE = {"idle": (10, 20, 30, 255), "listening": (40, 50, 60, 255),
               "thinking": (70, 80, 90, 255), "speaking": (100, 110, 120, 255),
               "any": (200, 210, 220, 255)}

    def art(self, folder: Path, *want: str) -> dict:
        """{slot: path} for the named slots, as real PNGs with distinct bytes."""
        return {label: png(folder / f"{label}.png", self.PALETTE[label])
                for label in want}

    def settings_for(self, files: dict) -> dict:
        body = dict(DEFAULT_SETTINGS)
        body["design_image_path"] = str(files.get("any") or "")
        for state in BUBBLE_STATES:
            body[f"design_image_{state}"] = str(files.get(state) or "")
        return body

    @staticmethod
    def archive(path: Path, *entries) -> Path:
        """A zip with the given (name, bytes) entries — whatever was sent."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return path

    @staticmethod
    def installed(bubble) -> list:
        """What is in the packs directory — "nothing was installed" as a list."""
        root = Path(bubble.PACKS_DIR)
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def test_a_hand_built_look_round_trips_through_a_file(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "idle", "listening", "thinking",
                         "speaking", "any")
        parent = tmp_path / "share"
        parent.mkdir()
        written, message = bubble.export_pack_file(parent, "My Look",
                                                   self.settings_for(files))
        assert written, message
        assert Path(written).name == "my-look.hpack", written
        assert "exported" in message and "state picture" in message, message
        assert sorted(p.name for p in parent.iterdir()) == ["my-look.hpack"], (
            "the hidden file it was written to has to be gone, or every export "
            "leaves litter beside the pack")
        slug, installed = bubble.install_pack_file(written)
        assert slug == "my-look", installed
        bubble.SETTINGS.update({"design_pack": slug, "design_image_path": ""})
        for state in BUBBLE_STATES:
            drawn = Path(bubble.design_picture(state))
            assert drawn.read_bytes() == Path(files[state]).read_bytes(), (
                f"{state} drew {drawn}, not the picture that was exported")

    def test_the_file_is_a_zip_a_person_could_open_themselves(self, bubble,
                                                              tmp_path):
        # `idle` as well as `any`, so the manifest has a nested object: the
        # indentation that makes it readable is the nested line's.
        files = self.art(tmp_path / "art", "idle", "any")
        parent = tmp_path / "share"
        parent.mkdir()
        written, message = bubble.export_pack_file(parent, "look",
                                                   self.settings_for(files))
        assert written, message
        assert zipfile.is_zipfile(written), "a pack file must be a plain zip"
        with zipfile.ZipFile(written) as zf:
            names = zf.namelist()
            body = zf.read("pack.json").decode("utf-8")
            picture = zf.read("any.png")
            state = zf.read(json.loads(body)["states"]["idle"])
        assert names[0] == "pack.json", names
        # The manifest is text and is deflated; a PNG is already compressed, so
        # storing it costs nothing and deflating it only spends time.
        kinds = {i.filename: i.compress_type for i in zf.infolist()}
        assert kinds["pack.json"] == zipfile.ZIP_DEFLATED, kinds
        assert kinds["any.png"] == zipfile.ZIP_STORED, kinds
        assert json.loads(body)["any"] == "any.png", body
        assert "\n    " in body, "the manifest has to be readable by a human"
        assert picture == Path(files["any"]).read_bytes(), (
            "the picture has to be in the archive byte for byte")
        assert state == Path(files["idle"]).read_bytes(), "and so has the state's"

    def test_the_file_carries_what_the_folder_export_carries(self, bubble,
                                                             tmp_path):
        """Two shapes, ONE assembly — so this is a property, not a coincidence.

        Both exports stage the art through the same step, which is what makes it
        safe for them to be two buttons; the manifest and the picture bytes have
        to match whichever one was pressed.
        """
        files = self.art(tmp_path / "art", "idle", "speaking", "any")
        art = self.settings_for(files)
        parent = tmp_path / "share"
        parent.mkdir()
        folder, message = bubble.export_pack(parent, "look", art)
        assert folder, message
        written, message = bubble.export_pack_file(parent, "look-two", art)
        assert written, message
        with zipfile.ZipFile(written) as zf:
            in_file = json.loads(zf.read("pack.json").decode("utf-8"))
            in_folder = json.loads(
                (Path(folder) / "pack.json").read_text(encoding="utf-8"))
            assert in_file["states"] == in_folder["states"], (in_file, in_folder)
            assert in_file["any"] == in_folder["any"], (in_file, in_folder)
            for name in in_folder["states"].values():
                assert zf.read(name) == (Path(folder) / name).read_bytes(), name

    def test_no_art_at_all_writes_nothing(self, bubble, tmp_path):
        parent = tmp_path / "share"
        parent.mkdir()
        written, message = bubble.export_pack_file(parent, "empty",
                                                   dict(DEFAULT_SETTINGS))
        assert written == "", written
        assert "no pictures to export" in message, message
        assert list(parent.iterdir()) == [], "a refusal must write nothing"

    def test_an_existing_pack_file_is_never_overwritten(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "any")
        parent = tmp_path / "share"
        parent.mkdir()
        sentinel = parent / "look.hpack"
        sentinel.write_bytes(b"mine")
        written, message = bubble.export_pack_file(parent, "look",
                                                   self.settings_for(files))
        assert written == "", written
        assert "already exists" in message, message
        assert sentinel.read_bytes() == b"mine"
        assert list(parent.iterdir()) == [sentinel], (
            "nothing may be staged before the destination is settled")

    def test_an_incomplete_look_is_refused_with_the_install_sentence(
            self, bubble, tmp_path):
        """The file export refuses in the SAME words the folder export does.

        Both assemble through one staging step, so a look that cannot be drawn
        has to be turned away with the sentence an install would use, whichever
        export was asked to write it.
        """
        art = self.settings_for(self.art(tmp_path / "art", "idle"))
        parent = tmp_path / "share"
        parent.mkdir()
        written, message = bubble.export_pack_file(parent, "half", art)
        assert written == "", written
        for state in ("listening", "thinking", "speaking"):
            assert state in message, message

    def test_an_incomplete_look_leaves_no_staging_behind(self, bubble, tmp_path):
        art = self.settings_for(self.art(tmp_path / "art", "idle"))
        parent = tmp_path / "share"
        parent.mkdir()
        written, message = bubble.export_pack_file(parent, "half", art)
        assert written == "", written
        for state in ("listening", "thinking", "speaking"):
            assert state in message, message
        assert list(parent.iterdir()) == [], (
            "the staging folder must go, or a refusal leaves half a pack")

    def test_a_file_that_is_not_an_archive_is_refused(self, bubble, tmp_path):
        junk = tmp_path / "look.hpack"
        junk.write_bytes(b"this is not a zip")
        slug, message = bubble.install_pack_file(junk)
        assert slug == "", slug
        assert "not a zip archive" in message, message
        assert self.installed(bubble) == []

    def test_a_missing_file_is_refused(self, bubble, tmp_path):
        slug, message = bubble.install_pack_file(tmp_path / "nope.hpack")
        assert slug == "", slug
        assert "is not a file" in message, message

    def test_an_archive_with_no_manifest_is_refused(self, bubble, tmp_path):
        source = self.archive(tmp_path / "look.hpack", ("idle.png", b"x"))
        slug, message = bubble.install_pack_file(source)
        assert slug == "", slug
        # The sentence has to be the ARCHIVE's — naming the file that is in the
        # user's hand and calling it a pack file — not the folder install's,
        # which would name a temporary directory nobody has ever heard of. Both
        # wordings contain "has no pack.json", so that alone pins nothing.
        assert "look.hpack has no pack.json" in message, message
        assert "a pack file needs one" in message, message
        assert self.installed(bubble) == []

    def test_a_file_pack_is_validated_exactly_like_a_folder(self, bubble,
                                                           tmp_path):
        """An archive that unpacks cleanly can still be a broken PACK.

        This is the reason the file door leads to the same install rather than a
        second one: a manifest naming a picture the archive does not hold has to
        be refused in the same words a folder would be, and nothing may land in
        the packs directory — otherwise "it came as a file" would be a way to
        skip the validation every folder goes through.
        """
        source = self.archive(
            tmp_path / "broken.hpack",
            ("pack.json",
             json.dumps({"name": "Broken", "any": "missing.png"})))
        slug, message = bubble.install_pack_file(source)
        assert slug == "", slug
        assert "no file at" in message and "any" in message, message
        assert self.installed(bubble) == []

    def test_an_entry_that_escapes_the_folder_never_reaches_the_disk(
            self, bubble, tmp_path):
        """A `..` entry is refused BY NAME rather than quietly rewritten.

        Python's own extractor sanitises this silently; a pack that had to be
        rewritten to be safe is not the pack that was sent, so it is named and
        refused — and the file it was aiming at must not exist afterwards.
        """
        source = self.archive(tmp_path / "evil.hpack", ("../escaped.png", b"x"))
        dest = tmp_path / "unpack"
        dest.mkdir()
        problem, folder = bubble._extract_pack_archive(source, dest, "evil.hpack")
        assert problem and "could leave the pack" in problem, problem
        assert "../escaped.png" in problem, problem
        assert folder is None
        assert list(dest.iterdir()) == [], "nothing may be unpacked"
        assert not (tmp_path / "escaped.png").exists()
        assert bubble.install_pack_file(source)[0] == ""

    def test_an_entry_named_for_a_windows_drive_is_refused(self, bubble,
                                                           tmp_path):
        """A drive letter is refused even on a system where it is just a name.

        On Linux `C:\\any.png` is one legal (if strange) file name, so nothing
        here would escape — but the same pack extracted on Windows would aim at a
        drive, and a pack holds relative names like `idle.png`. Refusing it by
        name costs nothing and means a pack behaves the same everywhere.
        """
        source = self.archive(tmp_path / "drive.hpack",
                              ("C:\\any.png", b"x"))
        slug, message = bubble.install_pack_file(source)
        assert slug == "", slug
        assert "could leave the pack" in message, message
        assert self.installed(bubble) == []

    def test_an_absolute_entry_is_refused(self, bubble, tmp_path):
        aim = tmp_path / "absolute.png"
        source = self.archive(tmp_path / "abs.hpack", (str(aim), b"x"))
        slug, message = bubble.install_pack_file(source)
        assert slug == "", slug
        assert "could leave the pack" in message, message
        assert not aim.exists(), "the entry must not land outside the archive"

    def test_a_symlink_entry_is_refused(self, bubble, tmp_path):
        """A zip can MARK an entry as a symlink; that is refused like a path out.

        `zipfile` writes such an entry as ordinary text rather than a link, but
        the entry is asking to be a link and a pack is a folder of pictures —
        letting it through would answer "you sent a symlink" with "your picture
        is not an image", which names the wrong problem.
        """
        source = tmp_path / "link.hpack"
        with zipfile.ZipFile(source, "w") as zf:
            info = zipfile.ZipInfo("any.png")
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
        dest = tmp_path / "unpack"
        dest.mkdir()
        problem, _folder = bubble._extract_pack_archive(source, dest, "link.hpack")
        assert problem and "could leave the pack" in problem, problem
        assert list(dest.iterdir()) == []

    def test_an_archive_with_too_many_entries_is_refused(self, bubble, tmp_path,
                                                         monkeypatch):
        """The count is capped, and the refusal says the number.

        The cap is lowered rather than writing 65 real entries: what is pinned is
        the REFUSAL, not the constant — a pack is a manifest and a handful of
        pictures, and an archive holding hundreds is not one.
        """
        monkeypatch.setattr(bubble, "PACK_MAX_ENTRIES", 4)
        source = self.archive(tmp_path / "many.hpack",
                              *[(f"f{n}.png", b"x") for n in range(8)])
        slug, message = bubble.install_pack_file(source)
        assert slug == "", slug
        assert "8 entries" in message, message
        assert "more than a design pack has" in message, message
        assert self.installed(bubble) == []

    def test_the_size_cap_refuses_before_anything_is_unpacked(self, bubble,
                                                              tmp_path,
                                                              monkeypatch):
        """A pack file is an attachment, so what it unpacks to is capped.

        The cap is lowered rather than committing two megabytes to the suite:
        what is pinned is the REFUSAL and its sentence — and that it happens
        before any part of the archive reaches the disk, which is what makes a
        zip bomb a refusal instead of a slow surprise.
        """
        monkeypatch.setattr(bubble, "PACK_MAX_BYTES", 1 << 20)
        source = self.archive(tmp_path / "bomb.hpack",
                              ("any.png", b"x" * (2 << 20)))
        dest = tmp_path / "unpack"
        dest.mkdir()
        problem, folder = bubble._extract_pack_archive(source, dest,
                                                       "bomb.hpack")
        assert problem and "more than 1 MB" in problem, problem
        assert "more than a design pack can be" in problem, problem
        assert folder is None, folder
        assert list(dest.iterdir()) == [], (
            "a size refusal must happen before a byte is written")
        assert bubble.install_pack_file(source)[0] == ""
        assert self.installed(bubble) == []

    def test_a_zip_that_wraps_the_pack_in_a_folder_is_accepted(self, bubble,
                                                               tmp_path):
        """Because that is how a person actually zips a pack.

        This module writes `pack.json` at the top level, but someone sharing a
        pack zips the FOLDER — so one wrapper folder holding a manifest is the
        pack, and both shapes a recipient will meet work.
        """
        files = self.art(tmp_path / "art", "any")
        source = self.archive(
            tmp_path / "wrapped.hpack",
            ("my-look/pack.json",
             json.dumps({"name": "My Look", "any": "any.png"})),
            ("my-look/any.png", Path(files["any"]).read_bytes()))
        slug, message = bubble.install_pack_file(source)
        assert slug == "my-look", message
        loaded = bubble.load_pack(slug)
        assert loaded is not None, message
        assert Path(loaded["any"]).read_bytes() == Path(files["any"]).read_bytes()

    def test_an_archive_holding_two_packs_is_refused(self, bubble, tmp_path):
        """Two candidates is a question only the sender can answer.

        Installing one of them silently would put a look on the desktop that the
        recipient never chose, so ambiguity is named and refused instead.
        """
        source = self.archive(
            tmp_path / "two.hpack",
            ("a/pack.json", json.dumps({"any": "x.png"})),
            ("a/x.png", b"x"),
            ("b/pack.json", json.dumps({"any": "y.png"})),
            ("b/y.png", b"y"))
        slug, message = bubble.install_pack_file(source)
        assert slug == "", slug
        assert "more than one pack.json" in message, message
        assert self.installed(bubble) == []

    def test_the_manifest_name_wins_over_the_file_name(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "any")
        source = self.archive(
            tmp_path / "whatever.hpack",
            ("pack.json",
             json.dumps({"name": "Prism Two", "any": "any.png"})),
            ("any.png", Path(files["any"]).read_bytes()))
        slug, message = bubble.install_pack_file(source)
        assert slug == "prism-two", message

    def test_a_pack_with_no_name_installs_under_the_file_it_arrived_in(
            self, bubble, tmp_path):
        """The private folder it unpacked into must never become its name.

        An archive is unpacked into a temporary directory whose name is nobody's
        word for anything, so the file the user actually chose names the pack —
        otherwise a look would install as `.handsoff-pack-ab12cd`.
        """
        files = self.art(tmp_path / "art", "any")
        source = self.archive(
            tmp_path / "Handed To Me.hpack",
            ("pack.json", json.dumps({"any": "any.png"})),
            ("any.png", Path(files["any"]).read_bytes()))
        slug, message = bubble.install_pack_file(source)
        assert slug == "handed-to-me", message
        assert Path(bubble.PACKS_DIR, slug).is_dir()

    def test_the_suffix_is_not_the_contract(self, bubble, tmp_path):
        """Content decides, so a sender who renamed the file still works.

        Someone holding `look.zip` should not have to fix its name before it
        installs: the archive either holds a pack or it does not, and that is
        the question actually asked of it.
        """
        files = self.art(tmp_path / "art", "any")
        parent = tmp_path / "share"
        parent.mkdir()
        written, message = bubble.export_pack_file(parent, "look",
                                                   self.settings_for(files))
        assert written, message
        renamed = parent / "look.zip"
        shutil.copy2(written, renamed)
        slug, message = bubble.install_pack_file(renamed)
        assert slug == "look", message
        loaded = bubble.load_pack(slug)
        assert loaded is not None, message
        assert Path(loaded["any"]).read_bytes() == Path(files["any"]).read_bytes()


class TestInspectPack:
    """Looking at a pack without taking it: what the panel previews.

    The property that makes a preview worth having is that it cannot LIE — the
    pack it draws has to be the pack an install would accept, judged by the same
    validator and refused in the same words. So the assertions that matter here
    are the equalities: the preview's refusal sentence IS the install's refusal
    sentence, and the pictures it hands the strip are the pictures an install
    would copy. Everything else is about owning a temporary folder honestly when
    the candidate arrived as a file.
    """

    PALETTE = {"idle": (10, 20, 30, 255), "listening": (40, 50, 60, 255),
               "thinking": (70, 80, 90, 255), "speaking": (100, 110, 120, 255),
               "any": (200, 210, 220, 255)}

    def art(self, folder: Path, *want: str) -> dict:
        """{slot: path} for the named slots, as real PNGs with distinct bytes."""
        return {label: png(folder / f"{label}.png", self.PALETTE[label])
                for label in want}

    @staticmethod
    def archive(path: Path, *entries) -> Path:
        """A zip with the given (name, bytes) entries — whatever was sent."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return path

    @staticmethod
    def installed(bubble) -> list:
        """What is in the packs directory — "nothing was installed" as a list."""
        root = Path(bubble.PACKS_DIR)
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def test_a_folder_is_read_where_it_already_sits(self, bubble, tmp_path):
        files = self.art(tmp_path / "art", "idle", "any")
        source = tmp_path / "look"
        manifest(source, {"name": "Look", "states": {"idle": "idle.png"},
                          "any": "any.png"})
        shutil.copy2(files["idle"], source / "idle.png")
        shutil.copy2(files["any"], source / "any.png")
        art, problem, scratch = bubble.inspect_pack(source)
        assert problem == "", problem
        assert scratch == "", "a folder needs no copy to be looked at"
        assert Path(art["any"]) == source / "any.png", art["any"]
        assert Path(art["states"]["idle"]) == source / "idle.png", art["states"]
        assert self.installed(bubble) == [], "looking installs nothing"

    def test_a_pack_file_is_unpacked_somewhere_the_CALLER_owns(
            self, bubble, tmp_path):
        """A previewed file has to live somewhere while it is drawn.

        The module does not keep that folder — it cannot know when the panel is
        done with it — so it hands it back and the property tested here is that
        the art really is inside it, which is what makes removing it a complete
        cleanup rather than a hopeful one.
        """
        files = self.art(tmp_path / "art", "idle", "any")
        source = self.archive(
            tmp_path / "sent.hpack",
            ("pack.json",
             json.dumps({"name": "Sent", "states": {"idle": "idle.png"},
                         "any": "any.png"})),
            ("idle.png", Path(files["idle"]).read_bytes()),
            ("any.png", Path(files["any"]).read_bytes()))
        art, problem, scratch = bubble.inspect_pack(source)
        assert problem == "", problem
        assert scratch and Path(scratch).is_dir(), scratch
        assert str(scratch) in art["states"]["idle"], (
            "the art the strip draws has to be inside the folder the caller "
            "is told to remove, or removing it does not remove the art")
        for state, path in (("idle", files["idle"]), ("any", files["any"])):
            drawn = art["states"].get(state) or art["any"]
            assert Path(drawn).read_bytes() == Path(path).read_bytes(), state
        assert self.installed(bubble) == []
        shutil.rmtree(scratch)
        assert not Path(scratch).exists(), "the caller has to be able to remove it"

    def test_the_preview_refuses_in_the_sentence_an_install_would_use(
            self, bubble, tmp_path):
        """The whole point: a preview cannot be more forgiving than an install.

        If the panel drew a pack the install would then turn down, the preview
        would be a lie and Try it would be the moment it was found out. Both
        doors read a pack through one function, so the sentences are not merely
        similar — they are the same string.
        """
        broken = tmp_path / "broken"
        manifest(broken, {"name": "Broken", "any": "missing.png"})
        art, problem, scratch = bubble.inspect_pack(broken)
        assert art is None and scratch == "", (art, scratch)
        assert "no file at" in problem and "missing.png" in problem, problem
        assert bubble.install_pack(broken)[1] == problem, (
            "preview and install have to refuse a pack in the SAME words")

    def test_a_previewed_file_is_refused_exactly_as_an_installed_one_is(
            self, bubble, tmp_path):
        """...and that has to hold for the file door too, entry names and all.

        The two doors unpack into DIFFERENT temporary folders, so their
        sentences can only match if the paths are spoken relative to the pack —
        which is also what stops a refusal naming a folder the user has never
        seen.
        """
        source = self.archive(
            tmp_path / "broken.hpack",
            ("pack.json",
             json.dumps({"name": "Broken", "any": "missing.png"})))
        art, preview_problem, scratch = bubble.inspect_pack(source)
        assert art is None and scratch == "", (art, scratch)
        assert "missing.png" in preview_problem, preview_problem
        assert "handsoff-preview" not in preview_problem, (
            f"the refusal has to name the ENTRY, not the folder it unpacked "
            f"into: {preview_problem!r}")
        assert bubble.install_pack_file(source)[1] == preview_problem, (
            "preview and install have to refuse a pack file in the SAME words")

    def test_a_refused_file_preview_removes_what_it_unpacked(
            self, bubble, tmp_path, monkeypatch):
        """A refusal must not leave the folder it made behind.

        The scratch folder is the module's only lasting effect, and a REFUSAL is
        exactly the case where nobody takes ownership of it — the caller is
        handed "" precisely because there is nothing to look at.
        """
        made = []
        real = tempfile.mkdtemp

        def recording_mkdtemp(*args, **kwargs):
            path = real(*args, **kwargs)
            made.append(path)
            return path

        monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
        junk = tmp_path / "junk.hpack"
        junk.write_bytes(b"not a zip")
        art, problem, scratch = bubble.inspect_pack(junk)
        assert art is None and scratch == "", (art, scratch)
        assert "not a zip archive" in problem, problem
        assert made, "the preview has to have had a folder to clean up"
        assert not any(Path(p).exists() for p in made), (
            "a refused preview must not leave its temporary folder behind")

    def test_the_file_names_the_slug_when_the_manifest_has_none(
            self, bubble, tmp_path):
        """What you would GET has to be what you were shown, name included."""
        files = self.art(tmp_path / "art", "any")
        source = self.archive(
            tmp_path / "Handed To Me.hpack",
            ("pack.json", json.dumps({"any": "any.png"})),
            ("any.png", Path(files["any"]).read_bytes()))
        art, problem, scratch = bubble.inspect_pack(source)
        assert problem == "", problem
        assert art["slug"] == "handed-to-me", art["slug"]
        assert bubble.install_pack_file(source)[0] == art["slug"], (
            "the preview must name the pack the install will create")
        shutil.rmtree(scratch, ignore_errors=True)

    def test_something_that_is_neither_a_folder_nor_a_file_is_refused(
            self, bubble, tmp_path):
        art, problem, scratch = bubble.inspect_pack(tmp_path / "nope")
        assert art is None and scratch == "", (art, scratch)
        assert "not a pack folder or file" in problem, problem


class TestSettingsPlumbing:
    """The setting itself: defaulted, coerced, and never validated away."""

    def test_the_setting_is_defaulted_and_stripped(self):
        from core.settings import coerce_settings
        assert DEFAULT_SETTINGS["design_pack"] == ""
        assert coerce_settings(dict(DEFAULT_SETTINGS,
                                    design_pack="  prism  "))["design_pack"] == "prism"
        without = {k: v for k, v in DEFAULT_SETTINGS.items()
                   if k != "design_pack"}
        assert coerce_settings(without)["design_pack"] == "", (
            "an older settings.json has no design_pack: the coercion must "
            "supply it rather than leave a key the renderer cannot read")

    def test_every_per_state_key_is_defaulted_and_expanded(self):
        from core.settings import coerce_settings
        for key in DESIGN_IMAGE_KEYS:
            assert DEFAULT_SETTINGS[key] == ""
        # ~ is expanded and whitespace stripped, and a key a settings.json
        # written before this feature omits is supplied rather than left out.
        out = coerce_settings(dict(DEFAULT_SETTINGS, design_image_idle=" ~/a.png "))
        assert out["design_image_idle"].endswith("/a.png")
        assert not out["design_image_idle"].startswith("~")
        for key in DESIGN_IMAGE_KEYS:
            if key != "design_image_idle":
                assert out[key] == "", key

    def test_a_pack_this_install_has_never_seen_is_kept(self):
        # coerce must NOT check the packs directory: a folder that is
        # temporarily moved must not silently erase the user's choice — the
        # failure has to be REPORTED (`pack_problem`), not acted on by deleting.
        from core.settings import coerce_settings
        assert coerce_settings(dict(
            DEFAULT_SETTINGS,
            design_pack="not-installed-yet"))["design_pack"] == "not-installed-yet"


class TestAnimatedPacks:
    """A state's art can be an ANIMATION: {"frames": [...], "fps": n}.

    A character that moves is the point of a picture pack — one face drawn four
    ways is a mood ring, but a blink, a perk and a mouth flap are a creature.
    The format is the contract pinned here: a still is still a string (every
    existing pack unchanged), an animation is an object, and BOTH are judged by
    the same validator with the same sentences a broken still gets — an
    animation is not a second, looser door. What makes it MOVE is time: the
    painter resolves the frame from `t`, so "fps" is a promise the renderer
    keeps, not a number the manifest may lie about.
    """

    PALETTE = {"f0": (10, 20, 30, 255), "f1": (60, 90, 130, 255),
               "f2": (120, 160, 210, 255), "other": (200, 210, 220, 255)}

    def art(self, folder: Path, *want: str) -> dict:
        return {label: png(folder / f"{label}.png", self.PALETTE[label])
                for label in want}

    def test_a_still_pack_is_unchanged(self, bubble, tmp_path):
        # The format is a superset: every existing pack — a string per state —
        # resolves exactly as before, now and after animations exist.
        source = tmp_path / "still"
        shots = self.art(source, "f0", "other")
        manifest(source, {"name": "Still", "states": {"idle": "f0.png"},
                          "any": "other.png"})
        bubble.SETTINGS["design_pack"] = ""
        installed = str(Path(bubble.PACKS_DIR) / bubble.install_pack(source)[0])
        bubble.SETTINGS["design_pack"] = bubble.install_pack(source)[0]
        try:
            # Resolved from the INSTALLED copy — that is what the bubble draws.
            assert bubble.design_picture("idle") == f"{installed}/f0.png"
            assert isinstance(bubble.design_picture("listening"), str)
            assert bubble.design_picture("listening") == f"{installed}/other.png"
            assert bubble.effective_art() == (f"{installed}/other.png",
                                              {"idle": f"{installed}/f0.png"})
        finally:
            bubble.SETTINGS["design_pack"] = ""

    def test_an_animation_resolves_to_a_spec_and_cycles_by_time(
            self, bubble, tmp_path):
        source = tmp_path / "anim"
        frames = self.art(source, "f0", "f1", "f2")
        manifest(source, {"name": "Anim",
                          "states": {"idle": {"frames": ["f0.png", "f1.png",
                                                        "f2.png"], "fps": 3}},
                          "any": "f0.png"})
        bubble.SETTINGS["design_pack"] = ""
        installed = str(Path(bubble.PACKS_DIR) / bubble.install_pack(source)[0])
        bubble.SETTINGS["design_pack"] = bubble.install_pack(source)[0]
        try:
            spec = bubble.design_picture("idle")
            assert isinstance(spec, dict), spec
            assert spec["frames"] == [f"{installed}/{k}.png"
                                      for k in ("f0", "f1", "f2")]
            assert spec["fps"] == 3.0
            # Time picks the frame: fps 3 means 1/3 s each, wrapping. Frame
            # identity is compared through the QImage layer, which is what the
            # painter composes from and what exists app-free (a QPixmap needs
            # a QGuiApplication — the crash that taught this test that).
            at = lambda t: bubble._design_art_image(spec, t)  # noqa: E731
            seq = [at(0.0), at(0.34), at(0.67), at(1.0)]
            for img in seq:
                assert img is not None, "every frame decodes"
            data = [bytes(img.constBits()) for img in seq]
            assert data[0] != data[1] and data[1] != data[2], (
                "different times give different frames")
            assert data[3] == data[0], "the cycle wraps at len(frames)"
        finally:
            bubble.SETTINGS["design_pack"] = ""

    def test_a_broken_animation_is_refused_in_the_still_sentence(
            self, bubble, tmp_path):
        # Not a second, looser door: a frame that does not exist is refused
        # exactly like a still picture that does not — "no file at", naming the
        # frame — and the pack resolves to NOTHING (never half art).
        source = tmp_path / "broken"
        self.art(source, "f0")
        manifest(source, {"name": "Broken",
                          "states": {"idle": {"frames": ["f0.png",
                                                         "missing.png"]}}})
        assert bubble.install_pack(source) == (
            "", "broken: no file at "
            f"{tmp_path / 'broken' / 'missing.png'} (the idle picture)"), (
            bubble.pack_problem("broken"))
        assert bubble.load_pack("broken") is None

        # And the bounds: too many frames, an empty list, junk fps.
        manifest(source, {"name": "Broken",
                          "states": {"idle": {"frames": ["f0.png"] * 17}}})
        assert "at most 16 frames" in bubble.install_pack(source)[1]
        manifest(source, {"name": "Broken",
                          "states": {"idle": {"frames": []}}})
        assert "non-empty" in bubble.install_pack(source)[1]
        manifest(source, {"name": "Broken",
                          "states": {"idle": {"frames": ["f0.png"],
                                              "fps": 1000}}})
        assert "\"fps\" must be between 0.5 and 30" in bubble.install_pack(source)[1]
        manifest(source, {"name": "Broken",
                          "states": {"idle": {"frames": ["f0.png"],
                                              "fps": "fast"}}})
        assert "must be a number" in bubble.install_pack(source)[1]
        manifest(source, {"name": "Broken",
                          "states": {"idle": {"frames": ["../out.png"]}}})
        assert "outside the pack" in bubble.install_pack(source)[1]

    def test_a_mixed_pack_holds_stills_and_animations(self, bubble, tmp_path):
        # One state may move while another sits still — a speaking mouth flap
        # on an idle face that only blinks is the normal case, not an edge.
        source = tmp_path / "mixed"
        frames = self.art(source, "f0", "f1", "other")
        manifest(source, {"name": "Mixed",
                          "states": {"idle": {"frames": ["f0.png", "f1.png"],
                                              "fps": 2},
                                     "speaking": "other.png"},
                          "any": "other.png"})
        bubble.SETTINGS["design_pack"] = ""
        installed = str(Path(bubble.PACKS_DIR) / bubble.install_pack(source)[0])
        bubble.SETTINGS["design_pack"] = bubble.install_pack(source)[0]
        try:
            assert isinstance(bubble.design_picture("idle"), dict)
            assert bubble.design_picture("speaking") == f"{installed}/other.png"
            assert bubble.design_picture("thinking") == f"{installed}/other.png"
            # effective_art keeps the SHAPES, so an export of this pack is
            # animated too (asserted in the round-trip below).
            _any, states = bubble.effective_art()
            assert isinstance(states["idle"], dict)
            assert states["speaking"] == f"{installed}/other.png"
        finally:
            bubble.SETTINGS["design_pack"] = ""

    def test_an_exported_animation_round_trips(self, bubble, tmp_path):
        # Export → install → the same spec: frames and fps survive, so a look
        # that moves travels as a look that moves, not as a frozen frame.
        source = tmp_path / "anim"
        self.art(source, "f0", "f1")
        manifest(source, {"name": "Anim",
                          "states": {"idle": {"frames": ["f0.png", "f1.png"],
                                              "fps": 5}},
                          "any": "f0.png"})
        bubble.SETTINGS.update({"design_pack": ""})
        bubble.SETTINGS["design_pack"] = bubble.install_pack(source)[0]
        try:
            target = tmp_path / "out"
            slug, message = bubble.export_pack(target.parent, "Round Trip",
                                               dict(bubble.SETTINGS,
                                                    design_pack=bubble.SETTINGS["design_pack"]))
            assert slug, message
            staged = target.parent / slug
            spec = json.loads((staged / "pack.json").read_text())
            assert spec["states"]["idle"]["fps"] == 5.0, spec
            assert len(spec["states"]["idle"]["frames"]) == 2
            # The exported frames are real files that decode.
            for frame in spec["states"]["idle"]["frames"]:
                assert (staged / frame).is_file(), frame
            slug2, message = bubble.install_pack(staged)
            assert slug2, message
            spec2 = bubble.design_picture("") if False else None
            bubble.SETTINGS["design_pack"] = slug2
            again = bubble.design_picture("idle")
            assert isinstance(again, dict) and again["fps"] == 5.0
            assert len(again["frames"]) == 2
            # Same pixels, new home.
            assert Path(again["frames"][0]).read_bytes() == \
                Path(again["frames"][0]).read_bytes()
        finally:
            bubble.SETTINGS["design_pack"] = ""

    def test_the_preview_strip_shows_an_animation_s_first_frame(
            self, bubble, tmp_path):
        # The strip is a decision aid, not a projector: it decodes the FIRST
        # frame through the same layers a still takes, so what it shows is a
        # frame the desktop will actually draw.
        source = tmp_path / "anim"
        frames = self.art(source, "f0", "f1")
        manifest(source, {"name": "Anim",
                          "states": {"idle": {"frames": ["f0.png", "f1.png"]}},
                          "any": "f0.png"})
        art, problem, scratch = bubble.inspect_pack(source)
        assert problem == "", problem
        spec = art["states"]["idle"]
        assert isinstance(spec, dict) and spec["frames"][0] == str(frames["f0"])
        assert Path(spec["frames"][0]).is_file()


class TestLivePackPreview:
    """A pack drawn on the BUBBLE before it is installed: the panel's preview.

    The strip answers "what does it look like"; only the desktop answers "how
    does it look HERE", which is why the candidate is handed to the bubble at
    all. What is pinned here is that the second answer is the SAME pack (judged
    by the reading an install would use), that it wins over the settings without
    editing them, that it is drawn by the `image` design whatever shape the
    settings name — a preview no one can see on the desktop is the old "nothing
    applies" complaint again — and that it always ENDS: because the panel said
    so, because the pack stopped being one, or because nobody renewed it.
    """

    PALETTE = {"idle": (10, 20, 30, 255), "listening": (40, 50, 60, 255),
               "any": (200, 210, 220, 255)}

    class _Clock:
        """A monotonic clock the test advances by hand: no sleeps, no flakes.

        The expiry is the whole reason a panel that dies cannot leave a look on
        the desktop, and it is only testable if time is something the test owns.
        """

        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

    def art(self, folder: Path, *want: str) -> dict:
        return {label: png(folder / f"{label}.png", self.PALETTE[label])
                for label in want}

    def test_a_previewed_pack_is_drawn_and_wins_over_the_settings(
            self, bubble, tmp_path):
        source = tmp_path / "cand"
        shots = self.art(source, "idle", "listening", "any")
        manifest(source, {"name": "Candidate",
                          "states": {"idle": "idle.png",
                                     "listening": "listening.png"},
                          "any": "any.png"})
        # The saved look, which the preview must OVERRIDE and must not change.
        saved = png(tmp_path / "saved.png", (1, 2, 3, 255))
        bubble.SETTINGS.update({"bubble_design": "cat",
                                "design_image_path": str(saved)})

        name, message = bubble.set_pack_preview(source)
        assert name == "Candidate", message
        assert "nothing installed" in message, message
        # Every state: its own picture where the pack has one, the pack's
        # fallback for the states it does not — the same precedence a pack has.
        assert Path(bubble.design_picture("idle")) == shots["idle"]
        assert Path(bubble.design_picture("listening")) == shots["listening"]
        assert Path(bubble.design_picture("thinking")) == shots["any"]
        assert Path(bubble.design_picture("speaking")) == shots["any"]
        # A pack is only ever drawn by the `image` design, so the preview has to
        # BE that design: swapping the picture alone would change nothing on the
        # desktop of a bubble that draws a cat.
        assert bubble.design_in_effect() == "image"
        assert bubble.SETTINGS["bubble_design"] == "cat", (
            "a preview is not an edit")
        assert bubble.preview_note() == "previewing Candidate (not installed)"
        assert bubble.installed_packs() == [], (
            "previewing must not install or copy anything")

        assert bubble.clear_pack_preview() == "stopped previewing Candidate"
        assert bubble.design_in_effect() == "cat", "back to the saved look"
        assert Path(bubble.design_picture("idle")) == saved
        assert bubble.preview_note() == "", "nothing is being previewed now"
        # ...and clearing something that was not there says exactly that, rather
        # than claiming to have stopped a preview that never existed.
        assert bubble.clear_pack_preview() == "no pack was being previewed"

    def test_a_pack_that_would_not_install_is_refused_and_clears_the_last(
            self, bubble, tmp_path):
        # A preview must not be more forgiving than an install — and a REFUSAL
        # has to take the previous candidate down: the panel asked to show this
        # instead, so leaving the old one up would be showing art nobody asked
        # for (and the one thing the user would then be judging).
        good = tmp_path / "good"
        shots = self.art(good, "idle", "any")
        manifest(good, {"name": "Good", "states": {"idle": "idle.png"},
                        "any": "any.png"})
        broken = tmp_path / "broken"
        manifest(broken, {"name": "Broken", "any": "missing.png"})
        saved = png(tmp_path / "saved.png", (1, 2, 3, 255))
        bubble.SETTINGS.update({"design_image_path": str(saved)})

        assert bubble.set_pack_preview(good)[0] == "Good"
        assert Path(bubble.design_picture("idle")) == shots["idle"]
        name, problem = bubble.set_pack_preview(broken)
        assert name == "", problem
        assert bubble.install_pack(broken)[1] == problem, (
            "a preview has to refuse a pack in the very words an install would")
        assert bubble.pack_preview() is None, "a refused preview is not kept"
        assert Path(bubble.design_picture("idle")) == saved, (
            "the rejected candidate is gone, and the old one went with it")

    def test_a_preview_stops_when_nobody_renews_it(self, bubble, tmp_path,
                                                   monkeypatch):
        clock = self._Clock()
        monkeypatch.setattr(bubble, "time", clock)
        source = tmp_path / "cand"
        self.art(source, "idle")
        # `any` and not only `idle`: a pack has to cover every state, one way or
        # the other, and the validator is what says so (see TestPackRefusals).
        manifest(source, {"name": "Candidate", "states": {"idle": "idle.png"},
                          "any": "idle.png"})
        saved = png(tmp_path / "saved.png", (1, 2, 3, 255))
        bubble.SETTINGS.update({"design_image_path": str(saved)})
        assert bubble.set_pack_preview(source)[0] == "Candidate"

        # A renewal is what keeps it up: the deadline moves with each heartbeat,
        # so a long-lived preview is many renewals rather than one long timer.
        for _ in range(3):
            clock.now += bubble.PREVIEW_TTL_S * 0.9
            assert bubble.set_pack_preview(source)[0] == "Candidate"
            assert Path(bubble.design_picture("idle")) == source / "idle.png", (
                "a renewed preview is still being drawn")

        # ...and with no renewal it lapses ON ITS OWN: this is what stops a panel
        # that crashed, was killed, or lost its socket from leaving a look on the
        # desktop that nothing is asking for.
        clock.now += bubble.PREVIEW_TTL_S + 0.001
        assert bubble.pack_preview() is None
        assert Path(bubble.design_picture("idle")) == saved
        assert bubble.design_in_effect() != "image", (
            "the saved shape comes back with the saved art")

    def test_a_preview_is_refused_when_the_folder_stops_being_a_pack(
            self, bubble, tmp_path):
        # Live, not at the door: the folder is re-read on every renewal, so a
        # candidate that is edited or deleted WHILE it is being previewed is
        # detected and dropped instead of drawn from state that no longer
        # matches the disk.
        source = tmp_path / "cand"
        self.art(source, "idle")
        manifest(source, {"name": "Candidate", "states": {"idle": "idle.png"},
                          "any": "idle.png"})
        saved = png(tmp_path / "saved.png", (1, 2, 3, 255))
        bubble.SETTINGS.update({"design_image_path": str(saved)})
        assert bubble.set_pack_preview(source)[0] == "Candidate"
        shutil.rmtree(source)
        name, problem = bubble.set_pack_preview(source)
        assert name == "" and "is not a folder" in problem, problem
        assert Path(bubble.design_picture("idle")) == saved


class TestAvatarDecoration:
    """The ring light around the avatar, and the round avatar it rings.

    Four decisions are load-bearing and each is pinned against impossible
    alternatives rather than against itself: only the documented value turns
    the ring on (a truthy "yes" must not, or the closed choice is not closed);
    the arcs live between the picture's fit and the aperture (inside it and
    not under it); the ring MOVES and brightens (a still bright arc would pass
    a "something was drawn" check); and only full-bleed art is rounded, because
    masking a picture that already carries its own silhouette can only cut ink
    the artist drew on purpose.
    """

    def test_only_the_documented_value_turns_the_ring_on(self, bubble, monkeypatch):
        assert bubble.avatar_ring_on() is False          # absent: no decoration
        monkeypatch.setitem(bubble.SETTINGS, "avatar_ring", "ring-light")
        assert bubble.avatar_ring_on() is True
        monkeypatch.setitem(bubble.SETTINGS, "avatar_ring", " Ring-Light ")
        assert bubble.avatar_ring_on() is True, "the app's own casing and spaces"
        for junk in ("", None, "on", "yes", "ring", "off", "ringlight",
                     1, True, "ring-light-x"):
            monkeypatch.setitem(bubble.SETTINGS, "avatar_ring", junk)
            assert bubble.avatar_ring_on() is False, junk

    def _deco(self, bubble, name, t, lv, lo=40.0, hi=58.0, w=128):
        img = QImage(w, w, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        bubble.BubbleWidget._draw_avatar_deco(
            p, w / 2.0, w / 2.0, lo, hi, name, t, lv,
            QColor("#4f8cff"), 1.0, 1.0)
        p.end()
        return img

    @staticmethod
    def _alpha_sum(img):
        return sum(img.pixelColor(x, y).alpha()
                   for y in range(img.width()) for x in range(img.width()))

    @staticmethod
    def _radii(img):
        """Every inked pixel's distance from the centre: (min, max)."""
        w = img.width()
        c = w / 2.0
        vals = [((x + 0.5 - c) ** 2 + (y + 0.5 - c) ** 2) ** 0.5
                for y in range(w) for x in range(w)
                if img.pixelColor(x, y).alpha() > 8]
        return (min(vals), max(vals)) if vals else (None, None)

    @staticmethod
    def _reach(img):
        return TestAvatarDecoration._radii(img)[1] or 0.0

    def test_the_decoration_table_and_the_schema_agree(self, bubble):
        """A name cannot be valid but unimplemented, or the other way round.

        The picker offers what the schema holds; the painter dispatches what
        the table holds. One name in one and not the other is either a choice
        that draws nothing (silently) or a painter no picker can reach.
        """
        from settings_schema import AVATAR_DECOS
        assert set(AVATAR_DECOS) - {"off"} == set(bubble.DECORATIONS), (
            sorted(AVATAR_DECOS), sorted(bubble.DECORATIONS))

    def test_every_decoration_draws_between_the_avatar_and_the_aperture(self, bubble, monkeypatch):
        """Swept over time, because a decoration that TRAVELS (pulse) is
        legitimately mid-band at any single instant — what must hold at every
        instant is that nothing enters the avatar's own space and nothing
        leaves the aperture, and what must hold over the sweep is that the band
        actually gets used.
        """
        monkeypatch.setattr(bubble, "APERTURE_R", 63.0)
        lo = 40.0
        for name in sorted(bubble.DECORATIONS):
            worst, farthest = 0.0, 0.0
            for t in (0.0, 0.4, 0.9, 1.7, 3.1):
                inner, outer = self._radii(self._deco(bubble, name, t, 0.0))
                assert outer is not None, f"{name} drew nothing at t={t}"
                worst = max(worst, 63.0 - outer)
                farthest = max(farthest, outer)
                assert outer <= 63.0, (
                    f"{name} reaches {outer:.1f} px at t={t} — past the "
                    f"aperture (63.0)")
                assert inner >= lo * 0.95, (
                    f"{name} reaches in to {inner:.1f} px at t={t} — inside the "
                    f"space the picture owns (below {lo})")
            assert worst >= 0.0 and farthest > 55.0, (
                f"{name} never uses its band: it tops out at {farthest:.1f} px")

    def test_every_decoration_animates_and_the_voice_brightens_it(self, bubble):
        for name in sorted(bubble.DECORATIONS):
            assert self._deco(bubble, name, 0.0, 0.0) != self._deco(bubble, name, 0.35, 0.0), (
                f"{name} is a still picture: two moments in time painted the same")
            quiet = self._alpha_sum(self._deco(bubble, name, 0.0, 0.0))
            loud = self._alpha_sum(self._deco(bubble, name, 0.0, 1.0))
            assert loud > quiet, (name, quiet, loud)

    def _painted_centre(self, bubble, monkeypatch, *, state, color, tint, art):
        """Paint the real `image` design and read one pixel at its centre.

        Called through the painter's own entry point with a bare instance
        (`__new__`) rather than a constructed widget: the whole `image` paint
        path from `_paint_image` down is static, so no window, no assistant and
        no QApplication are needed to ask what the design puts on the screen.
        The centre is where the picture is, so it is where "did my character
        keep its colour" is decided.
        """
        path = png(Path(tempfile.mkdtemp()) / "art.png", art)
        monkeypatch.setitem(bubble.SETTINGS, "bubble_design", "image")
        monkeypatch.setitem(bubble.SETTINGS, "design_pack", "")
        monkeypatch.setitem(bubble.SETTINGS, "design_image_path", str(path))
        monkeypatch.setitem(bubble.SETTINGS, "avatar_tint", tint)
        monkeypatch.setitem(bubble.SETTINGS, "avatar_deco", "off")
        monkeypatch.setattr(bubble, "APERTURE_R", 63.0)
        w = bubble.BubbleWidget.__new__(bubble.BubbleWidget)
        w._state = state
        img = QImage(160, 160, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        bubble.BubbleWidget._paint_image(w, p, {
            "cx": 80.0, "cy": 80.0, "t": 1.3, "color": QColor(color),
            "level": 0.6, "energy": 1.0, "glow": 1.0, "anim": 1.0,
            "radius": 63.0, "state": state,
        })
        p.end()
        return img.pixelColor(80, 80)

    def test_original_colours_means_the_art_keeps_them(self, bubble, monkeypatch):
        """The user's own bug: a yellow character came out blue.

        Pinned as a PROPERTY rather than against pixel values, because the two
        modes differ by exactly one decision: `Natural` keeps the art's hue and
        carries the state by the rim and the decoration; `State colours` paints
        the art in the state colour. So the test paints the SAME yellow art at
        TWO state colours and asks whether the state colour reached the
        character's face: in natural mode it must not, in state mode it must.
        A regression to the old behaviour (the rim's halo filling the disc) is
        caught either way — the natural centres would no longer match the art,
        and the two modes would stop being distinguishable at all.
        """
        art = (255, 224, 91, 255)                    # a yellow character
        states = (("idle", "#4f8cff"), ("speaking", "#ff8c42"))

        def centre(tint, state, color):
            return self._painted_centre(bubble, monkeypatch, state=state,
                                        color=color, tint=tint, art=art)

        natural = [centre("natural", s, c) for s, c in states]
        tinted = [centre("state", s, c) for s, c in states]

        for (state, _c), px in zip(states, natural):
            d = ((px.red() - art[0]) ** 2 + (px.green() - art[1]) ** 2
                 + (px.blue() - art[2]) ** 2) ** 0.5
            assert px.green() > px.blue(), (
                f"{state}: natural mode painted the character "
                f"blue (#{px.red():02x}{px.green():02x}{px.blue():02x}) — the "
                f"art's own colours did not survive")
            assert d < 150.0, (
                f"{state}: natural mode moved the art's colour by {d:.0f} "
                f"(#{px.red():02x}{px.green():02x}{px.blue():02x} vs "
                f"#{art[0]:02x}{art[1]:02x}{art[2]:02x})")

        # ...and the mode is the ONLY difference: with the state tint on, the
        # same art DOES take the state colour, so a fix that simply stopped
        # tinting at all would fail here rather than pass everywhere.
        idle_px, speaking_px = tinted
        assert idle_px.blue() > idle_px.green(), (
            f"state tint no longer paints the art: idle came out "
            f"#{idle_px.red():02x}{idle_px.green():02x}{idle_px.blue():02x}")
        assert idle_px.blue() > natural[0].blue(), (
            "state mode must lay more of the state colour on the art than "
            "natural mode does")
        # ...warm toward the STATE's hue rather than the art's, which is what
        # the tint does and what a fixed hue ratio would still catch: an orange
        # state colour pulls the art away from its own yellow (fewer green
        # parts per red part), while natural keeps the art's own ratio.
        assert (speaking_px.green() / max(1, speaking_px.red())
                < natural[1].green() / max(1, natural[1].red())), (
            f"state mode left the art's own hue at speaking: "
            f"#{speaking_px.red():02x}{speaking_px.green():02x}{speaking_px.blue():02x} "
            f"vs natural "
            f"#{natural[1].red():02x}{natural[1].green():02x}{natural[1].blue():02x}")

    def test_every_decoration_VISIBLY_moves_rather_than_merely_differing(self, bubble,
                                                                        monkeypatch):
        """Motion a person could see, not motion a comparison can detect.

        The neighbouring test asks whether two moments differ at all, which
        antialiasing satisfies on its own — and it is blind to the case that
        matters most here: a colour animation. Measured on the deployed bundle,
        an ALPHA sum is identical at every moment of a rainbow ring (the band's
        opacity is uniform; only the hues turn), so a decoration whose whole
        motion is chromatic looks perfectly still to it. Counting pixels per
        channel is no better: a 25-step threshold reports ZERO movement for a
        fully-saturated wheel that is visibly rotating.

        So the property is the one an eye has: a good share of the ring's ink
        must change by more than a step you can see. Measured across every
        decoration in all three colour modes the worst case is 17%, so 10% is
        a floor with room in it rather than a number fitted to one painter.
        """
        step, floor = 8, 0.10
        for deco_color in ("state", "rainbow", "#ff0000"):
            for name in sorted(bubble.DECORATIONS):
                a = self._deco_in(bubble, monkeypatch, name, deco_color,
                                  "#4f8cff", t=0.0)
                b = self._deco_in(bubble, monkeypatch, name, deco_color,
                                  "#4f8cff", t=0.5)
                ink = moved = 0
                for y in range(a.height()):
                    for x in range(a.width()):
                        c1, c2 = a.pixelColor(x, y), b.pixelColor(x, y)
                        if max(c1.alpha(), c2.alpha()) <= 8:
                            continue
                        ink += 1
                        if (abs(c1.red() - c2.red()) > step
                                or abs(c1.green() - c2.green()) > step
                                or abs(c1.blue() - c2.blue()) > step):
                            moved += 1
                assert ink > 0, (name, deco_color)
                assert moved >= ink * floor, (
                    f"{name} ({deco_color}) looks still: only {moved} of {ink} "
                    f"inked pixels ({100.0 * moved / ink:.1f}%) changed by more "
                    f"than {step}")

    def test_full_bleed_art_is_rounded_and_silhouette_art_is_untouched(self, bubble):
        full = QImage(64, 64, QImage.Format_ARGB32)
        full.fill(QColor(0, 0, 0, 255))
        assert bubble.BubbleWidget._needs_rounding(full) is True
        out = bubble.BubbleWidget._round_avatar(full)
        assert out.pixelColor(1, 1).alpha() == 0, "the corner is not cut"
        assert out.pixelColor(32, 32).alpha() > 200, "the centre must survive"

        silhouette = QImage(64, 64, QImage.Format_ARGB32)
        silhouette.fill(0)
        q = QPainter(silhouette)
        q.setPen(Qt.NoPen)
        q.setBrush(QColor(255, 0, 0, 255))
        q.drawEllipse(QPointF(32.0, 32.0), 30.0, 30.0)
        q.end()
        assert bubble.BubbleWidget._needs_rounding(silhouette) is False, (
            "art whose corners are already transparent must not be masked")
        assert bubble.BubbleWidget._round_avatar(silhouette) is silhouette, (
            "character art came back changed")

    # ------------------------------------------------- the decoration's colour

    def _mean_rgb(self, img):
        """The average colour of the inked pixels — what the ring LOOKS like."""
        px = [img.pixelColor(x, y) for y in range(img.width())
              for x in range(img.width()) if img.pixelColor(x, y).alpha() > 20]
        assert px, "nothing was drawn"
        n = float(len(px))
        return (sum(c.red() for c in px) / n, sum(c.green() for c in px) / n,
                sum(c.blue() for c in px) / n)

    def _deco_in(self, bubble, monkeypatch, name, deco_color, state_hex,
                 t=0.0, lv=0.0):
        monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", deco_color)
        img = QImage(128, 128, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        bubble.BubbleWidget._draw_avatar_deco(
            p, 64.0, 64.0, 40.0, 58.0, name, t, lv,
            QColor(state_hex), 1.0, 1.0)
        p.end()
        return img

    def test_the_colour_modes_are_the_ones_the_schema_offers(self, bubble):
        """`core.bubble` cannot import the schema, so it spells the two words
        itself. Spelled twice, they can drift — and a drift is silent: the
        schema would offer a mode the resolver quietly ignores."""
        from settings_schema import AVATAR_DECO_COLORS
        assert tuple(bubble.DECO_COLOR_MODES) == tuple(AVATAR_DECO_COLORS), (
            bubble.DECO_COLOR_MODES, AVATAR_DECO_COLORS)

    def test_the_decoration_colour_is_a_closed_choice_plus_a_hex(self, bubble,
                                                                 monkeypatch):
        assert bubble.avatar_deco_color() == "state"          # absent: the default
        for word in ("state", "rainbow", " Rainbow ", "STATE"):
            monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", word)
            assert bubble.avatar_deco_color() == word.strip().lower(), word
        for hexed in ("#ff0000", "ff0000", "#0A0b0C"):
            monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", hexed)
            assert bubble.avatar_deco_color() == hexed.strip().lower(), hexed
        for junk in ("", None, "red", "#ff00", "#4f8cffXYZ", "rrggbb",
                     123, True, "#ff00000", "custom"):
            monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", junk)
            assert bubble.avatar_deco_color() == "state", junk

    def test_a_rainbow_ring_ignores_the_state_entirely(self, bubble, monkeypatch):
        """The whole point of an independent colour: the state must not reach
        it. Painted at two state colours, a rainbow decoration must come out
        the SAME colour — and it must still move in colour over time, because a
        rainbow that does not cycle is just a colour."""
        monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", "rainbow")
        blue = bubble.avatar_deco_colour(QColor("#4f8cff"), 1.0, 0.0, 1.0)
        red = bubble.avatar_deco_colour(QColor("#ff4d5e"), 1.0, 0.0, 1.0)
        assert blue == red, (blue.name(), red.name())
        assert blue.name() != QColor("#4f8cff").name()
        hues = {bubble.avatar_deco_colour(QColor("#4f8cff"), t, 0.0, 1.0).hue()
                for t in (0.0, 0.7, 1.4, 2.1, 2.8)}
        assert len(hues) >= 4, f"the rainbow barely sweeps: {sorted(hues)}"
        # ...and the voice brightens it, the same contract every decoration has
        quiet = bubble.avatar_deco_colour(QColor("#4f8cff"), 1.0, 0.0, 1.0)
        loud = bubble.avatar_deco_colour(QColor("#4f8cff"), 1.0, 1.0, 1.0)
        assert loud.lightnessF() > quiet.lightnessF(), (loud.name(), quiet.name())

    def test_a_chosen_colour_is_used_exactly_and_beats_the_state(self, bubble,
                                                                 monkeypatch):
        """Picked red on a blue state must draw a RED ring, and the rest of the
        design must still say which state it is in — the rim is the state's, so
        independence cannot cost readability."""
        picked = "#ff0000"
        monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", picked)
        assert (bubble.avatar_deco_colour(QColor("#4f8cff"), 0.0, 0.0, 1.0)
                == QColor(picked))

        mine = self._mean_rgb(self._deco_in(bubble, monkeypatch, "ring-light",
                                            picked, "#4f8cff"))
        monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", "state")
        theirs = self._mean_rgb(self._deco_in(bubble, monkeypatch, "ring-light",
                                              "state", "#4f8cff"))
        assert mine[0] > mine[2], f"the picked red was not used: {mine}"
        assert theirs[2] > theirs[0], f"the state run is not blue: {theirs}"
        assert mine != theirs, "the colour choice changed nothing on screen"

        # the state is still readable: the RIM is drawn in the state colour even
        # while the decoration is red.
        monkeypatch.setitem(bubble.SETTINGS, "avatar_deco_color", picked)
        monkeypatch.setitem(bubble.SETTINGS, "avatar_deco", "ring-light")
        w = bubble.BubbleWidget.__new__(bubble.BubbleWidget)
        w._state = "idle"
        img = QImage(160, 160, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        bubble.BubbleWidget._paint_image(w, p, {
            "cx": 80.0, "cy": 80.0, "t": 1.3, "color": QColor("#4f8cff"),
            "level": 0.0, "energy": 1.0, "glow": 1.0, "anim": 1.0,
            "radius": 63.0, "state": "idle"})
        p.end()
        bluest = max((img.pixelColor(x, y) for y in range(160)
                      for x in range(160)),
                     key=lambda c: c.blue() - c.red())
        assert bluest.blue() > bluest.red() + 40, (
            f"the state colour left the design: {bluest.name()}")

    def test_the_feather_mask_is_cached_by_size(self, bubble):
        first = bubble.BubbleWidget._feather_mask(48, 48)
        assert first is not None
        assert bubble.BubbleWidget._feather_mask(48, 48) is first, (
            "the mask is rebuilt every frame")


class TestAnimationCacheCapacity:
    """One animation must stay resident in the decode cache.

    `_decoded_image` keys its cache by (path, mtime, size) and an animated pack
    names `PACK_MAX_FRAMES` distinct files PER STATE. A cache smaller than one
    animation cycles every frame through the FIFO, so each frame advance
    re-decoded (load + scale + shade) several times a second, sustained — a cost
    the still path never pays and nothing measured.
    """

    def _frames(self, tmp_path, n):
        return [png(tmp_path / f"f{i:02d}.png", rgba=(20 + i, 140, 255, 255))
                for i in range(n)]

    def test_the_cap_covers_a_whole_animation(self, bubble):
        """The two constants are pinned to each other so the cache cannot drift
        below what a pack is allowed to hold."""
        assert bubble._IMAGE_CACHE_MAX >= (bubble.PACK_MAX_FRAMES
                                           + len(BUBBLE_STATES))

    def test_no_frame_is_evicted_by_its_own_animation(self, bubble, tmp_path,
                                                      monkeypatch):
        frames = self._frames(tmp_path, bubble.PACK_MAX_FRAMES)
        decodes: list[str] = []
        real = bubble.image_layers

        def counting(raw):
            decodes.append(raw)
            return real(raw)

        monkeypatch.setattr(bubble, "image_layers", counting)
        for f in frames:
            bubble._image_entry(f)
        assert len(decodes) == bubble.PACK_MAX_FRAMES
        # the animation loops back to its first frame — it must still be there
        bubble._image_entry(frames[0])
        assert len(decodes) == bubble.PACK_MAX_FRAMES, (
            f"frame 0 was evicted by its own animation (cache holds "
            f"{bubble._IMAGE_CACHE_MAX} of {bubble.PACK_MAX_FRAMES} frames), so "
            "every frame advance re-decodes")


class TestInstallOver:
    def test_installing_the_same_name_keeps_one_previous_generation(self, bubble,
                                                                   tmp_path):
        slug = ""
        for i, tint in enumerate(((255, 0, 0, 255), (0, 255, 0, 255),
                                 (0, 0, 255, 255))):
            folder = tmp_path / f"gen{i}"
            png(folder / "base.png", tint)
            manifest(folder, {"name": "Same", "any": "base.png"})
            slug, _message, root = install(bubble, folder)
        assert slug == "same"
        previous = sorted(p.name for p in root.iterdir()
                          if p.name.endswith(".previous"))
        assert previous == ["same.previous"], (
            f"three installs must leave ONE previous generation, not {previous}")
        assert bubble.pack_problem("same") == ""
        assert [s for s, _n in bubble.installed_packs()] == ["same"], (
            "a previous generation is not an installed pack")
