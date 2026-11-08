---
description: Route a DV or RTL goal to a playbook, load its steps into the todo list, and end with the evidence gate and independent verification.
argument-hint: <goal, e.g. "fix the FIFO full flag bug" or "add coverage for back-to-back pops">
---

Goal: $ARGUMENTS

Pick exactly one playbook below by reading the goal. Say which one and why in one
line. Then create one todo per step and work through them in order. The last two
steps are always `dv:verify` and a `dv-verifier` subagent run; never skip or reorder them.

## Playbooks

**Bug fix** (a test fails, a symptom is reported)
1. `dv:why-fail` on the failing test and seed; stop if not reproduced.
2. `dv:vplan` lookup for the assertion or coverpoint that should have caught it.
3. Add the missing assertion or coverpoint first, confirm it fails on the bug.
4. Make the smallest RTL or TB change that fixes the root cause.
5. `dv:verify`.
6. Spawn `dv-verifier` on the resulting commit.

**New feature or test** (add behaviour or coverage)
1. `dv:vplan`: add or update the row, `status: planned`.
2. Write the test or sequence against the row's summary. Run it on the current RTL;
   if it passes before the feature exists, it tests nothing.
3. Implement the feature.
4. `dv:verify`.
5. Spawn `dv-verifier`.

**Coverage hole** (bins uncovered)
1. `control-eda cov report --uncovered --json`; list the bins.
2. For each bin, write a directed sequence or constraint. Never exclude a bin.
3. `dv:verify` (coverage must rise).
4. Spawn `dv-verifier`.

**Refactor** (no behaviour change)
1. `control-eda door classify --json`; a one-way door stops here for a human.
2. Make the change.
3. `control-eda lec --base <parent sha> --json` must report zero non-equivalent points.
4. `dv:verify`.
5. Spawn `dv-verifier`.

**Repeated review comment**
1. `dv:correct`.

## Rules for every playbook

- Report success only with the evidence bundle path and the verifier's verdict.
- A one-way door (ports, CSR map, clock/reset, waivers, ECOs) is finished at
  "merge-ready, human merge required", never "merged".
- Label every claim in the final report measured, inferred or guessed.
