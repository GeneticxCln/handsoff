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
from settings_schema import DEFAULT_SETTINGS


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
