---
name: why-fail
description: Read-only investigation of a failing simulation, assertion or regression test. Use when a test fails, when asked why a seed fails, or before any bug fix. Reruns the seed, pulls a wave-query cycle table around the failure, and names a root cause only after opening the files involved. Never edits RTL or testbench.
---

# dv:why-fail

Find the root cause. Change nothing. The output is a triage note another engineer or
agent can act on without re-deriving anything.

## Rules

- Open the files. Naming a root cause from a log message alone is the failure mode
  this skill exists to prevent. Quote the RTL or TB lines you blame.
- Reproduce first: `control-eda sim --test <t> --seed <s> --trace --json`. If it
  passes, say "not reproduced" and stop; do not guess.
- Use `wave-query` instead of asking a human for a waveform screenshot.
- Label every claim `measured`, `inferred` or `guessed`.

## Procedure

1. Reproduce with the exact test and seed. Record the first error line and its time.
2. Pull the cycle table around it:
   ```bash
   control-eda wave-query evidence/sim/<test>_<seed>.fst --at <time> --window 16 \
     --signals '<tb>.<dut>.*' --json
   ```
   Read it. Note which signal changes first in the window.
3. `control-eda vplan lookup --files <rtl of that signal>` to find the assertion or
   coverpoint that should have caught it. If none, note the vplan gap.
4. Open the RTL around the driver of the first wrong signal. Trace one level back.
5. Check if `main` already fixed it: `git log --oneline main -- <file>` for commits
   after the failing commit.

## Triage note

```
Test tb_fifo seed 9137 fails at 18450 ns (reproduced: yes)
First error: rdata=b1 expected=fe
First wrong signal: dut.rptr advances by 2 at 18440 ns          [measured]
Driver: rtl/fifo.sv:38  `rptr <= rptr + 2'd2;` under FIFO_BUG_DOUBLE_POP  [measured]
Root cause: read pointer increments twice per pop               [inferred]
Should have caught it: a_rptr_step (missing; vplan gap)         [inferred]
Already fixed on main: no                                       [measured]
Suggested fix owner: dv:verify after a one-line RTL change; door two-way
```
