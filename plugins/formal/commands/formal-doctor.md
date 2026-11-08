---
description: Show which formal and equivalence tools this machine has, and what the formal gate will use.
---

Run `control-eda formal detect --json` and `control-eda rules test --json`. Report,
in a short table, each formal and equivalence backend with available or missing,
the selected backend, the SMT solvers found, and whether `dv.toml` has `[formal]`
and `[equiv]` sections. For every missing piece, give the install hint the output
names. Then list rules that are not `fires`. Do not change anything.
