---
name: sva
description: Write or change SystemVerilog assertions so they cannot pass vacuously. Use when adding an assert, assume or cover property, when a review asks for an assertion, or when an assertion "always passes". Every assert gets a cover witness and a fault define that makes formal produce a counterexample; every assume names the upstream assert that guarantees it. Ends with `control-eda formal check`.
---

# formal:sva

An assertion is evidence only if three things are shown by a tool: it holds, its
trigger can happen, and it fails when the design is wrong. Write all three together.

## Before you write

1. `control-eda formal detect --json`. Note `selected.formal`. If it is `null`, you can
   still write the properties, but the result of this skill will be `INCONCLUSIVE`
   (tool missing) and you must say so. Never claim an assertion is proven without a run.
2. `control-eda vplan lookup --files <rtl file> --json` to find the vplan row the
   property belongs to. A property with no row is a gap; add the row first.

## Write the property

For each behaviour, add three things in the RTL (or a bound `tb/sva/*.sv` module):

| Piece | Form | Why |
| --- | --- | --- |
| assert | `a_<name>: assert property (@(posedge clk) disable iff (!rst_n) <antecedent> \|-> <consequent>);` | the check |
| cover  | `c_<name>: cover property (@(posedge clk) disable iff (!rst_n) <antecedent>);` | witness: the trigger is reachable |
| fault  | `` `ifdef <BLOCK>_BUG_<NAME> `` in the RTL, a one-line plausible bug | proof the assert can fail |

Then list them on the vplan row: `assertions`, `covers`, `faults`. For an
implication, `control-eda formal check` also adds `<label>__antecedent` as a
cover automatically; an explicit cover on the row is still better because it
documents intent.

Rules for the property itself:

- Label every property. Unlabeled properties cannot be tracked across runs.
- Specify, don't restate. `full == (count == DEPTH)` is a spec when `count` is the
  occupancy; `full == full_q` restating the RTL is not.
- Prefer single-cycle forms (`|->`, `|=>`, `$past`, `$rose`, `$stable`) when they say
  what you mean. They run on every backend, including free Yosys. Sequences (`##`,
  `[*]`, `throughout`, named properties) need a native SVA backend (Yosys with
  Verific, JasperGold, VC Formal, Questa); on free Yosys they are reported
  `unsupported` and the gate is `INCONCLUSIVE`, which is honest but not a pass.
- An `assume` constrains every proof. Add it only with an `assumes:` entry on the
  vplan row naming the upstream assert that guarantees it, e.g.
  `assumes: {m_req_stable: producer.a_req_stable}`. The dv `no_weaken` hook blocks an
  assume that is not listed.

## Run the gate

```bash
control-eda formal check --json
```

Read `verdict`, then `reasons`:

| You see | Meaning | Do |
| --- | --- | --- |
| `proof: cex` | the design violates the property | read `cex_trace` with `control-eda wave-query`; fix the RTL or, if the property is wrong, say why and ask |
| `witness: unreachable` | the assert is vacuous | the trigger never happens, often an over-constraining assume or wrong reset |
| `mutation: missed` | the fault does not break any of the row's asserts | the property is too weak for that bug; strengthen it |
| `mutation: no_fault` | no fault on the row | add a fault define |
| `proof: bounded` | no cex to `depth`, induction did not close | add helper asserts (invariants) or ask a human to raise `[formal].depth` |
| `proof: unsupported` | the backend cannot read it | rewrite single-cycle, or report it needs a native SVA backend |
| `status: tool_missing` | no formal tool here | report INCONCLUSIVE with the install hint; do not substitute reasoning |

Then run `dv:verify` (`control-eda verify`), which folds the formal result into the bundle.

## Never

- Never weaken a property, add `disable iff`, delete a cover, lower `[formal].depth`
  or set `accept_bounded = true` to get green. Those are human decisions.
- Never call a property proven from reading the code.
