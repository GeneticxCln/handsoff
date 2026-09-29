"""Audit fixes for the settings layer (settings.json shared by two writers).

Each test pins one finding:

- a GUI full save used to erase keys it did not know and re-stamp a newer
  file's version DOWN (the loader dropped unknown keys before the merge could
  keep them, and every writer stamped unconditionally);
- the env fallbacks were resolved by the bubble's loader only, so the settings
  app saved the schema default over an env-configured value and declared bogus
  conflicts on keys nobody else touched;
- "atomic" writes never fsynced, so power loss could leave a torn store whose
  recovery resets the configuration;
- the GUI's batched live-apply write (persist_settings) must preserve what it
  does not touch, exactly like the single-key writer;
- the GUI's backup helper created its copy at the source's mode and tightened
  it afterwards (a create-then-chmod window on private data);
- dead code: the unused _page_fields helper and an unreachable trailing
  return in _read_settings_for_write.

The Qt-side halves of these findings (live apply, model-switch ordering, the
memory-store lock, the spin widgets, the corrupt-history wording) are pinned
by scenarios in tests/test_settings_gui.py.
"""
from __future__ import annotations

import ast
import copy
import inspect
import json
import os
import stat
import textwrap

from conftest import HERE as ROOT, _load

from core import settings as _core_settings

HERE = ROOT

FUTURE_VERSION = _core_settings.SETTINGS_VERSION + 1


# --------------------------------------------------------------- unknown keys


class TestAFullSaveKeepsWhatItDoesNotUnderstand:
    def test_load_settings_keeps_unknown_keys(self, tmp_path):
        """Unknown keys pass through the loader so writers can re-emit them —
        a retired key is still dropped (that list is the one deletion rule)."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"future_key": {"a": 1},
                                 "piper_voice": "old.onnx",
                                 "mic_threshold": 700}))
        s = _core_settings.load_settings(f)
        assert s["future_key"] == {"a": 1}
        assert "piper_voice" not in s, "a retired key is not an unknown one"
        assert s["mic_threshold"] == 700

    def test_load_write_roundtrip_keeps_future_keys_and_version(
            self, tmp_path):
        """A load followed by a write leaves a newer build's file unchanged,
        apart from the keys this build deliberately touches."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": FUTURE_VERSION,
                                 "future_key": {"a": 1},
                                 "model": "m"}))
        loaded = _core_settings.load_settings(f)
        _core_settings.write_settings(loaded, f, tmp_path)
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["future_key"] == {"a": 1}
        assert on_disk["version"] == FUTURE_VERSION, (
            "a write from this older build re-labelled the newer file")
        assert on_disk["model"] == "m"

    def test_full_gui_save_keeps_future_keys_and_version(self, tmp_path):
        """The settings app's own load (merge_settings) and its full save
        (write_all against that snapshot) must preserve both, too: this is
        the exact pair that erased a newer build's configuration."""
        mod = _load("handsoff_settings_audit_future", HERE /
                    "handsoff-settings.py")
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": FUTURE_VERSION,
                                 "future_key": {"a": 1},
                                 "model": "m"}))
        cfg = mod.merge_settings(json.loads(f.read_text(encoding="utf-8")))
        assert cfg["future_key"] == {"a": 1}, (
            "the GUI's load dropped a key it does not know")
        snapshot = copy.deepcopy(cfg)
        cfg["mic_threshold"] = 640
        written = _core_settings.settings_object(f, tmp_path).write_all(
            cfg, expected_data=snapshot)
        assert written["future_key"] == {"a": 1}
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["future_key"] == {"a": 1}
        assert on_disk["version"] == FUTURE_VERSION
        assert on_disk["mic_threshold"] == 640

    def test_persist_keeps_a_future_version_stamp(self, tmp_path):
        """The single-key writer must not re-stamp a newer file's version
        either — its stamp was unconditional like the full writer's."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"version": FUTURE_VERSION,
                                 "future_key": 1}))
        assert _core_settings.persist_setting(
            "tts_rate", 1.5, f, tmp_path) is True
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["version"] == FUTURE_VERSION
        assert on_disk["future_key"] == 1
        assert on_disk["tts_rate"] == 1.5

    def test_an_older_file_is_still_stamped_up(self, tmp_path):
        """Non-downgrading means UP is still applied: a pre-versioning file
        gets the current stamp, exactly as before."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"mic_threshold": 700}))
        assert _core_settings.persist_setting(
            "tts_rate", 1.5, f, tmp_path) is True
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["version"] == _core_settings.SETTINGS_VERSION


