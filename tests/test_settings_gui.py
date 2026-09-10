"""Offscreen Qt tests for the Settings GUI (handsoff-settings.py).

The settings GUI is the least-covered shipped module and the reason the
suite-wide coverage floor sits at 60 instead of 70. These tests drive the
REAL SettingsWindow through chunky end-to-end scenarios — save/load,
memory/history rendering, keybind snippets, disk-reload, the health
status line, and the per-tool policy rows.

Why subprocesses: in-process construction of the settings window aborts
the whole suite when any earlier test module left a QApplication or live
bubble threads behind (the exact hazard TestSettingsHistoryTab documents
in test_settings.py). Each scenario here therefore runs in a fresh child
process with QT_QPA_PLATFORM=offscreen and every path redirected to a
tmp HOME — no real config, no real bubble, no network, no systemd.
Coverage measures these lines because the child re-executes the module
under the same interpreter and coverage.py's subprocess patching (via
COVERAGE_PROCESS_START + .pth hook) attributes it to this run.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HERE

GUI_DRIVER = """
import copy
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

# --- offscreen Qt + isolated env: FIRST, before any Qt import
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QPA_PLATFORMTHEME"] = ""
os.environ["NO_AT_BRIDGE"] = "1"
os.environ["QT_ACCESSIBILITY"] = "0"
os.environ["HANDSOFF_PYTHON"] = sys.executable

# Subprocess coverage: pytest-cov 7 dropped the automatic .pth hook, so the
# driver engages measurement itself when the parent run exported
# COVERAGE_PROCESS_START (no-op otherwise; idempotent if a .pth hook already
# started coverage). Data save happens via atexit on the sys.exit below.
try:
    import coverage
    coverage.process_startup()
except Exception:
    pass

home = Path(os.environ["SGUI_HOME"])
config_dir = home / ".config" / "handsoff"
state_dir = home / ".local" / "state" / "handsoff"
config_dir.mkdir(parents=True, exist_ok=True)
state_dir.mkdir(parents=True, exist_ok=True)
niri_dir = home / ".config" / "niri"
niri_dir.mkdir(parents=True, exist_ok=True)
piper_dir = config_dir / "piper-voice"
piper_dir.mkdir(parents=True, exist_ok=True)
(NIRI := niri_dir / "config.kdl").write_text("// niri config\\n")

import importlib.util


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HERE = os.environ["SGUI_HERE"]
settings_app = load("handsoff_settings_gui", os.path.join(HERE, "handsoff-settings.py"))
bubble = load("handsoff_core_gui", os.path.join(HERE, "handsoff.py"))

# --- point the settings app's lazy H proxy at THIS bubble exec, then
# --- rebind every path constant to tmp BEFORE any window is built
settings_app.H.__dict__["_bubble"] = bubble
import threading
settings_app.H.__dict__["_lock"] = threading.Lock()

settings_file = config_dir / "settings.json"
history_file = config_dir / "history.json"
memory_file = config_dir / "memory.json"
decisions_file = state_dir / "decisions.jsonl"
bubble.SETTINGS_FILE = settings_file
bubble.CONFIG_DIR = config_dir
bubble.STATE_DIR = state_dir
bubble.HISTORY_FILE = history_file
bubble.MEMORY_FILE = config_dir / "memory.json"
bubble.DECISIONS_FILE = state_dir / "decisions.jsonl"
bubble.LOG_FILE = state_dir / "handsoff.log"
bubble.CONTROL_SOCK = state_dir / "control.sock"
bubble.PIPER_VOICE_DIR = piper_dir
bubble.RESTART_SCRIPT = home / "absent" / "handsoff-restart"
bubble.SETTINGS = copy.deepcopy(bubble.DEFAULT_SETTINGS)

import core.settings
bubble._SETTINGS_OBJ = core.settings.settings_object(settings_file, config_dir)

settings_app.HOME = home
settings_app.NIRI_CONFIG = NIRI

# --- stub every side door: network, systemd, niri reload, xdg-open


