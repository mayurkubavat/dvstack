---
description: Verification gate rules for RTL and testbench work. Always on in dv-enabled projects.
alwaysApply: true
trigger: always_on
---

# dv gate

This project uses the dv plugin. These rules hold whether or not hooks are
enforcing them.

1. A change to RTL or testbench files is done only when `control-eda verify`
   has written `evidence/<sha>.json` with verdict PASS. Report the verdict and
   the bundle path; never say "done" without them.
2. Never weaken the gate to get green: do not remove or comment out an
   `assert property`, add `disable iff`, add `ignore_bins`, remove
   `illegal_bins`, add inline lint or coverage waivers, drop seeds from
   `dv.toml`, or lower the coverage floor. If the gate is wrong, stop and tell
   the human; they make that edit.
3. Reproduce before you diagnose: `control-eda sim --test <t> --seed <s> --trace`
   then `control-eda wave-query` on the dump. Name a root cause only after
   opening the files involved.
4. Label every claim in a report `measured`, `inferred` or `guessed`. A guessed
   claim cannot support PASS.
5. One-way doors (port lists, CSR maps, clock and reset trees, waivers, ECOs)
   end at "merge-ready, human merge required". Two-way doors (sequences,
   coverage, mutation-proven SVA, LEC-proven refactors, lint cleanups) may be
   verified by the `dv-verifier` agent before merge.
6. Read `vplan.yaml` before touching a block; add a `planned` row for any RTL
   file that has none.
