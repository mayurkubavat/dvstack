---
name: refactor
description: Make a behavior-preserving RTL refactor and prove it with an equivalence check against the parent commit. Use for renames, restructuring, re-coding FSMs or expressions, splitting or merging always blocks, or any change claimed to "not change behavior". The change is done only when control-eda equiv reports zero unproven points.
---

# formal:refactor

"No functional change" is a claim, and equivalence checking is how you measure it.
A refactor is done when `control-eda equiv --base <parent>` says `equivalent`.

## 1. Check the tool and the scope

```bash
control-eda formal detect --json      # selected.equiv: eqy, yosys, conformal, formality or a custom one
git status --short                    # start from a clean tree: the base is a commit
```

If `selected.equiv` is `null`, you can still make the change, but report
`INCONCLUSIVE (no equivalence checker)` and fall back to `dv:verify`. Never call a
refactor equivalent by reading the diff.

Keep a refactor pure. Do not mix in a bug fix or a feature; those go in their own
commit with their own `dv:verify`. Interfaces (ports, parameters) stay the same.

## 2. Make the change, then prove it

```bash
control-eda equiv --base HEAD --json         # uncommitted refactor vs the last commit
control-eda equiv --base main --json         # a branch vs main
control-eda verify --equiv HEAD --claim "[measured] refactor proven equivalent"   # both in one bundle
```

Assertions are stripped before comparing, so adding or editing SVA does not affect
the result; only design logic is compared.

## 3. Read the result

| status | verdict | do |
| --- | --- | --- |
| `equivalent` | PASS | report the points proven and the bundle path |
| `not_proven` | FAIL | read `unproven[]`: those outputs or registers differ, or could not be matched |
| `tool_missing` | INCONCLUSIVE | say so, give the install hint |
| `error` | INCONCLUSIVE | the tool ran but could not read the design; read `log` |

`not_proven` on free Yosys has two causes. Either the change altered behavior (look
at the listed signal first, then rerun `dv:verify` to find a failing seed), or the
refactor renamed internal registers so `equiv_make` could not pair them. In the
second case, keep the old register names, or use a stronger backend (`eqy`,
Conformal, Formality) via `[equiv].backend` in dv.toml. Do not edit the base.

## Never

- Never weaken `[equiv]` settings or pick a weaker backend to get `equivalent`.
- Never call the refactor done with `not_proven` points, however small.
