# attic/

One-shot historical patchers, kept for provenance only. These scripts were
already applied to the codebase and are NOT supported tooling — they hardcode
anchors that have since drifted, so re-running them fails (by design: they
abort on any anchor miss).

- `patch_wakeword.py` — one-time source patch that added the openWakeWord
  spotter (now a maintained part of `handsoff.py`, covered by `TestWakeSpotter`).

This directory is deliberately outside CI: it is excluded from the coverage
report (`.coveragerc`) and from both workflows' `py_compile` sets, so a rotten
anchor here can never fail a build. Nothing outside `attic/` imports it.
