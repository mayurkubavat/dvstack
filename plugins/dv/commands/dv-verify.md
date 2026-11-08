---
description: Run the evidence gate on the current diff and print the verdict and bundle summary.
argument-hint: [--base <ref>]
---

Run the `dv:verify` skill on the current working tree against base `$ARGUMENTS`
(default: the merge base with `main`). Print the VERDICT line first, then the
summary table, then gaps. Do not modify any RTL or testbench file while doing so.
