# handsoff — audit 2026-09-15

Method: structural reads + `grep`/`ast` census + `wc` + `pytest --collect-only`
(1390). Five scout subagents dispatched; all five hit free-model rate limits
and returned nothing — findings below are first-hand, each with its source.

## Verdict

No proper spec engineering existed: README (manual) + ACCEPTANCE (desk
checklist) + GAP_ANALYSIS (audit log) + 4 point designs + 2 plans, but no
vision/requirements/architecture/API/data/ops/test-plan. Created as
`specs/00/10/20/30/40/50/60` (this file = `90`). Ship it.

## Strengths (keep)

- Single sources: schema owns defaults+vocabularies+looks; `@tool` owns
  schemas+prompt+permissions; manifest glob owns the shipped set; `Offer` /
  `BoundedRegistry` own admission; `look_matching()` derives the look.
- Loud-degradation culture: boundary fault injection, no fabricated success,
  refusal records on disk + spoken + journaled.
- Deterministic gates: coverage floor, order shuffle ×2, thread-crash=error,
  discovered compile/shell sets, installer smoke, versioned pre-commit.
- Secret hygiene: 0600 + lstat-symlink-refuse, redacted log targets, ICS
  label redaction, secret-path predicate at three entries, token-gated
  mutating socket verbs.

## Ranked risks

1. **`_DEPLOY_FILES` drift (open).** `handsoff.py:628` lists 8 files
   (`handsoff.py handsoff-settings.py settings_schema.py hardware.py
   core/__init__ core/settings core/doctor handsoff-restart`) while
   install.sh `CORE_REQUIRED` ships 13 core modules (+ audio brain tools
   lifecycle calendar assistant bubble web theme registry). Safety today:
   `_deployment_snapshot` unions the manifest glob (ceiling) over the tuple
   (floor), so a manifest install still compares all 13+. Exposure: a
   manifest-less/hand-rolled install compares only the 8 and reports
   `in-sync` while 5+ modules differ. Fix: derive the floor from
   `CORE_REQUIRED` or assert equality in `test_ops.py`.
2. **Dual model-mirror caches (mitigated, watch).** `core/audio` owns caches;
   `handsoff.py` mirrors `_tts_model`/`_whisper_model` for `_speak`/health/
   doctor/tests. Read-then-assign once let a reload drop be overwritten by a
   stale in-flight read. Current fix (push reads module copy inside the
   lock, drop takes the same lock, adopt refuses republish of a dropped
   model) is ordering-sensitive — any new reader/writer of the mirrors must
   take the same lock or the defect returns.
3. **Monolith mass (accepted, cutting).** 7057 + 4812 + 4069 + 3330 lines in
   four files. Cut plan works ((a)/4c/4d/4e done) but every new feature
   landed in `handsoff.py` lengthens the critical path the suite + pre-commit
   already take ~2.5 min to guard. Rule: new seams go to `core/` with a
   `H.*` alias, never new globals in the app.
4. **Control-token trust boundary is same-UID (documented).** 0700 dir +
   peer-uid stop other users; a same-UID compromised child passes uid and is
   stopped only by the token — which any process that can read `STATE_DIR`
   can also read. Mutating verbs are safe from sandboxed-app accidents, not
   from a same-user attacker. Do not oversell it.
5. **Regex watcher residual (documented).** Length + exponential-shape guards
   hold; `(a|aa)+` overlap blowup remains expressible. Blast radius is one
   daemon watcher thread; per-line work bounded by `WATCH_LINE_MAX`. Revisit
   only with first-charset analysis, not more length caps.
6. **Ydotool probe is connect-not-round-trip (documented).** Alive-but-wedged
   daemon reads reachable. Round-trip needs the wire struct; `/proc`-inode
   alternative breaks root-owned daemons (measured). Keep the limitation
   stated in doctor output.
7. **Root scratch near-misses (mitigated).** `optimus*.wav`/`output.wav`/
   `test.py` once nearly shipped/committed. `.gitignore` now covers
   root-level audio + `test.py`; installer ships declared+tracked only.
   Watch: any new root scratch extension reopens the hole — prefer `attic/`
   (excluded from cover/compile/ship) or `tests/` fixtures.
8. **Settings GUI is the recovery tool and the biggest unmeasured surface.**
   ~68% via offscreen drivers; multi-output OCR unreliable (fell back to
   data-layer assertions). Appearance-tab live-apply paths need a human eye
   after palette/painter changes — keep the ten-design ink guard green.

## No-inference notes (checked, not assumed)

- No `eval/exec(untrusted)`: only `compile()` for self-edit preview/verify
  (`core/tools.py:837,3296,3305`) + `app.exec()` (Qt) + `__import__` in the
  origin-checked loader. No `pickle`, no `os.system`, no `shell=True`.
- Subprocess is argv-list only (`subprocess.run([...])`), never a shell
  string; shell operators are refused before `shlex.split`.
- `my_tool` (line 441) is a docstring example inside `tool()` — NOT the
  49th tool. AST census = 48.
- `stop_watchers` is a plain method, not a 49th `@tool`.
- `paste_text`/`get_datetime`/`read_file` etc. with no explicit `gates=`
  default to their OWN name — gated, not ungated. Only `gates=''` is ungated
  (`wait`, `niri_capabilities`, `confirm_action`, `handsoff_doctor`).
- Test arithmetic: ~1300 `def test_` vs 1390 collected (parametrization).
  Quote collected when talking CI, functions when talking files.

## Follow-ups (small, ordered)

1. Unify `_DEPLOY_FILES` with `CORE_REQUIRED` (or test their equality).
2. DONE (2026-09-16) — `tests/test_specs_freshness.py`, its own file rather
   than a class in `test_regression.py` (and listed in `60-test-plan.md`): each
   count is read from the source of truth the spec names for itself — 48 tools,
   59 settings keys, 22 PTT verbs, 13 core modules, plus 19 permission keys —
   and every place a spec states one is read back and compared. It also pins
   STRUCTURE, which a count cannot see: every core module in the architecture
   map, every test file in the test plan, every spec in this index. Size claims
   are deliberately left unpinned, because a line count is a dated snapshot.
   First run, one real gap: `test_settings_contract.py` was in no row.
3. Keep GAP_ANALYSIS append-only; link new batches here, do not merge.
4. Next cut seam: voice pipeline (`Recorder`/`ContinuousListener`/speak)
   behind a `core/voice.py` handle — the largest coherent block left in the
   app that tests already address through `H.*`.
