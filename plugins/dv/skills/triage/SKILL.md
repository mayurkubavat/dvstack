---
name: triage
description: Overnight regression triage. Use on a nightly or CI regression, a folder of failing simulator logs, or when asked which failures are new. Groups failures by error signature, skips signatures already owned or already fixed on main, reproduces and shrinks each new one, runs dv:why-fail on it and opens one draft PR per new signature, so people only see new kinds of failure. Never fixes RTL on main and never hides a failing seed from the gate.
---

# dv:triage

A regression with 300 failures usually holds three bugs. Your job is to turn the
failures into signatures, drop the ones nobody needs to see, and hand each new one
to a person as a draft PR that already has a short repro and a root-cause note.

## Rules

- `control-eda triage run` decides the buckets. Do not regroup failures by reading
  logs yourself, and do not call two signatures "the same bug" without a
  `dv:why-fail` note that shows it (`[measured]` or `[inferred]`).
- The signature store (`triage/signatures.json`) only decides what people are
  shown. It is not a waiver: the gate (`dv:verify`) still fails on every failing
  seed. Never add a seed to a skip list, lower `[sim].random_seeds`, or edit a
  test to make a signature go away.
- Only draft PRs, only on `triage/<signature>` branches. Never push to `main`,
  never mark a triage PR ready, never merge.
- Missing tools are reported, not guessed around. A `tool_missing` reproduce,
  main or shrink field goes into the PR body as is, with its install hint.
- Label every claim `measured`, `inferred` or `guessed`.

## Procedure

### 1. Collect and bucket

Pick the input that matches what you were given:

```bash
control-eda triage run --json                         # failures already in evidence/sim
control-eda triage run --regress 20 --json            # run every test: fixed seeds + 20 random
control-eda triage run --logs 'nightly/**/*.log' --json   # logs from any simulator
```

Add `--main origin/main` if the default (`[triage].main`, else `origin/main`, else
`main`) is not the branch fixes land on. The command writes `triage/latest.json`
and `triage/report.md` and exits 0 only when the verdict is `CLEAN`.

Read `verdict` first:

- `CLEAN`: every signature is known or already fixed on main. Report the one-line
  summary and stop.
- `INCONCLUSIVE`: the regression could not run (`inconclusive[]` says why). Report
  it with the install hint. Do not report "no failures".
- `NEEDS_HUMAN`: work through `needs_human[]` below.

Each bucket has `status` (`new`, `known`, `regressed`), `main.state`
(`fixed`, `still_failing`, `changed`, `same_commit`, `not_available`,
`tool_missing`), `reproduce.state`, `shrink` and `action`.

### 2. Per bucket, by `action`

**`none`**: known (it has an owner, `known.link`) or fixed on main
(`main.commits` lists the candidate fixing commits, `[inferred]`). Count it in the
summary; do nothing else.

**`human`**: a regressed signature (a fixed bug is back) or one that did not
reproduce here (flaky or environment-dependent). Do not open a PR. Put it at the top
of the summary with its old link from `known`, and say which.

**`draft_pr`**: a new signature that still fails. For each one:

1. Run `dv:why-fail` on the shrunk repro, not the original seed. The command is in
   `shrink.command` (or `pr.repro` when shrinking was not possible). Its
   triage note is the root-cause section of the PR.
2. Create the branch `pr.branch` from the failing commit. Commit two things:
   - `triage/<signature>.md`: `pr.body` with the root-cause placeholder replaced
     by the `dv:why-fail` triage note.
   - The shrunk seed as a pinned regression seed: append it to `[sim].seeds` in
     `dv.toml` only if `[triage].pin_seeds = true`. Adding a seed strengthens the
     gate; the branch is red until the bug is fixed, which is what a draft is for.
   Do not change RTL or testbench code on this branch. A fix is a separate change
   that goes through `/dv-mode` (bug-fix playbook) and `dv:verify`.
3. Open a draft PR with `pr.title` and the same body: `gh pr create --draft`, or the
   GitHub tools of the environment you run in. With neither, leave the branch
   pushed (or local) and print the title and body for a person to open.
4. Record the owner so tomorrow's run shows it as known:
   ```bash
   control-eda triage record --sig <signature> --status open --link <PR url>
   ```
   Commit `triage/signatures.json` on the triage branch with the note; it reaches
   main when a person merges the fix.

When a fix merges, whoever merges it runs
`control-eda triage record --sig <signature> --status fixed`; seeing that signature
again is then a regression, shown as `human`.

## Report format

```
TRIAGE: NEEDS_HUMAN   (fifo_block @ 637b46a, 412 failing runs, 5 signatures)

| Signature  | Status    | Runs | On main      | Shrunk     | Action |
| 89bddd416d | new       | 311  | still fails  | 2000 -> 4  | draft PR #57 |
| e8dc0fbbef | new       | 96   | still fails  | 2000 -> 9  | draft PR #58 |
| cab8729858 | regressed | 3    | still fails  | -          | human: was fixed in #41 |
| 51aa02c9e3 | known     | 1    | -            | -          | owned by #50 |
| 7c03d1f2b8 | new       | 1    | fixed (a1b2c3d) | -       | none |

Claims:
- [measured] 412 failing runs fall into 5 signatures (triage/latest.json)
- [measured] 7c03d1f2b8 passes on origin/main at a1b2c3d
- [inferred] a1b2c3d "fix rptr wrap" is the fix (only commit touching fifo.sv since)
- [inferred] 89bddd416d: read pointer advances twice per pop (dv:why-fail note in #57)
```

People read the table and the draft PRs. Everything else stays in
`triage/report.md`.
