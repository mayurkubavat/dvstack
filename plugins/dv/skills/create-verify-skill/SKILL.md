---
name: create-verify-skill
description: Generate a project-local verification skill for one IP block, following the Launch, Doctor, Drive, Evidence, Cleanup, Helpers recipe. Use when onboarding a new block or testbench to the dv plugin, or when asked to make verification of a specific block repeatable. The skill must pass 10 of 10 runs before it counts as real.
---

# dv:create-verify-skill

Produce `.claude/skills/verify-<block>/SKILL.md` in the target project, plus its
helper scripts, so that `dv:verify` has an exact, executable path for this block.

## The six sections

Every generated skill has these headings, in this order:

1. **Launch**: the exact `control-eda sim` compile command for this block, with the
   file list or filelist and defines. One command, copy-pasteable.
2. **Doctor**: read-only checks that the block can be verified here: tools,
   licenses, `vplan.yaml` rows for the block, the fault defines exist in the RTL.
3. **Drive**: the tests to run and the seeds, taken from `vplan.yaml` and `dv.toml`.
   Stable handles only: test names and plusargs, never line numbers in logs.
4. **Evidence**: what proves the block works: the log pattern (`TEST PASSED`), the
   assertion labels that must have fired their covers, the coverage floor.
5. **Cleanup**: what to remove after a run. Never `evidence/`.
6. **Helpers**: scripts under `.claude/skills/verify-<block>/scripts/` that do each
   step, so a reviewer reruns them without reading the skill.

## Procedure

1. `control-eda vplan lookup --block <name> --json` to gather rows.
2. Write the skill from the template below. Fill every placeholder from tool output,
   not from memory.
3. Run the generated skill end to end 10 times with different random seeds:
   `control-eda sim --test <t> --seed random --json` x10. Record results in
   `.claude/skills/verify-<block>/runs.log`.
4. Inject each fault define from the vplan row once and confirm the skill reports
   FAIL. A skill that cannot fail is a draft.
5. Report: 10/10 passes, N/N faults caught, or what fell short.

## Rules

- Never weaken a journey to make it pass. If a test is flaky, that is a finding,
  not a seed to drop.
- Mock nothing. The skill drives the real simulator on the real RTL.
- A skill that was never executed is a draft; say so in its header until the
  10-run log exists.

## Template

```markdown
---
name: verify-<block>
description: Verify the <block> block end to end with real simulation. Use before any change to <rtl files> is called done.
---
# Launch
control-eda sim --block <block> --compile-only --json
# Doctor
control-eda doctor --json && control-eda vplan lookup --block <block> --json
# Drive
<test> with seeds <fixed> plus 3 random
# Evidence
"TEST PASSED" in every log; covers <c_*> hit; coverage >= <floor>%
# Cleanup
rm -rf obj_dir  (keep evidence/)
# Helpers
scripts/run_all.sh, scripts/inject_faults.sh
```
