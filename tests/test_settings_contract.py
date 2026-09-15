"""The settings contract: one table, and every layer held to it.

A setting used to be declared in DEFAULT_SETTINGS, coerced in
`core/settings.py`, collected from its widget in `handsoff-settings.py`, and
listed a fourth time in the Appearance tab's live-apply tuple. The last one
drifted twice (`colors`, then `avatar_deco_color`), and each time the user saw
the same thing: "I changed it and nothing happened".

`settings_schema.SETTINGS_FIELDS` is now the single declaration, and these
tests are what make it one: a key that is not in the table, a table row whose
kind the generator cannot apply, a live row the tab does not watch, or a value
`_collect` reads that nothing declared — each fails HERE, at the layer that
forgot, instead of in a tab that silently does nothing.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from conftest import HERE, _load


def _schema():
    import settings_schema
    return settings_schema


def _core_settings():
    from core import settings as core_settings
    return core_settings


class TestTheTableItself:
    def test_the_table_and_the_shipped_defaults_name_the_same_keys(self):
        """The table is the contract, so it must cover exactly the settings.

        Both directions matter: a default with no row is a setting nothing
        coerces (the hole this table exists to close), and a row with no
        default has no value to fall back to when the user's is junk.
        """
        schema = _schema()
        keys = [field.key for field in schema.SETTINGS_FIELDS]
        assert len(keys) == len(set(keys)), (
            "a key is declared twice in SETTINGS_FIELDS: "
            + ", ".join(sorted({k for k in keys if keys.count(k) > 1})))
        assert set(keys) == set(schema.DEFAULT_SETTINGS), (
            "table and defaults disagree — no row for: "
            f"{sorted(set(schema.DEFAULT_SETTINGS) - set(keys))}; "
            "no default for: "
            f"{sorted(set(keys) - set(schema.DEFAULT_SETTINGS))}")

    def test_every_row_uses_a_kind_the_generator_implements(self):
        schema = _schema()
        kinds = set(schema.field_kinds())
        used = {field.kind for field in schema.SETTINGS_FIELDS}
        assert used <= kinds, (
            f"unknown kind(s) {sorted(used - kinds)} — add them to "
            "settings_schema.field_kinds and implement them in "
            "core.settings._apply_field")

    def test_every_custom_row_names_a_coercer_that_exists(self):
        """A `custom` row pointing at a missing function must not reach a run.

        `_apply_field` raises on one, on purpose: a privacy flag quietly losing
        its validation is worse than a loud failure, and this is what keeps the
        loud failure unreachable in shipping code.
        """
        schema = _schema()
        core_settings = _core_settings()
        registered = set(core_settings._CUSTOM_COERCERS)
        named = {field.coerce for field in schema.SETTINGS_FIELDS
                 if field.kind == "custom"}
        assert named, "no custom rows: this guard would be vacuous"
        assert named <= registered, (
            f"rows naming a coercer that does not exist: "
            f"{sorted(named - registered)}")
        assert registered == named, (
            "a custom coercer nothing declares is dead code: "
            f"{sorted(registered - named)}")

    def test_numeric_rows_declare_real_bounds(self):
        """A min/max pair is what keeps a hand-edited number usable."""
        schema = _schema()
        for field in schema.SETTINGS_FIELDS:
            if field.kind not in ("int", "float"):
                continue
            assert field.lo is not None and field.hi is not None, field
            assert field.lo < field.hi, field

    def test_choice_rows_declare_choices(self):
        schema = _schema()
        for field in schema.SETTINGS_FIELDS:
            if field.kind in ("choice", "colour"):
                assert field.choices, field


class TestTheCoercionIsGenerated:
    def test_coercion_visits_every_declared_key(self):
        """The strongest form of the old source-grep guard.

        The previous guard read `coerce_settings`'s source text and looked for
        each key as a string literal, with an exemption set for keys validated
        in a loop. That worked while the function was hand-written and is
        meaningless now that the table drives it: what has to be true is that
        EVERY row is actually applied, so this counts the calls.
        """
        schema = _schema()
        core_settings = _core_settings()
        visited: list[str] = []
        original = core_settings._apply_field

        def spy(settings, field, log):
            visited.append(field.key)
            return original(settings, field, log)

        core_settings._apply_field = spy
        try:
            core_settings.coerce_settings(dict(schema.DEFAULT_SETTINGS))
        finally:
            core_settings._apply_field = original
        declared = [field.key for field in schema.SETTINGS_FIELDS]
        assert visited == declared, (
            "coerce_settings did not apply every row, in table order — "
            f"missing: {sorted(set(declared) - set(visited))}, "
            f"extra: {sorted(set(visited) - set(declared))}")

    def test_every_key_is_actually_replaced_when_the_value_is_junk(self):
        """Visited is not the same as APPLIED.

        A loop that called `_apply_field` and then ignored the result would
        pass the spy above; what a user needs is that a junk value never
        survives into the bubble. Every key is probed with a value its kind
        cannot mean, and must come back as something else.
        """
        schema = _schema()
        core_settings = _core_settings()
        for field in schema.SETTINGS_FIELDS:
            junk = {"bool": "junk-not-a-bool", "int": "junk", "float": "junk",
                    "text": None, "str": 12345, "path": None, "choice": "junk",
                    "colour": "not-a-colour", "str_list": "not-a-list",
                    "custom": "junk"}[field.kind]
            probe = dict(schema.DEFAULT_SETTINGS)
            probe[field.key] = junk
            out = core_settings.coerce_settings(probe)
            assert out[field.key] != junk or out[field.key] is None, (
                f"{field.key} ({field.kind}) let its junk value through: "
                f"{out[field.key]!r}")


class TestTheAppearanceTabReadsTheSameTable:
    # The Appearance tab's controls, as a SPECIFICATION rather than a
    # derivation. Which controls that tab owns is not visible in the code: the
    # tab derives its live-apply set FROM the table, so a row that quietly
    # loses `live=True` makes both sides agree on the smaller set and the
    # control silently stops applying — the exact complaint this refactor
    # exists to end, now reachable one level up. This set is the one place a
    # person has to write the list down, and dropping a row from the table
    # fails here, loudly, instead of in the tab.
    APPEARANCE_CONTROLS = frozenset({
        "bubble_design", "bubble_size", "animation_energy", "bubble_accent",
        "colors", "design_image_path", "design_pack", "avatar_ring",
        "avatar_tint", "avatar_deco_color",
        "design_image_idle", "design_image_listening",
        "design_image_thinking", "design_image_speaking",
    })

    def test_the_live_set_is_exactly_the_appearance_controls(self):
        schema = _schema()
        live = set(schema.live_keys())
        assert live == set(self.APPEARANCE_CONTROLS), (
            "a control the tab owns is not a live row: "
            f"{sorted(set(self.APPEARANCE_CONTROLS) - live)}; "
            "a live row no control owns: "
            f"{sorted(live - set(self.APPEARANCE_CONTROLS))} — mark the row "
            "live=True (or remove it) in settings_schema.SETTINGS_FIELDS")

    def test_every_key_a_look_sets_is_a_live_row(self):
        """One click writes five values, and the live apply only saves what it
        watches — so a look key that is not live is a click that appears to do
        nothing. Derived from the catalogue, not from a second list."""
        schema = _schema()
        live = set(schema.live_keys())
        assert schema.APPEARANCE_LOOKS, "no looks: this guard would be vacuous"
        for entry in schema.APPEARANCE_LOOKS:
            assert "colors" in live, entry["name"]
            for key in entry:
                if key in ("name", "label", "note"):
                    continue
                # A catalogue key names a SETTING through the schema's own
                # mapping (`design` -> `bubble_design`), so this guard cannot
                # be fooled by the one key where the two vocabularies differ.
                resolved = ("colors" if key == "colors"
                            else schema.look_setting_key(key))
                assert resolved in schema.DEFAULT_SETTINGS, (
                    f"look {entry['name']!r} sets {key!r}, which names no "
                    "setting")
                assert resolved in live, (
                    f"look {entry['name']!r} sets {resolved!r}, which the live "
                    "apply does not watch — the click would change nothing")

    def test_the_tab_watches_exactly_the_live_rows(self):
        """The tuple that drifted twice is now derived — this pins that it is.

        `_apply_appearance_live` compares these keys against the disk and reads
        anything it is not watching as "a load, not an edit", so a control whose
        key is missing here applies nothing and saves nothing.
        """
        schema = _schema()
        mod = _load("handsoff_settings_contract", HERE / "handsoff-settings.py")
        live = tuple(schema.live_keys())
        assert len(live) >= 10, live               # never vacuous
        assert tuple(mod.SettingsWindow.APPEARANCE_KEYS) == live, (
            "the Appearance tab's live-apply set is not the table's live rows — "
            f"tab only: {sorted(set(mod.SettingsWindow.APPEARANCE_KEYS) - set(live))}, "
            f"table only: {sorted(set(live) - set(mod.SettingsWindow.APPEARANCE_KEYS))}")

    def test_every_live_row_reports_a_name_a_person_would_use(self):
        """`Applied live: bubble_design` is a leak of the setting's own name."""
        schema = _schema()
        for field in schema.SETTINGS_FIELDS:
            if not field.live:
                continue
            label = schema.field_label(field.key)
            assert label and label != field.key, (
                f"live row {field.key!r} has no label, so the tab would report "
                "the identifier")
            assert "_" not in label, (field.key, label)

    def test_every_key_the_form_collects_is_declared(self):
        """`_collect` reads every control; a key it writes that nothing
        declares would be saved into settings.json and read by nobody.

        Parsed with `ast`: the mapping is one `self.cfg["key"] = ...` per
        control, and a text sweep would also match the comments explaining it.
        """
        schema = _schema()
        mod = _load("handsoff_settings_collect", HERE / "handsoff-settings.py")
        # `dedent`, not `cleandoc`: getsource returns the method at its class
        # indentation, and cleandoc leaves the `def` where the first line was
        # while pulling the body out to column 0.
        src = textwrap.dedent(inspect.getsource(mod.SettingsWindow._collect))
        written = set()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Subscript):
                continue
            target = node.value
            if not (isinstance(target, ast.Attribute) and target.attr == "cfg"):
                continue
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                written.add(key.value)
        assert len(written) > 20, written          # never vacuous
        undeclared = sorted(written - set(schema.DEFAULT_SETTINGS))
        assert undeclared == [], (
            f"the form writes setting(s) nothing declares: {undeclared} — add "
            "a row to settings_schema.SETTINGS_FIELDS")


