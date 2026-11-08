---
name: correct
description: Turn a repeated review comment or agent mistake into structure the agent cannot skip. Use when the same comment appears twice in review, when a lesson should become a lint rule, a bound SVA module, a hook or a CI check, or when asked to "make sure this never happens again". The rule of three from pstack, applied to RTL and DV.
---

# dv:correct

Advice given twice is a bug in the structure, not in the agent. Move the correction
as high up the layers as it can go, then fix every existing violation in one wave.

## Layers, strongest first

| Layer | Use when | Lands in |
| --- | --- | --- |
| Code generation | The pattern can be made impossible (registers from SystemRDL, one FIFO library) | generators, approved IP library |
| Lint | A textual or structural pattern (`casex`, `#` delays, inline waivers, hierarchical refs outside `bind`) | Verible rule config, `control-eda lint` custom checks, UVMCheck-Linter |
| Assertion | A runtime property (handshake, one-hot, no X after reset) | bound SVA module in `tb/sva/`, listed in `vplan.yaml` |
| Hook | An edit the agent must never make (weakening an assert, adding a waiver) | `plugins/dv/hooks/` |
| CI | A check the PR must pass | `.github/workflows/dv.yml` |
| Skill text | Only when none of the above can express it | the relevant `SKILL.md` |

For the lint and SVA layers, `formal:review-to-rule` writes the rule into the
project's `rules.yaml` and `control-eda rules test` proves it fires; lint rules there
run inside `control-eda lint`, so they need no EDA tool.

## Procedure

1. State the comment in one line and count its occurrences (link them).
2. Pick the highest layer that can express it. Write the rule there.
3. Prove the rule fires: run it against the original violation and show the error.
4. Fix every existing violation: `control-eda lint --json` after adding the rule
   lists them. Fix them all in one PR, separate from the rule PR if the count is large.
5. Add a line to `docs/corrections.md`: date, comment, layer, rule name, PR links.

## Rules

- A rule that has never fired on the original mistake is a draft. Do not merge drafts.
- Prefer deletion: if the comment is "don't use X", remove X from the paved road
  (the approved library or template) before writing a lint that bans it.
- Never fix the existing violations by adding waivers.
