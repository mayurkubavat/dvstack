---
name: review-to-rule
description: Turn a review comment that keeps coming back into a lint rule or an SVA assertion that runs on every change, and prove the rule fires. Use when the same comment shows up a second or third time in RTL or DV review, when a reviewer says "we keep saying this", or when asked to make a lesson permanent. Writes rules.yaml (lint) or an assertion plus fault (SVA), runs control-eda rules test, and fixes existing violations.
---

# formal:review-to-rule

A comment made three times is a missing rule. This skill writes the rule in
`rules.yaml` at the project root, where `control-eda lint` and `control-eda verify`
enforce it for everyone. Lint rules are regexes run by control-eda itself, so they
work on machines with no EDA tools at all. For the wider menu (hooks, CI, code
generation) see `dv:correct`; this skill is the lint and SVA path.

## 1. Gather the evidence

Write down the comment in the reviewer's words and every place it was made (PR
comment links). Fewer than two occurrences: note it in `docs/corrections.md` and stop.

## 2. Choose the kind

| The comment is about | Kind | Example |
| --- | --- | --- |
| text or structure in the source | `lint` | "no `casex`", "no `#` delays in RTL", "no `initial` in synthesizable code", "`always_ff` only for flops" |
| behaviour over time | `sva` | "full must mean DEPTH entries", "valid must hold until ready", "grant is one-hot" |

If a regex can express it without false positives, use `lint`; it is cheaper and
runs everywhere. If it needs cycles, use `sva`.

## 3a. A lint rule

Add to `rules.yaml` (schema: `schemas/rules.schema.json` in dvstack):

```yaml
- id: no-initial-in-rtl
  review: "No initial blocks in synthesizable RTL"
  seen: [https://github.com/org/repo/pull/41#discussion_r1, https://github.com/org/repo/pull/57#discussion_r2]
  kind: lint
  files: ["rtl/**/*.sv"]
  pattern: '^\s*initial\b'
  message: "initial blocks do not synthesize; reset the register instead"
  examples:
    bad: ["initial q = 0;", "  initial begin"]
    good: ["// initial value comes from reset", "always_ff @(posedge clk) q <= d;"]
```

Comments are blanked before matching unless `ignore_comments: false`. Put the
original offending line from the review in `bad`, and the nearest legitimate code
in `good`, so a false positive shows up in the test.

## 3b. An SVA rule

Use `formal:sva` to write the assertion with its cover and a fault define, list
them on the vplan row, then add:

```yaml
- id: fifo-full-exact
  review: "full must mean exactly DEPTH entries"
  seen: [...]
  kind: sva
  assertion: a_full_iff_depth
  fault: FIFO_BUG_FULL_EARLY
```

## 4. Prove the rule fires

```bash
control-eda formal check --json   # sva rules only: produces the counterexample evidence
control-eda rules test --json
```

Each rule must be `fires`. `draft` means it missed a bad example, hit a good one, or
formal found no counterexample for its fault: fix the rule, not the examples.
`unverified` means no formal tool on this machine; the sva rule is written but not
yet proven, so say so and leave it for a machine or CI job that has one. Only
`fires` lint rules are enforced by `control-eda lint`.

## 5. Fix what it finds

```bash
control-eda rules check --json
```

Fix every existing violation in the same change (or a follow-up PR if there are
many). Never fix violations with waivers or by narrowing `files` to dodge them.
Add a row to `docs/corrections.md`: date, comment, layer (lint or SVA), rule id, links.
Finish with `dv:verify`.