class TestDeclaredBehaviourChanges:
    """The coercion was rewritten, so its differences must be deliberate.

    A 929-case differential run against the previous implementation found
    exactly these, and nothing else. Each is the OLD behaviour being wrong: a
    JSON null for a text setting used to become the literal string "None", and
    the branch that was meant to strip `mic_device` stripped `tts_reference`
    twice instead. They are pinned here so a later "restore the old behaviour"
    edit has to argue with a test rather than pass unnoticed.
    """

    def test_a_null_text_setting_no_longer_becomes_the_word_none(self):
        schema = _schema()
        core_settings = _core_settings()
        for key in ("model", "ollama_host", "assistant_name"):
            out = core_settings.coerce_settings(
                {**schema.DEFAULT_SETTINGS, key: None})
            assert out[key] == schema.DEFAULT_SETTINGS[key], key
            assert out[key] != "None", key
        out = core_settings.coerce_settings(
            {**schema.DEFAULT_SETTINGS, "home_place": None})
        assert out["home_place"] == ""

    def test_the_microphone_name_is_stripped_like_every_other_text(self):
        schema = _schema()
        core_settings = _core_settings()
        out = core_settings.coerce_settings(
            {**schema.DEFAULT_SETTINGS, "mic_device": "  Yeti  "})
        assert out["mic_device"] == "Yeti"

    def test_autostart_is_a_flag_now(self):
        """It was the one key the old coercion never touched at all."""
        schema = _schema()
        core_settings = _core_settings()
        assert core_settings.coerce_settings(
            {**schema.DEFAULT_SETTINGS, "autostart": "false"})["autostart"] is False
        assert core_settings.coerce_settings(
            {**schema.DEFAULT_SETTINGS, "autostart": "true"})["autostart"] is True
