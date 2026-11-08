---
name: vplan
description: Read and maintain the agent-readable verification plan (vplan.yaml). Use when asked which test, covergroup or assertion covers a feature or file, when a symptom like "the AXI write path hangs" must be mapped to a test, when adding a feature or test, or when checking for RTL files with no verification row. The feature map agents consult before touching a block.
---

# dv:vplan

`vplan.yaml` at the project root is the feature map: one row per feature linking
the RTL that implements it, the tests that exercise it, the covergroups that
measure it and the assertions that guard it. Agents read it before touching a
block, and `dv:verify` uses it to pick what to run.

## Schema (`schemas/vplan.schema.json`)

```yaml
version: 1
block: fifo
features:
  - id: fifo.push_pop            # stable id, dotted
    summary: Data pushed is popped in order
    rtl: [rtl/fifo.sv]
    tests: [tb_fifo]              # names control-eda sim accepts
    covergroups: []               # or cg names
    assertions: [a_no_overflow]   # assert labels in the RTL
    covers: [c_full]              # cover labels that make the asserts non-vacuous
    faults: [FIFO_BUG_DOUBLE_POP] # defines that must make a test fail
    door: two-way                 # or one-way
    status: verified              # planned | written | verified
```

## Commands

```bash
control-eda vplan check --json                 # schema + orphan RTL + rows with no test
control-eda vplan lookup --files rtl/fifo.sv   # rows touching these files
control-eda vplan lookup --query "write hang"  # rows whose summary or id matches
```

## Rules

- Every RTL file in `dv.toml [project].rtl_globs` must appear in at least one row.
  `vplan check` lists orphans; report them, then propose rows with `status: planned`.
- Never mark a row `verified` yourself. `dv:verify` flips it when its evidence
  bundle passes for that feature.
- Changing `door` to `two-way` is a human decision. Propose it; do not do it.
- Keep summaries one line, in the words a reviewer would use for the symptom, so a
  bug report can be matched by `lookup --query`.
- Edit `vplan.yaml` with small diffs. Rows are sorted by id.

## Mapping a symptom

1. `control-eda vplan lookup --query "<symptom words>" --json`
2. If nothing matches, grep the RTL for the signals named in the report, then
   `lookup --files` on the files you find.
3. Name the exact test and seed to run, and the coverpoint or assertion that
   should have caught it. If none exists, that is a vplan gap: add a `planned` row.
