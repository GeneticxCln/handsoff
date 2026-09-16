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
        The literal writes that remain are the keys a bespoke panel owns — the
        generated ones are read in a single loop, which is the next guard.
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
        assert len(written) >= 8, written          # never vacuous
        undeclared = sorted(written - set(schema.DEFAULT_SETTINGS))
        assert undeclared == [], (
            f"the form writes setting(s) nothing declares: {undeclared} — add "
            "a row to settings_schema.SETTINGS_FIELDS")

    def test_the_form_collects_every_generated_control_in_one_loop(self):
        """The generated half of the save path, pinned to the mechanism.

        `_load_values` and `_collect` both walk `self._controls`, which is what
        makes a new row need no line in either. Counting the literal writes
        above would call that loop-covered key MISSING, so the loop is asserted
        directly: a generated key collected by hand would be the second source
        of truth this table exists to remove.
        """
        mod = _load("handsoff_settings_collect_loop", HERE / "handsoff-settings.py")
        for name in ("_collect", "_load_values"):
            src = textwrap.dedent(inspect.getsource(getattr(mod.SettingsWindow, name)))
            reached = set()
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.For):
                    continue
                it = node.iter
                if not (isinstance(it, ast.Call)
                        and isinstance(it.func, ast.Attribute)
                        and it.func.attr == "items"
                        and isinstance(it.func.value, ast.Attribute)
                        and it.func.value.attr == "_controls"):
                    continue
                reached.update(n.id for n in ast.walk(node.target)
                               if isinstance(n, ast.Name))
            assert "key" in reached, (
                f"`{name}` no longer reads the generated controls from "
                "`self._controls` — a new table row would then need a line "
                "added here, which is the hand-written path this contract "
                "removes")


