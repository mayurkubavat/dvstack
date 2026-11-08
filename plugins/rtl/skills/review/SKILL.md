---
name: review
description: Review an RTL diff for clock domain crossings, reset handling and X-propagation with two or three independent reviewers on different models, then merge their findings so agreement is visible and every disagreement goes to a human. Use when asked to review RTL, before an RTL PR is marked ready, or when a change touches clocks, resets, synchronizers or case statements.
---

# rtl:review

One model reviewing a diff finds what that model tends to find. Two or three
models reviewing the same diff, blind to each other, show where they agree and
where they do not; the places they disagree are where a human's attention pays.
This skill gives every reviewer the same evidence, keeps them independent, and
merges the results mechanically.

## 1. Evidence first (no EDA tool needed)

```bash
control-eda review scan --base <base-branch> --json
```

This writes `evidence/review-scan.json`: a structural pass over the changed RTL
that finds clock domains, signals crossing them, the two-flop synchronizer
pattern, multi-bit buses through flop synchronizers, async resets not tested
first, resets shared across domains, `casex`, `===` in RTL, X assignments and
combinational `case` without `default`. Verilator lint warnings on the same topics
are added when Verilator is installed, and a site CDC tool (SpyGlass, Questa CDC,
Meridian) is added when `dv.toml` declares it under `[review.tools.<name>]`.
The `tools[]` list says which ran. The scan is a heuristic, not a CDC sign-off:
it is a starting list for the reviewers, not the answer.

Declare asynchronous inputs (`[review].async_inputs = ["irq"]`) so crossings from
outside the module are seen.

## 2. Independent reviews

Run the reviewer agents in parallel, each with the same short brief: the base ref,
the changed files, and the path to `evidence/review-scan.json`. Do not pass one
reviewer's output to another.

| Environment | Reviewers |
| --- | --- |
| Claude Code | `rtl-reviewer-a` (opus), `rtl-reviewer-b` (sonnet), optionally `rtl-reviewer-c` (haiku) as subagents |
| Cursor | the same agents; set their `model:` to two different models your plan allows |
| Antigravity or any single-model tool | run the reviewer skill two times in fresh conversations; the merge will say `single_model` |

Each reviewer writes `reviews/<reviewer>.json` (schema: `schemas/review.schema.json`
in dvstack): `findings` with file, line, category (`cdc`, `reset`, `xprop`, `other`),
severity, claim and evidence, and `cleared` for places it checked and judged correct.

## 3. Merge

```bash
control-eda review merge --json          # reads reviews/*.json and the scan
```

It writes `evidence/review.json` and `evidence/review.md` (ready to paste into the PR).
Every finding gets a status:

| status | means | who acts |
| --- | --- | --- |
| `agreed` | every reviewer raised it | fix it |
| `majority` | two or more, not all | fix it; a human confirms medium and high |
| `single` | one reviewer only | human decides |
| `contested` | one raised it, another explicitly cleared it | human decides |
| `single_model` | all reviewers ran on one model | human reviews everything |

`tool_backed` lists the scan findings at the same place. `tool_only` lists medium
and high scan findings no reviewer raised: a reviewer missed something, or the scan
is wrong; a human looks at both.

Verdict: `CHANGES_REQUESTED` (an agreed or majority high finding, or a high finding
the scan also saw), `NEEDS_HUMAN` (a disagreement or a tool-only finding),
`COMMENTS`, or `CLEAN`.

## 4. Report

Lead with the verdict and the path to `evidence/review.md`. List the findings that
need a human first, with both reviewers' claims side by side. Do not resolve a
disagreement yourself by picking the reviewer you agree with; that is the human's call.
Fixes for agreed findings go through `dv:verify` like any other change.

## Never

- Never let a reviewer see another reviewer's findings before it writes its own.
- Never drop a finding from a reviewer file, or edit a reviewer's claim, before the merge.
- Never present `single_model` agreement as independent agreement.