def _no_http(url, payload=None, timeout=10):
    raise urllib.error.URLError(f"stubbed in GUI tests ({url})")


class _Result:
    returncode = 1
    stdout = ""
    stderr = ""


settings_app.http_json = _no_http
settings_app.systemd_owns_autostart = lambda: False
settings_app._reload_niri = lambda: None
settings_app.subprocess.run = lambda *a, **k: _Result()
settings_app.subprocess.Popen = lambda *a, **k: None

# --- seed disk state the scenarios read


def seed(settings=None, history=None):
    settings_file.write_text(json.dumps(settings or {}), encoding="utf-8")
    if history is None:
        history_file.unlink(missing_ok=True)
    else:
        history_file.write_text(json.dumps(history), encoding="utf-8")


from PySide6.QtWidgets import QApplication

app = QApplication([])
win = settings_app.SettingsWindow()

SCENARIOS = {}
TESTS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _spin_health(win, want="querying", timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and want in win.health_label.text():
        app.processEvents()
        time.sleep(0.05)


@scenario
def save_roundtrip():
    seed({"model": "testmodel:latest", "mic_threshold": 700,
          "allow_remote_ollama": True})
    win.reload_from_disk()
    win.ctx_spin.setValue(8192)
    win.thresh_spin.setValue(750)
    win.wake_name_edit.setText("cypher")
    assert win.save() is True
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["num_ctx"] == 8192
    assert on_disk["mic_threshold"] == 750
    assert on_disk["assistant_name"] == "cypher"
    assert on_disk["model"] == "testmodel:latest"
    assert on_disk["allow_remote_ollama"] is True
    assert "Saved to" in win.status_label.text()


@scenario
def save_refuses_without_model():
    seed({"model": "testmodel:latest"})
    win.reload_from_disk()
    # coercion fills an empty model from defaults, so the refusal path is
    # reached by blanking the in-memory cfg the way a degenerate H would
    win.cfg["model"] = ""
    assert win.save() is False
    assert "pick a model first" in win.status_label.text()


@scenario
def external_change_reloads_and_reports():
    seed({"model": "third:latest"})
    win._check_disk_changes()
    assert win.cfg["model"] == "third:latest"
    assert "reloaded from disk" in win.status_label.text()
    seed({"model": "testmodel:latest"})
    win._check_disk_changes()
    assert win.cfg["model"] == "testmodel:latest"


@scenario
def missing_settings_file_mtime_is_zero():
    settings_file.unlink(missing_ok=True)
    assert win._settings_mtime() == 0.0


@scenario
def conversation_pane_renders_roles_and_tool_calls():
    seed(history=[
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi",
         "tool_calls": [{"function": {"name": "run_command"}}]},
        {"role": "tool", "content": "ok\\nsecond line"},
    ])
    win._refresh_history()
    text = win.history_view.toPlainText()
    assert "3 messages" in text
    assert "[user] hello there" in text
    assert "calls tool: run_command" in text
    assert "[tool result] ok" in text
    history_file.unlink(missing_ok=True)
    win._refresh_history()
    assert "history is empty" in win.history_view.toPlainText()