class TestTheWindowDrawsTheTable:
    """Every setting has a control, and the window's controls are the table's rows.

    A setting used to be drawn wherever somebody remembered to draw it, so a new
    one cost a widget here, a load line there and a collect line somewhere else —
    and the three forgot at different times, which is how a live row ends up with
    nothing on screen and a control ends up saving nothing. The window builds its
    controls FROM the table now, and draws a row in the SHAPE the row asks for,
    so this holds those promises without Qt: every control and every shape a row
    names is one the window implements, no shape is dead code, and a row the
    table cannot build a control for is one something actually draws.
    """

    def _window_module(self):
        return _load("handsoff_settings_controls", HERE / "handsoff-settings.py")

    def test_every_control_a_row_names_is_one_the_window_implements(self):
        schema = _schema()
        built = set(self._window_module()._CONTROL_BUILDERS)
        assert built, "no control builders — this guard would be vacuous"
        assert built <= set(schema.CONTROL_NAMES), (
            "the window implements a control the schema does not declare, so "
            "no row can ask for it: "
            f"{sorted(built - set(schema.CONTROL_NAMES))}")
        for field in schema.SETTINGS_FIELDS:
            control = schema.control_for(field)
            assert control, (
                f"{field.key} names no control and its kind {field.kind!r} has "
                "no default one — nothing would draw it")
            assert control in schema.CONTROL_NAMES, (
                f"{field.key} names control {control!r}, which the schema does "
                "not declare")
            if control not in ("custom", "none"):
                assert control in built, (
                    f"{field.key} asks for a {control!r} control that "
                    "handsoff-settings.py does not build — the row would be "
                    "logged and skipped, and the setting unreachable")

    def test_every_row_is_drawn_by_its_control_or_a_shape_that_exists(self):
        """A row is drawn one of two ways, and BOTH ends of both ways must exist.

        Either the table's generated control draws it, or the row names a SHAPE
        (`render`) — and a shape nothing implements is a setting with no widget,
        exactly as a control name nothing implements would be. A `custom` row is
        the one case the table cannot build, so it must name a shape of its own
        or be drawn as another row's companion; there is no third way in, which
        is what makes "a new setting needs one line" safe. The shape registry is
        also held against the declared vocabulary in both directions: a name with
        no method and a method no name reaches are the same defect from the two
        sides, and a shape no row asks for is dead code that hides a rename.
        """
        schema = _schema()
        mod = self._window_module()
        built = set(mod.SettingsWindow.RENDERERS)
        assert built, "no renderers — this guard would be vacuous"
        assert built == set(schema.RENDER_NAMES), (
            "implemented but not declared: "
            f"{sorted(built - set(schema.RENDER_NAMES))}; declared but not "
            f"implemented: {sorted(set(schema.RENDER_NAMES) - built)}")
        claims = schema.claimed_keys()
        for field in schema.SETTINGS_FIELDS:
            name = schema.renderer_for(field)
            if name:
                assert name in built, (
                    f"{field.key} asks to be drawn as {name!r}, which "
                    "handsoff-settings.py does not implement — the row would be "
                    "named in the journal and skipped")
            if schema.control_for(field) == "custom":
                assert name or field.key in claims, (
                    f"{field.key} is a custom row that nothing draws: no render "
                    "of its own, and no row claims it in `also`")
        used = {field.render for field in schema.SETTINGS_FIELDS if field.render}
        assert sorted(set(schema.RENDER_NAMES) - used) == [], (
            f"shape(s) no row asks for: {sorted(set(schema.RENDER_NAMES) - used)}")

    def test_companion_rows_are_declared_well_formed_and_drawn_once(self):
        """`also` is a placement claim, so a broken one is a lost setting.

        A companion is skipped at its own table position and drawn with the row
        that claims it, so the claim has to be exact: a declared row, never the
        claiming row itself, never claimed twice (the second owner would simply
        never see it), in the OWNER's card, and AFTER the owner in that card's
        order — a companion drawn before its owner is drawn where it is and the
        claim quietly does nothing.
        """
        schema = _schema()
        place = {field.key: (field.tab, field.group)
                 for field in schema.SETTINGS_FIELDS}
        order = {key: [f.key for f in schema.group_fields(tab, group)]
                 for key, (tab, group) in place.items()}
        claimed: dict = {}
        for field in schema.SETTINGS_FIELDS:
            for key, _label in schema.also_pairs(field):
                assert key in place, (
                    f"{field.key} claims {key!r}, which the table has no row for")
                assert key != field.key, f"{field.key} claims itself"
                assert key not in claimed, (
                    f"{key} is claimed by both {claimed.get(key)} and {field.key}")
                claimed[key] = field.key
                assert place[key] == place[field.key], (
                    f"{field.key} claims {key} from another card: "
                    f"{place[key]} is not {place[field.key]}")
                assert (order[field.key].index(key)
                        > order[field.key].index(field.key)), (
                    f"{key} is declared BEFORE the row that claims it "
                    f"({field.key}), so the claim would do nothing")
        assert claimed, "no `also` claims: this guard would be vacuous"

    def test_presentation_data_only_sits_on_rows_that_can_show_it(self):
        """A placeholder on a checkbox, a height on a spin box: data nobody reads.

        Each of these is a row asking to be drawn in a way its control cannot,
        and the failure is perfectly quiet — the widget simply ignores it — which
        is why the table has to be checked instead of the screen. The labels a
        combo spells are checked the same way: a label for a value the row does
        not offer is a typo that changes nothing.
        """
        schema = _schema()
        seen = {"placeholder": 0, "height": 0, "choice_labels": 0, "explain": 0}
        for field in schema.SETTINGS_FIELDS:
            control = schema.control_for(field)
            if field.placeholder:
                seen["placeholder"] += 1
                assert control in ("line", "lines"), (field.key, control)
            if field.height:
                seen["height"] += 1
                assert control == "lines", (field.key, control)
            if field.explain:
                seen["explain"] += 1
            if field.choice_labels:
                seen["choice_labels"] += 1
                assert control == "combo", (field.key, control)
                offered = {str(choice) for choice in field.choices}
                spelled = {str(value) for value, _words in field.choice_labels}
                assert spelled <= offered, (
                    f"{field.key} spells a value it does not offer: "
                    f"{sorted(spelled - offered)}")
        # never vacuous: every one of these is in use somewhere in the table
        assert all(seen.values()), seen

    @staticmethod
    def _main_tab_names(mod) -> set:
        """The pages the window actually builds, read from `__init__`'s addTab.

        Read rather than listed: a page renamed in the window must fail the guard
        below, and a list here would have to be renamed with it — which is the
        kind of agreement-in-two-places this file exists to refuse. Only the MAIN
        tabs count (the History page's own sub-tabs are not settings pages), so
        the call has to be on the `tabs` widget itself.
        """
        src = textwrap.dedent(inspect.getsource(mod.SettingsWindow.__init__))
        names = set()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            if getattr(node.func, "attr", "") != "addTab":
                continue
            if getattr(getattr(node.func, "value", None), "id", "") != "tabs":
                continue
            label = node.args[1]
            if isinstance(label, ast.Constant) and isinstance(label.value, str):
                names.add(label.value.lower())
        return names

    def test_a_generated_row_declares_the_label_and_the_page_it_needs(self):
        """A widget with no label names nothing, and one on no page is unreachable.

        The page is also checked against the main tabs the window builds, read
        from `__init__`: a row assigned to a page that does not exist is the
        quietest version of this failure — the control is built, registered and
        never shown.
        """
        schema = _schema()
        mod = self._window_module()
        for field in schema.SETTINGS_FIELDS:
            if schema.control_for(field) in ("custom", "none", ""):
                continue
            assert field.title, (
                f"{field.key} declares no title, so its row would be labelled "
                "with the setting's own name")
            assert field.title != field.key, (field.key, field.title)
            assert field.tab, (
                f"{field.key} builds a control that no page of the settings "
                "window shows")
        pages = self._main_tab_names(mod)
        assert len(pages) >= 5, pages
        wanted = {field.tab for field in schema.SETTINGS_FIELDS if field.tab}
        assert wanted <= pages, (
            f"row(s) assigned to pages the window never builds: "
            f"{sorted(wanted - pages)}")

    def test_every_row_lands_in_a_card_the_table_declares(self):
        """A row's card IS its placement, so a card nothing declares is no card.

        The window draws a page by walking the cards the schema declares and
        placing each card's rows, so a row whose `group` names no declared card
        reaches no widget at all: nothing raises, nothing is logged, the row is
        simply never iterated. That is the quietest way a new setting can end up
        invisible, and it is why `group` has to be a declared name rather than a
        free string.
        """
        schema = _schema()
        cards = tuple(schema.PAGE_GROUPS)
        assert cards, "no cards declared: this guard would be vacuous"
        for row in cards:
            assert row[2], f"card {row[0]}/{row[1]} has no title to show"
        declared = {(row[0], row[1]) for row in cards}
        for field in schema.SETTINGS_FIELDS:
            if schema.control_for(field) == "none":
                continue            # runtime bookkeeping: never drawn at all
            assert field.group, (
                f"{field.key} names no card, so no page would ever place it")
            assert (field.tab, field.group) in declared, (
                f"{field.key} asks for card {field.group!r} on page "
                f"{field.tab!r}, which nothing declares — its control would be "
                "built and never shown")

    # The guard that read the keys a PAGE named by hand — the helper calls and
    # row-special registrations this class used to walk — is GONE rather than
    # re-aimed: the pass that gave every row its own `render` removed the last
    # such call (`_control_for_key("mic_threshold")` became `field.key`), so
    # there is no key left to mis-spell. What a page still names directly is the
    # WIDGET of a row it reads, and the guard below covers exactly that.

    def test_every_attribute_the_window_reads_is_a_generated_control(self):
        """`self.ollama_host` is a reference to a widget the TABLE builds.

        The generated controls are registered under the setting's own name
        (`setattr(self, field.key, control.widget)`), which is convenient and
        load-bearing: the pages read a few of them back by name — the server URL
        while refreshing models, the reference clip beside its Choose button, the
        threshold the live meter restarts on. A row that stops being generated
        (becoming `custom`) leaves those references dangling, and the window
        raises at OPEN, which is the worst place: it is the tool you open when
        the bubble is already broken.
        """
        schema = _schema()
        mod = self._window_module()
        declared = set(schema.DEFAULT_SETTINGS)
        generated = set(schema.generated_keys())
        src = textwrap.dedent(inspect.getsource(mod.SettingsWindow))
        referenced = set()
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                    and isinstance(node.ctx, ast.Load)):
                continue
            if node.attr in declared:
                referenced.add(node.attr)
        assert len(referenced) >= 3, sorted(referenced)   # never vacuous
        dangling = sorted(referenced - generated)
        assert dangling == [], (
            f"the window reads these settings as widgets, and the table no "
            f"longer generates a control for them: {dangling} — the window "
            "would raise at open; either generate the control again or reach "
            "the value through `_controls`")

    def test_the_only_control_less_rows_are_runtime_bookkeeping(self):
        """`ctrl="none"` is a promise that no PERSON sets this key.

        It is the one escape hatch in the table, so it stays a list a person can
        read: a setting the bubble writes for itself. A user-facing setting
        landing here would be invisible in the window with nothing to say so.
        """
        schema = _schema()
        control_less = sorted(field.key for field in schema.SETTINGS_FIELDS
                              if schema.control_for(field) == "none")
        assert control_less == ["tool_call_times"], (
            f"a setting with no control and no bespoke panel: {control_less} — "
            "either give it a control or move it out of the settings table")
        for field in schema.SETTINGS_FIELDS:
            if schema.control_for(field) != "none":
                continue
            assert field.tab == "", (
                f"{field.key} has no control but claims the {field.tab!r} page")


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
