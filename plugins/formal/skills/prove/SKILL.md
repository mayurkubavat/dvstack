---
name: prove
description: Run the formal anti-vacuity gate on a block and report it with evidence. Use when asked to prove, formally check, or "run formal" on RTL, when a PR adds or changes SVA, or before claiming an assertion holds. Finds the formal tool installed on this machine (SymbiYosys, Yosys, JasperGold, VC Formal, Questa, or a project-defined backend), runs proof, cover witnesses and fault-injected counterexamples, and writes evidence/formal.json.
---

# formal:prove

You run the tools and report what they said. If a tool is missing, the answer is
`INCONCLUSIVE (tool missing)`, never a pass and never a guess.

## 1. What is installed here

```bash
control-eda formal detect --json
```

This machine is not the one the plugin was written on. Read the output:

- `selected.formal`: the backend `formal check` will use, or `null`.
- `formal[]`: every backend with `available`, `missing` and `install`. Commercial
  backends (`jasper`, `vcformal`, `questa`) are templates; `tested: false` means
  their log parsing has not been checked against your tool version.
- `configured.formal`: false means `dv.toml` has no `[formal]` section. Add one
  (top, sources, clock, reset, depth); see `examples/fifo_block/dv.toml` in dvstack.

If nothing is available, stop. Report the `install` hint (open source:
`apt install yosys z3`, `brew install yosys z3`, or the OSS CAD Suite) and say the
formal result is INCONCLUSIVE. Simulation evidence from `dv:verify` still stands.

## 2. Run

```bash
control-eda formal check --json        # prove + cover + mutate + assume audit
control-eda formal check --only prove,cover --json   # quick loop while editing
```

The gate's settings (depth, backend, accept_bounded, required) come from `dv.toml`.
`--depth` exists for exploration; a gate result uses the dv.toml depth.

## 3. Report

Quote the verdict and the evidence path, then one line per assertion from `asserts[]`:
`name: proof=<state> witness=<state> mutation=<state> (fault)`. Label claims:
everything copied from the JSON is `[measured]`; your diagnosis is `[inferred]`.

| verdict | means |
| --- | --- |
| PASS | every assert proven, its witness reached, a fault on its row caught, every assume tied to an upstream assert |
| FAIL | a counterexample, an unreachable witness (vacuous assert), or a fault no assert caught |
| INCONCLUSIVE | tool missing, a property the backend cannot read, a bounded-only proof, or a missing witness, fault or upstream assert |

For a FAIL, read `cex_trace` (a VCD) with `control-eda wave-query <trace> --at <time> --signals ...`
and open the RTL before naming a cause. Use `dv:why-fail` for the method.

## 4. Pluggable backends

A team with an in-house flow adds it in `dv.toml` without changing the plugin:

```toml
[formal.backends.inhouse]
exe = "scripts/run_formal.sh"
cmd = ["{{exe}}", "{{script}}"]
script = "top={{top}} depth={{depth}} files={{files}}"
patterns.assert_proven = 'PROVEN (?P<name>\w+)'
patterns.assert_cex = 'FAILED (?P<name>\w+)'
```

Overrides for the commercial templates go under `[formal.backends.jasper]` (and so on).
Anything a backend's log does not report is `unknown`, so a wrong regex fails safe.

To see what another user without a tool would get, hide it:
`CONTROL_EDA_HIDE_TOOLS=yosys,sby control-eda formal check`.