# ---------------------------------------------------------------- env matches


class TestTheGuiSeesTheEnvValuesTheBubbleDoes:
    def test_gui_merge_applies_the_env_fallbacks_like_the_loader(
            self, tmp_path, monkeypatch):
        """One env application for both readers: the GUI's merge used to skip
        it, so its "expected" disagreed with the bubble's env-resolved
        "current"."""
        mod = _load("handsoff_settings_audit_env", HERE / "handsoff-settings.py")
        monkeypatch.setenv("OLLAMA_HOST", "http://envhost:11434")
        assert mod.merge_settings({})["ollama_host"] == "http://envhost:11434"
        # the file still wins over the env, exactly like the loader
        assert mod.merge_settings(
            {"ollama_host": "http://file:1"})["ollama_host"] == "http://file:1"
        # ...and the two readers agree on a sparse file
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"model": "m"}))
        merged = mod.merge_settings(json.loads(f.read_text(encoding="utf-8")))
        assert (merged["ollama_host"]
                == _core_settings.load_settings(f)["ollama_host"]
                == "http://envhost:11434")

    def test_gui_save_with_env_configured_host_raises_no_bogus_conflict(
            self, tmp_path, monkeypatch):
        """Editing a field next to an env-configured one used to trip
        SettingsConflictError: the GUI "expected" the schema default while the
        bubble's current was the env value — a conflict nobody caused."""
        mod = _load("handsoff_settings_audit_env2", HERE /
                    "handsoff-settings.py")
        monkeypatch.setenv("OLLAMA_HOST", "http://envhost:11434")
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"model": "m"}))
        cfg = mod.merge_settings(json.loads(f.read_text(encoding="utf-8")))
        snapshot = copy.deepcopy(cfg)
        cfg["mic_threshold"] = 500
        written = _core_settings.settings_object(f, tmp_path).write_all(
            cfg, expected_data=snapshot)
        assert written["ollama_host"] == "http://envhost:11434", (
            "a save must keep (materialize) the env value, not the default")
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["ollama_host"] == "http://envhost:11434"
        assert on_disk["mic_threshold"] == 500


# ---------------------------------------------------------------------- fsync


class _FsyncRecorder:
    """Records (is_directory, event) in call order around os.replace."""

    def __init__(self, monkeypatch):
        self.events: list = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            self.events.append(
                ("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
            return real_fsync(fd)

        def replace(src, dst):
            self.events.append(("replace", os.fspath(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)

    def sequence(self, dst):
        """The events that belong to one replace of `dst`."""
        want = os.fspath(dst)
        names = [e for e in self.events
                 if e[0] == "fsync" or e[1] == want]
        return [e[0] if e[0] == "fsync" else "replace" for e in names]

    def file_then_directory(self, dst):
        seq = self.sequence(dst)
        assert seq == ["fsync", "replace", "fsync"], seq
        assert self.events[0][1] is False, "the temp FILE must be fsynced"
        assert self.events[-1][1] is True, "the DIRECTORY must be fsynced"


class TestAtomicWritesFsync:
    def test_atomic_private_write_fsyncs_file_then_directory(
            self, tmp_path, monkeypatch):
        """Power loss between the write and the replace used to leave a torn
        store whose recovery resets the configuration; the fsyncs are what
        makes "atomic" mean atomic."""
        rec = _FsyncRecorder(monkeypatch)
        target = tmp_path / "settings.json"
        _core_settings.atomic_private_write(target, '{"a": 1}')
        rec.file_then_directory(target)
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    def test_backup_runtime_json_fsyncs_file_then_directory(
            self, tmp_path, monkeypatch):
        rec = _FsyncRecorder(monkeypatch)
        src = tmp_path / "history.json"
        src.write_text('[{"old": true}]', encoding="utf-8")
        _core_settings.backup_runtime_json(src)
        rec.file_then_directory(tmp_path / "history.json.bak")

    def test_gui_atomic_text_write_fsyncs_file_then_directory(
            self, tmp_path, monkeypatch):
        mod = _load("handsoff_settings_audit_fsync", HERE /
                    "handsoff-settings.py")
        rec = _FsyncRecorder(monkeypatch)
        target = tmp_path / "config.kdl"
        target.write_text("// niri config\n", encoding="utf-8")
        mod._atomic_text_write(target, "// niri config\n// edited\n")
        rec.file_then_directory(target)
        assert "// edited" in target.read_text(encoding="utf-8")


# ------------------------------------------------- the live-apply write batch


class TestPersistSettingsBatch:
    def test_the_batch_writes_its_keys_and_preserves_the_rest(self, tmp_path):
        """persist_settings is the machinery the Appearance live apply uses:
        one locked read-merge-write that must leave every key it was not
        given — including a newer build's — byte-for-byte alone."""
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"mic_threshold": 700, "future_key": 1}))
        assert _core_settings.persist_settings(
            {"bubble_size": 150,
             "colors": {"idle": "#112233"}},
            f, tmp_path) is True
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["bubble_size"] == 150
        assert on_disk["colors"]["idle"] == "#112233"
        assert on_disk["mic_threshold"] == 700
        assert on_disk["future_key"] == 1

    def test_the_batch_reports_failure_and_keeps_the_old_file(
            self, tmp_path, monkeypatch):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"mic_threshold": 700}))
        real = _core_settings.atomic_private_write

        def enospc(path, text):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(_core_settings, "atomic_private_write", enospc)
        assert _core_settings.persist_settings(
            {"bubble_size": 150}, f, tmp_path) is False
        monkeypatch.setattr(_core_settings, "atomic_private_write", real)
        assert json.loads(f.read_text(encoding="utf-8"))["mic_threshold"] == 700

    def test_persist_setting_is_the_one_key_form(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"mic_threshold": 700}))
        assert _core_settings.persist_setting(
            "bubble_size", 150, f, tmp_path) is True
        on_disk = json.loads(f.read_text(encoding="utf-8"))
        assert on_disk["bubble_size"] == 150
        assert on_disk["mic_threshold"] == 700


