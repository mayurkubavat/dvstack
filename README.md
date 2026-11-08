# dvstack

Claude Code plugins for RTL design and verification engineers.

The first plugin, `dv`, is an evidence gate. Its `dv:verify` skill refuses to call a
change done until lint, simulation on fixed and random seeds, coverage, and
anti-vacuity checks on every assertion exist on disk as `evidence/<sha>.json`. A
separate verifier agent reruns that evidence before anything is called merge-ready.
Hooks stop the agent from weakening the gate: no relaxed assertions, no excluded
coverage bins, no waivers, no dropped seeds.

The method is Lauren Tan's trust ladder (pstack), ported from Cursor to Claude
Code, with the formal and simulation layers hardware teams already own.

## Install

```bash
# in Claude Code
/plugin marketplace add mayurkubavat/dvstack
/plugin install dv@dvstack

# the CLI the plugin drives (once per machine)
git clone https://github.com/mayurkubavat/dvstack
pip install -e dvstack/tools/control-eda
```

Verilator (or Icarus) must be on `PATH`. Commercial simulators plug in as adapters.

## Set up a project

Two files at the project root, both agent-readable:

- `dv.toml`: tool, sources, tests, fixed seeds, coverage floor
  ([example](examples/fifo_block/dv.toml)).
- `vplan.yaml`: one row per feature linking RTL, tests, covergroups, assertions,
  their `cover` twins and the fault defines that must make a test fail
  ([example](examples/fifo_block/vplan.yaml)).

Then `/dv-doctor` in Claude Code, or `control-eda doctor` in a shell.

## Try it on the reference block

```bash
cd examples/fifo_block
control-eda verify --claim "[measured] all seeds pass"
```

```
VERDICT: PASS   bundle: evidence/worktree.json

  lint   0 errors, 0 warnings
  sim    8 runs, 0 not ok   tests=['tb_fifo'] faults=['FIFO_BUG_DOUBLE_POP', 'FIFO_BUG_FULL_EARLY']
  cov    94.74% (floor 80.0)
  sva    a_full_empty_excl: cover hit, mutation caught (FIFO_BUG_FULL_EARLY)
  sva    a_no_overflow: cover hit, mutation caught (FIFO_BUG_FULL_EARLY)
```

Break the RTL and the same command says `FAIL`. Remove a `cover` and it says
`INCONCLUSIVE`, because an assertion that never saw its antecedent proves nothing.

## What is in the `dv` plugin

| Piece | Name | Does |
| --- | --- | --- |
| Skill | `dv:verify` | The evidence gate, step by step, ending in the bundle |
| Skill | `dv:vplan` | Map a file or a symptom to tests, covergroups and assertions |
| Skill | `dv:why-fail` | Read-only root cause: rerun the seed, `wave-query` the failure, open the files |
| Skill | `dv:correct` | Turn a repeated review comment into a lint rule, assertion, hook or CI check |
| Skill | `dv:create-verify-skill` | Generate a project-local verify skill for one IP (Launch, Doctor, Drive, Evidence, Cleanup, Helpers) |
| Agent | `dv-verifier` | Clean-context rerun of the bundle: VERIFIED, NOT VERIFIED or INCONCLUSIVE |
| Commands | `/dv-mode`, `/dv-verify`, `/why-fail`, `/correct`, `/dv-doctor` | Playbook router and entry points |
| Hooks | `no-weaken`, `door-check`, `lint-on-save`, `evidence-gate`, `session-doctor`, `human-relay` | The rules the agent cannot skip |

### control-eda

```
control-eda doctor | lint | sim | cov | wave-query | vplan | scope | door | verify | evidence
```

`--json` on everything. `wave-query` turns a VCD or FST into a cycle table around a
time, so an agent never needs a waveform GUI:

```bash
control-eda wave-query evidence/sim/tb_fifo_42.vcd --at 95ps --window 2 --signals 'tb_fifo.dut.*'
```

## Two-way and one-way doors

`door-policy.yaml` names the edits a human must merge: ports, CSR maps, clock and
reset trees, waivers, ECOs. Everything else is a two-way door and may, once the
verifier agent agrees with human review often enough, merge on its own. The hook
marks the session and `control-eda door classify` records it in the bundle.

## Roadmap

- `rtl` plugin: lint-rule promotion from review history, LEC-gated refactors, multi-model RTL review.
- `formal` plugin: SVA authoring with cover and mutation checks, bounded proofs with SymbiYosys.
- `dv-evals` plugin: blinded evals of the skills on historical bugs, across models.

## Develop

```bash
pip install -e "tools/control-eda[dev]"
pytest -q tests
```

MIT licensed.
