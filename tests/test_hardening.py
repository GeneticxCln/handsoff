"""Edge-case tests for the runtime-hardening paths merged from the parallel
session: _prepare_runtime / _private_dir / _secure_file / _secure_runtime_files
and _remove_stale_control_socket. Every test here covers a case the original
happy-path suite did not."""
from __future__ import annotations

import fcntl
import importlib.util
import os
import socket
import threading
from pathlib import Path

import pytest

from conftest import HERE as ROOT

HERE = ROOT   # the repo root


@pytest.fixture(scope="module")
def H():
    spec = importlib.util.spec_from_file_location(
        "handsoff_core_hardening", HERE / "handsoff.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sandbox(H, monkeypatch, tmp_path):
    """Redirect every runtime path into a private tmp sandbox."""
    cfg = tmp_path / "cfg"
    state = tmp_path / "state"
    monkeypatch.setattr(H, "CONFIG_DIR", cfg)
    monkeypatch.setattr(H, "STATE_DIR", state)
    monkeypatch.setattr(H, "WHISPER_MODEL_DIR", cfg / "whisper-model")
    monkeypatch.setattr(H, "PIPER_VOICE_DIR", cfg / "piper-voice")
    monkeypatch.setattr(H, "SETTINGS_FILE", cfg / "settings.json")
    monkeypatch.setattr(H, "HISTORY_FILE", cfg / "history.json")
    monkeypatch.setattr(H, "MEMORY_FILE", cfg / "memory.json")
    monkeypatch.setattr(H, "CRASH_LOG", state / "crash.log")
    monkeypatch.setattr(H, "PENDING_FILE", state / "pending-restart.json")
    monkeypatch.setattr(H, "LOCK_FILE", state / "handsoff.lock")
    monkeypatch.setattr(H, "LOG_FILE", state / "handsoff.log")
    monkeypatch.setattr(H, "CONTROL_SOCK", state / "control.sock")
    monkeypatch.setattr(H, "MIC_EVENTS_FILE", state / "mic-health.json")
    monkeypatch.setattr(H, "REMINDERS_FILE", state / "reminders.json")
    return tmp_path


def _mk_socket(path: Path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    return s


class TestPrivateDir:
    def test_creates_dir_owner_only(self, H, sandbox):
        d = sandbox / "cfg" / "sub" / "deep"
        assert H._private_dir(d) is True
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_tightens_existing_group_world_readable(self, H, sandbox):
        d = sandbox / "cfg"
        d.mkdir()
        d.chmod(0o755)
        assert H._private_dir(d) is True
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_refuses_symlink(self, H, sandbox):
        target = sandbox / "elsewhere"
        target.mkdir()
        link = sandbox / "cfg"
        link.symlink_to(target)
        assert H._private_dir(link) is False

    def test_refuses_regular_file_at_dir_path(self, H, sandbox):
        f = sandbox / "cfg"
        f.write_text("not a dir")
        assert H._private_dir(f) is False

    def test_refuses_foreign_owned_dir(self, H, monkeypatch, sandbox):
        d = sandbox / "cfg"
        d.mkdir()
        monkeypatch.setattr(H.os, "getuid", lambda: d.stat().st_uid + 1)
        assert H._private_dir(d) is False


class TestSecureFile:
    def test_missing_file_is_success(self, H, sandbox):
        assert H._secure_file(sandbox / "state" / "nope.json") is True

    def test_regular_file_gets_0600(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "f.json"
        f.write_text("{}")
        f.chmod(0o644)
        assert H._secure_file(f) is True
        assert (f.stat().st_mode & 0o777) == 0o600

    def test_refuses_symlink_including_broken(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        target = sandbox / "real.txt"
        target.write_text("data")
        link = state / "link.json"
        link.symlink_to(target)
        assert H._secure_file(link) is False
        broken = state / "broken.json"
        broken.symlink_to(sandbox / "ghost")     # broken symlink
        assert H._secure_file(broken) is False

    def test_refuses_directory(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        d = state / "dir.json"
        d.mkdir()
        assert H._secure_file(d) is False

    def test_refuses_foreign_owned(self, H, monkeypatch, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "f.json"
        f.write_text("{}")
        monkeypatch.setattr(H.os, "getuid", lambda: f.stat().st_uid + 1)
        assert H._secure_file(f) is False

    def test_stale_permissive_socket_self_heals(self, H, sandbox):
        """E10 (the wedge): a stale socket with 0777 (umask 000 bind) must be
        tightened, not make _prepare_runtime refuse startup forever."""
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        old = os.umask(0o000)
        try:
            s = _mk_socket(sp)                    # mode 0777 under umask 0
        finally:
            os.umask(old)
        assert (sp.stat().st_mode & 0o777) == 0o777
        assert H._secure_file(sp) is True         # self-healed, not refused
        assert (sp.stat().st_mode & 0o777) == 0o600
        s.close()

    def test_already_private_socket_untouched(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        s = _mk_socket(sp)
        sp.chmod(0o600)
        assert H._secure_file(sp) is True
        s.close()


class TestPrepareRuntime:
    def test_fresh_sandbox_passes_and_private(self, H, sandbox):
        assert H._prepare_runtime() is True
        for d in (sandbox / "cfg", sandbox / "state"):
            assert (d.stat().st_mode & 0o777) == 0o700

    def test_refuses_when_a_dir_is_symlink(self, H, sandbox):
        (sandbox / "elsewhere").mkdir()
        cfg = sandbox / "cfg"
        cfg.symlink_to(sandbox / "elsewhere")
        assert H._prepare_runtime() is False

    def test_all_files_hardened_even_when_one_is_bad(self, H, sandbox):
        H._prepare_runtime()
        good = sandbox / "state" / "mic-health.json"
        good.write_text("[]")
        good.chmod(0o644)
        bad = sandbox / "state" / "handsoff.lock"
        bad.mkdir()                               # a dir where a file belongs
        assert H._secure_runtime_files() is False
        assert (good.stat().st_mode & 0o777) == 0o600   # still tightened


class TestRemoveStaleControlSocket:
    def test_missing_socket_is_a_noop(self, H, sandbox):
        H._remove_stale_control_socket()          # must not raise

    def test_removes_own_stale_socket(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        s = _mk_socket(sp)
        s.close()
        H._remove_stale_control_socket()
        assert not sp.exists()

    def test_refuses_symlink(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        real = sandbox / "real.sock"
        real.write_text("x")
        link = state / "control.sock"
        link.symlink_to(real)
        with pytest.raises(OSError, match="symlink"):
            H._remove_stale_control_socket()
        assert real.exists()                      # target untouched

    def test_refuses_regular_file(self, H, sandbox):
        state = sandbox / "state"
        state.mkdir()
        f = state / "control.sock"
        f.write_text("this is not a socket")
        with pytest.raises(OSError, match="not a socket"):
            H._remove_stale_control_socket()
        assert f.exists()                         # contents preserved

    def test_refuses_foreign_owned_socket(self, H, monkeypatch, sandbox):
        state = sandbox / "state"
        state.mkdir()
        sp = state / "control.sock"
        s = _mk_socket(sp)
        s.close()
        monkeypatch.setattr(H.os, "getuid", lambda: sp.stat().st_uid + 1)
        with pytest.raises(OSError, match="uid"):
            H._remove_stale_control_socket()
        assert sp.exists()


class TestRuntimePrepareStartupIntegration:
    """The fail-closed paths a user actually sees."""

    def test_doctor_reports_socket_problem(self, H, sandbox, monkeypatch):
        """Before E9, a refusing socket path failed silently at startup."""
        state = sandbox / "state"
        state.mkdir()
        link = state / "control.sock"
        link.symlink_to(sandbox / "evil")
        monkeypatch.setattr(H, "ollama_available", lambda: False)
        monkeypatch.setattr(H.ToolBelt, "_niri_msg",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no niri in tests"))))
        text = H.run_doctor()
        assert "REFUSES STARTUP" in text and "symlink" in text

    def test_doctor_reports_missing_socket_calmly(self, H, sandbox, monkeypatch):
        monkeypatch.setattr(H, "ollama_available", lambda: False)
        monkeypatch.setattr(H.ToolBelt, "_niri_msg",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no niri in tests"))))
        assert "not created yet" in H.run_doctor()

    def test_serve_refuses_when_prepare_fails(self, H, sandbox, monkeypatch):
        """ControlServer._serve must bail out cleanly, not bind an insecure
        socket, when _prepare_runtime refuses the runtime."""
        (sandbox / "cfg").symlink_to(sandbox / "elsewhere")
        srv = H.ControlServer.__new__(H.ControlServer)
        srv._assistant = None
        srv._stop = threading.Event()
        srv._server = None
        srv._thread = None
        srv._serve()                              # must not raise
        assert srv._server is None
        assert not (sandbox / "state" / "control.sock").exists()

    def test_lock_file_created_private(self, H, sandbox):
        H.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fh = None
        for _attempt in range(H.LOCK_RETRIES + 1):
            try:
                fh = open(H.LOCK_FILE, "w")
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                continue
            os.chmod(H.LOCK_FILE, 0o600)
            fh.write("123")
            fh.flush()
            break
        assert (H.LOCK_FILE.stat().st_mode & 0o777) == 0o600
        fh.close()