# ------------------------------------------------------- create-with-mode (GUI)


class TestTheGuiBackupIsBornOwnerOnly:
    def test_backup_keep_n_needs_no_chmod_after_the_fact(
            self, tmp_path, monkeypatch):
        """The backup used to be created at the SOURCE's mode and tightened
        afterwards — a window in which a permissive file's private data sat
        readable under the backup's name. It is written to a 0600 temp and
        renamed now, so no chmod-after-create can come back."""
        mod = _load("handsoff_settings_audit_backup", HERE /
                    "handsoff-settings.py")
        src = tmp_path / "memory.json"
        src.write_text('[{"k": "name", "v": "private"}]', encoding="utf-8")
        src.chmod(0o644)               # the permissive source of the story
        # the spy goes in AFTER the source's own chmod, so only the helper's
        # calls are recorded
        chmods = []
        real_chmod = os.chmod

        def spy_chmod(path, mode, **kw):
            chmods.append((os.fspath(path), mode))
            return real_chmod(path, mode, **kw)

        monkeypatch.setattr(os, "chmod", spy_chmod)
        mod._backup_keep_n(src, "bak-facts")
        baks = list(tmp_path.glob("memory.json.bak-facts.*"))
        assert len(baks) == 1
        assert baks[0].stat().st_mode & 0o777 == 0o600
        assert baks[0].read_text(encoding="utf-8") == \
            '[{"k": "name", "v": "private"}]'
        assert chmods == [], (
            "the backup must be created owner-only, not chmod'ed after")


# ------------------------------------------------------------------ dead code


class TestDeadCodeRemoved:
    def test_the_unused_page_fields_helper_is_gone(self):
        """_page_fields was referenced nowhere (grep before removal); its
        reappearance would be a second definition of "a page's rows" beside
        _group_fields, which is exactly the drift the table removes."""
        mod = _load("handsoff_settings_audit_dead", HERE /
                    "handsoff-settings.py")
        assert not hasattr(mod, "_page_fields")

    def test_read_settings_for_write_ends_on_the_wrong_shape_guard(self):
        """The unreachable trailing `return {}` is gone: the wrong shape is
        handled by falling out of the try into ONE quarantine+return, with no
        second guard (and no statement after the final return) at function
        level. The OSError propagation note sits with the read it describes."""
        src = textwrap.dedent(inspect.getsource(
            _core_settings._read_settings_for_write))
        body = ast.parse(src).body[0].body
        tail_expr, tail_return = body[-2], body[-1]
        assert isinstance(tail_return, ast.Return), (
            "the function must end on its fall-through return")
        assert isinstance(tail_expr, ast.Expr) and all(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") ==
            "quarantine_file"
            for n in ast.walk(tail_expr)
            if isinstance(n, ast.Call)), (
            "the fall-through return must be the wrong-shape quarantine")
        # the old dead shape carried a SECOND top-level `if not isinstance`
        # guard followed by the unreachable return; there is exactly one
        # wrong-shape check now, inside the try
        assert not [n for n in body if isinstance(n, ast.If)], (
            "dead code crept back after the wrong-shape quarantine")
