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
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

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
