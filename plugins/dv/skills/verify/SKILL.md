---
name: verify
description: Evidence gate for RTL and testbench changes. Use before claiming any DV or RTL change is done, before opening a PR, or when asked to verify, prove, or check a design change. Runs doctor, lint, simulation with fixed and random seeds, coverage merge and anti-vacuity checks through control-eda and writes evidence/<sha>.json. Refuses to report success without that file.
---

# dv:verify

You are the evidence gate. A change is done when `evidence/<sha>.json` exists, every
step in it passed, and every claim in it is labelled. Nothing else counts, including
your own confidence.

## Rules that never bend

1. Run the real tools through `control-eda`. Never reason about what a simulation
   would do. If a tool is missing, stop and report `INCONCLUSIVE`, never `PASS`.
2. Never weaken the gate to get green. You may not relax an assertion, add
   `disable iff`, exclude a coverage bin, lower a coverage target, add a waiver,
   shrink the seed list, or shorten a test. If the gate fails, the change is not
   done. Say so.
3. Label every claim in your report `measured` (you read it from a tool result),
   `inferred` (you derived it from measurements) or `guessed`. A guessed claim
   cannot support a PASS.
4. Keep the evidence. Cleanup never deletes `evidence/`.

## Procedure

Work through these in order. Each step ends in a file; the next step reads it.

### 1. Doctor

```bash
control-eda doctor --json
```

If `ok` is false, stop. Report what is missing and the command the output names.

### 2. Scope

```bash
control-eda scope --json            # changed RTL/TB files vs the base branch
control-eda vplan lookup --files <changed files> --json
```

This gives the vplan rows (tests, covergroups, assertions) the change touches.
If a changed RTL file has no vplan row, say so in the report under `gaps`. Do not
invent a row; `dv:vplan` owns that.

### 3. Lint

```bash
control-eda lint --json
```

Zero errors required. Warnings are listed in the evidence, not hidden.

### 4. Simulate

Run every test the vplan names for the touched files, each with the fixed seeds in
`dv.toml` (`[sim].seeds`) plus `[sim].random_seeds` fresh random seeds:

```bash
control-eda sim --test <name> --seed <n> --cov --trace --json
```

On a failure, do not retry with a different seed to hide it. Capture the wave:

```bash
control-eda wave-query <fst-or-vcd> --at <fail time> --window 20 --signals '<dut>.*' --json
```

and attach the cycle table to the evidence under `sim.failures[]`.

### 5. Coverage

```bash
control-eda cov merge --json
control-eda cov diff --base <base sha> --json
```

Coverage must not drop, and `excluded_bins_added` must be 0.

### 6. Anti-vacuity on new or changed SVA

For each `assert property` added or edited in the diff:

- Its `cover property` twin must have hits in the coverage data. No cover, no pass.
- Run the fault-injection check: `control-eda sim --test <name> --seed <n> --define <FAULT_DEFINE> --expect-fail --json`
  with a define the RTL exposes (see `examples/fifo_block/rtl/fifo.sv`). If a
  project has no fault defines, record `mutation: "not_available"` and the gate
  result becomes `INCONCLUSIVE` for that assertion, not `PASS`.

If `dv.toml` has a `[formal]` section, `control-eda verify` also runs
`control-eda formal check` (proof, cover witness, fault counterexample, assume
audit; see the `formal` plugin) and folds it into the bundle: a counterexample or a
vacuous assertion is FAIL. With no formal tool installed it is recorded as a gap
naming what to install, and the verdict rests on simulation unless
`[formal].required = true`. Report that gap; never say the assertions were proven.

### 7. Bundle

```bash
control-eda evidence bundle --json > evidence/<sha>.json
control-eda evidence check evidence/<sha>.json
```

`evidence check` validates the bundle against `schemas/evidence.schema.json` and
prints the verdict: `PASS`, `FAIL` or `INCONCLUSIVE`. Report that word, then the
summary table, then the gaps. The `evidence-gate` hook reads the same file and will
not let the session stop without it when RTL or TB files changed.

## Report format

```
VERDICT: PASS | FAIL | INCONCLUSIVE   (sha abc1234, block <name>, door two-way|one-way)

| Step  | Result | Evidence |
| lint  | 0 errors, 2 warnings | evidence/lint.log |
| sim   | 6/6 tests, seeds [1,42,9137,+3 random] | evidence/sim/*.log |
| cov   | 87.2% -> 88.9%, 0 bins excluded | evidence/cov.json |
| sva   | p_no_drop: proof pass, cover hit, mutation caught | evidence/sva.json |

Claims:
- [measured] all 6 tests pass on all 6 seeds
- [inferred] the fix closes the hang reported in #42 (the failing seed 9137 now passes)
- [guessed] none

Gaps: fifo_ctrl.sv has no vplan row.
```

A `door: one-way` bundle (ports, CSR map, clock/reset, waivers, ECOs, as classified
by `door-policy.yaml`) is still verified the same way, but say plainly that it needs
a human merge.
