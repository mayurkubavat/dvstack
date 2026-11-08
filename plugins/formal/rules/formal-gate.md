---
description: Formal verification rules for SVA, assumptions and refactors. Always on in projects that use the formal plugin.
alwaysApply: true
trigger: always_on
---

# formal gate

These rules hold whether or not hooks are enforcing them.

1. A property is proven only when `control-eda formal check` says so in
   `evidence/formal.json`. Never call an assertion proven, a refactor equivalent,
   or a cover reachable from reading the code.
2. If no formal or equivalence tool is installed, the result is INCONCLUSIVE
   (tool missing). Report the install hint from `control-eda formal detect`; do not
   substitute reasoning or simulation for a proof.
3. Every assert needs a reachable cover witness and a fault define that formal
   shows it catches. Every assume needs a vplan `assumes:` entry naming the upstream
   assert that guarantees it.
4. Never weaken the gate: no deleting covers, no new unlisted assumes, no lowering
   `[formal].depth`, no `accept_bounded = true`, no switching to a weaker backend.
   Those are human decisions; stop and ask.
5. A refactor is done only when `control-eda equiv --base <parent>` reports
   `equivalent`. Keep refactors free of functional changes.
6. A review comment seen twice becomes a rule in `rules.yaml` (formal:review-to-rule),
   and the rule must show `fires` in `control-eda rules test` before it counts.