@scenario
def clear_history_keeps_backup():
    from PySide6.QtWidgets import QMessageBox

    seed(history=[{"role": "user", "content": "remember this"}])

    class _Yes(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.Yes

    saved = settings_app.QMessageBox
    settings_app.QMessageBox = _Yes
    try:
        win._clear_history()
    finally:
        settings_app.QMessageBox = saved
    assert "history cleared" in win.history_view.toPlainText()
    backups = list(config_dir.glob("history.json.bak-manual.*"))
    assert backups, "clear must keep a backup"


@scenario
def history_tab_renders_and_truncates():
    seed(history=[{"role": "user", "content": "x" * 500}])
    win._refresh_history()
    text = win.history_view.toPlainText()
    assert "1 messages" in text
    assert len(text) < 400          # per-message truncation
    history_file.write_text("not json at all", encoding="utf-8")
    win._refresh_history()
    assert "history is empty" in win.history_view.toPlainText()


@scenario
def keybinds_snippet_written_to_tmp_config():
    win._write_keybinds()
    snippet = config_dir / "niri-keybinds.kdl"
    assert snippet.exists()
    text = snippet.read_text(encoding="utf-8")
    # the snippet KDL-escapes inner quotes (backslash-quote)
    for action in ("toggle", "interrupt", "handsfree", "dictation"):
        assert action + "\\\\" in text or action + "\\\"" in text or action in text
        assert "--ptt" in text
    assert "snippet written" in win.status_label.text()


@scenario
def restart_and_log_guards():
    win._on_restart_bubble()
    assert "restart script missing" in win.status_label.text()
    win._show_log()
    assert "no log file yet" in win.status_label.text()


@scenario
def apply_autostart_disabled_is_noop():
    msg = settings_app.apply_autostart(False)
    assert "nothing to change" in msg or "unchanged" in msg


@scenario
def health_line_reports_not_running():
    bubble.CONTROL_SOCK = state_dir / "absent.sock"
    win._refresh_health()
    _spin_health(win)
    assert "not running" in win.health_label.text()
    assert "settings still work" in win.health_label.text()
    assert win.health_label.styleSheet() == "color: palette(mid);"


@scenario
def fmt_health_ok_and_degraded():
    ok = {"mic": {"state": "listening", "device": "d" * 50, "rate": 16000,
                  "utterances": 3},
          "brain": {"reachable": True, "model": "m:1"},
          "tts": {"ready": True}, "stt": {"ready": True}}
    line = settings_app._fmt_health(ok)
    assert "mic: listening" in line and "brain: ok m:1" in line
    assert "\\u2026" in line or "\\u2026".encode().decode() in line or "…" in line
    degraded = {"mic": {"state": "silent"},
                "brain": {"reachable": False}, "tts": {}, "stt": {}}
    assert "silent" in settings_app._fmt_health(degraded)


@scenario
def health_query_bad_inputs():
    assert settings_app._health_query(state_dir / "no-such.sock") is None
    assert settings_app._health_query(None) is None


@scenario
def policy_rows_roundtrip():
    assert len(win.policy_rows) >= 40          # the real tool census
    rows = win.policy_rows["run_command"]
    rows.setCurrentIndex(1)                    # DENY
    win._collect()
    assert win.cfg["command_policy"]["run_command"] == "DENY"
    rows.setCurrentIndex(0)                    # ALLOW -> not persisted
    win._collect()
    assert "run_command" not in win.cfg["command_policy"]


@scenario
def remote_ollama_checkbox_roundtrip():
    win.remote_ollama_chk.setChecked(True)
    win._collect()
    assert win.cfg["allow_remote_ollama"] is True
    win.remote_ollama_chk.setChecked(False)
    win._collect()
    assert win.cfg["allow_remote_ollama"] is False


@scenario
def model_picker_and_voice_combo():
    from PySide6.QtWidgets import QListWidgetItem
    from PySide6.QtCore import Qt
    win.model_list.clear()
    it = QListWidgetItem("pickme:latest")
    it.setData(Qt.UserRole, "pickme:latest")
    win.model_list.addItem(it)
    win.model_list.setCurrentItem(it)
    assert win._selected_model() == "pickme:latest"
    (piper_dir / "en_US-test-medium.onnx").write_bytes(b"x")
    win.refresh_voices()
    labels = [win.voice_combo.itemText(i)
              for i in range(win.voice_combo.count())]
    assert any("en_US-test-medium" in lab for lab in labels)


@scenario
def tabs_and_colors():
    texts = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    for expected in ("Brain", "Voice", "Permissions",
                     "Appearance", "Startup", "History"):
        assert expected in texts, texts
    assert "Memory" not in texts  # folded into History → Durable facts
    assert set(win._colors) == set(win.cfg["colors"])
    win._colors["idle"] = "#123456"
    win._collect()
    assert win.cfg["colors"]["idle"] == "#123456"


@scenario
def clear_history_via_stubbed_dialog():
    from PySide6.QtWidgets import QMessageBox

    seed(history=[{"role": "user", "content": "bye"}])

    class _Yes(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.Yes

    saved = settings_app.QMessageBox
    settings_app.QMessageBox = _Yes
    try:
        win._clear_history()
    finally:
        settings_app.QMessageBox = saved
    backups = list(config_dir.glob("history.json.bak-manual.*"))
    assert backups and "(history cleared" in win.history_view.toPlainText()

    class _No(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.No

    settings_app.QMessageBox = _No
    try:
        seed(history=[{"role": "user", "content": "keep me"}])
        win._clear_history()
        assert history_file.exists()
        assert history_file.read_text(encoding="utf-8") == json.dumps(
            [{"role": "user", "content": "keep me"}])
    finally:
        settings_app.QMessageBox = saved


@scenario
def autostart_enable_migrates_old_line():
    NIRI.write_text(
        f"// niri config\\n{settings_app.AUTOSTART_LINE_OLD}\\n",
        encoding="utf-8")
    assert "migrated" in settings_app.set_autostart(True)
    text = NIRI.read_text(encoding="utf-8")
    assert text.count(settings_app.AUTOSTART_LINE) == 1
    assert settings_app.AUTOSTART_LINE_OLD not in text
    assert (NIRI.parent / "config.kdl.bak-handsoff").exists()

    # Already-exactly-one new line: idempotent enable, then clean removal.
    NIRI.write_text(
        f"// niri config\\n{settings_app.AUTOSTART_LINE}\\n", encoding="utf-8")
    assert settings_app.set_autostart(True) == "autostart unchanged"
    assert "removed" in settings_app.set_autostart(False)
    assert settings_app.AUTOSTART_LINE not in NIRI.read_text(encoding="utf-8")


@scenario
def history_view_renders_decisions():
    seed(history=[
        {"role": "user", "content": "what time is it?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "get_time"}}]},
        {"role": "tool", "content": "12:34\\nextra"},
        {"role": "assistant", "content": "It is 12:34."},
    ])
    win._refresh_history()
    text = win.history_view.toPlainText()
    assert "4 messages — newest last:" in text
    assert "[user] what time is it?" in text
    assert "calls tool: get_time" in text
    assert "[tool result] 12:34 extra" in text
    assert "[assistant] It is 12:34." in text

    seed(history=[])
    win._refresh_history()
    assert "(history is empty" in win.history_view.toPlainText()


@scenario
def facts_pane_lists_forgets_and_dedups():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    memory_file.write_text(json.dumps([
        {"k": "name", "v": "the user's name is Alice"},
        {"k": "name", "v": "the user's name is Bob"},
        {"k": "dog", "v": "the user has a dog named Rex"},
    ]), encoding="utf-8")
    win._refresh_facts()
    assert win.facts_list.count() == 2     # deduped by key, newest value wins
    texts = [win.facts_list.item(i).text()
             for i in range(win.facts_list.count())]
    assert any("Bob" in t for t in texts)
    assert not any("Alice" in t for t in texts)
    assert any("Rex" in t for t in texts)

    rex_row = next(i for i in range(win.facts_list.count())
                   if "Rex" in win.facts_list.item(i).text())
    win.facts_list.setCurrentRow(rex_row)

    class _Yes(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.Yes

    saved = settings_app.QMessageBox
    settings_app.QMessageBox = _Yes
    try:
        win._forget_fact()
    finally:
        settings_app.QMessageBox = saved
    # only the exact fact is removed from the file; key dupes stay until the
    # bubble's next merge — but the PANE shows the deduped view immediately
    assert json.loads(memory_file.read_text(encoding="utf-8")) == [
        {"k": "name", "v": "the user's name is Alice"},
        {"k": "name", "v": "the user's name is Bob"}]
    assert list(config_dir.glob("memory.json.bak-facts.*")), \
        "forget must keep a backup"
    win._refresh_facts()
    assert win.facts_list.count() == 1
    assert "Bob" in win.facts_list.item(0).text()

    # 'No' in the confirm dialog leaves the fact in place
    win.facts_list.setCurrentRow(0)
    class _No(QMessageBox):
        @staticmethod
        def question(*a, **k):
            return QMessageBox.No

    settings_app.QMessageBox = _No
    try:
        win._forget_fact()
    finally:
        settings_app.QMessageBox = saved
    assert len(json.loads(memory_file.read_text(encoding="utf-8"))) == 2

    memory_file.unlink(missing_ok=True)
    win._refresh_facts()
    assert win.facts_list.item(0).text() == "(no durable facts yet)"


@scenario
def decision_log_renders_and_tolerates_garbage():
    win._refresh_decisions()
    assert "no decisions logged yet" in win.decisions_view.toPlainText()

    entries = [
        {"id": "abc-1", "ts": "2026-09-10T12:00:00+02:00",
         "tool": "run_command", "target": "pactl set-sink-mute @DEFAULT_SINK@ 1",
         "decision": "ALLOW", "result": "dispatched"},
        "not json at all\\n",
        {"id": "abc-2", "ts": "2026-09-10T12:00:05+02:00",
         "tool": "reboot_system", "target": "now",
         "decision": "CONFIRM", "result": "proposed"},
    ]
    decisions_file.write_text(
        "".join(e if isinstance(e, str) else json.dumps(e) + "\\n"
                for e in entries),
        encoding="utf-8")
    win._refresh_decisions()
    text = win.decisions_view.toPlainText()
    assert "2 decisions — newest last:" in text
    assert "run_command" in text and "pactl set-sink-mute @DEFAULT_SINK@ 1" in text
    assert "ALLOW" in text and "dispatched" in text
    assert "CONFIRM" in text and "reboot_system" in text
    assert "proposed" in text and "#abc-2" in text
    assert "not json" not in text   # garbage line skipped, not fatal

    decisions_file.unlink(missing_ok=True)
    win._refresh_decisions()
    assert "no decisions logged yet" in win.decisions_view.toPlainText()


if __name__ == "__main__":
    name = sys.argv[1]
    try:
        SCENARIOS[name]()
    except SystemExit as e:   # pytest.exit / aborts re-raised cleanly
        sys.exit(int(e.code or 0))
    except BaseException as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
"""


def _run_scenario(name: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "SGUI_HOME": str(tmp_path),
        "SGUI_HERE": str(HERE),
    })
    return subprocess.run(
        [sys.executable, "-c", GUI_DRIVER, name],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(HERE),
    )


SCENARIO_NAMES = [
    "save_roundtrip",
    "save_refuses_without_model",
    "external_change_reloads_and_reports",
    "missing_settings_file_mtime_is_zero",
    "conversation_pane_renders_roles_and_tool_calls",
    "clear_history_keeps_backup",
    "facts_pane_lists_forgets_and_dedups",
    "decision_log_renders_and_tolerates_garbage",
    "history_tab_renders_and_truncates",
    "keybinds_snippet_written_to_tmp_config",
    "restart_and_log_guards",
    "apply_autostart_disabled_is_noop",
    "autostart_enable_migrates_old_line",
    "history_view_renders_decisions",
    "health_line_reports_not_running",
    "fmt_health_ok_and_degraded",
    "health_query_bad_inputs",
    "policy_rows_roundtrip",
    "remote_ollama_checkbox_roundtrip",
    "model_picker_and_voice_combo",
    "tabs_and_colors",
    "clear_history_via_stubbed_dialog",
]


@pytest.mark.parametrize("name", SCENARIO_NAMES)
class TestSettingsGui:
    """Every scenario constructs the real window offscreen in a fresh child."""

    def test_scenario(self, name, tmp_path):
        r = _run_scenario(name, tmp_path)
        assert r.returncode == 0, (
            f"scenario {name} failed:\n{r.stderr[-3000:]}")
