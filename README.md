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
/plugin install formal@dvstack     # optional: formal gate, equivalence, review-to-rule

# the CLI the plugin drives (once per machine)
git clone https://github.com/mayurkubavat/dvstack
pip install -e dvstack/tools/control-eda
```

Verilator (or Icarus) must be on `PATH`. Commercial simulators plug in as adapters.
Formal tools are optional and found at run time (`control-eda formal detect`); with
none installed the formal step reports `INCONCLUSIVE (tool missing)` and says what to
install, while the simulation gate keeps working.

Cursor and Google Antigravity are supported too; see
[docs/environments.md](docs/environments.md):

```bash
control-eda install --env cursor        # writes .cursor/ in the current project (all plugins)
control-eda install --env antigravity   # writes .agents/ in the current project
control-eda install --env cursor --plugins dv,formal
```

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
  cov    94.78% (floor 80.0)
  formal PASS (yosys)
  sva    a_full_empty_excl: cover hit, mutation caught (FIFO_BUG_FULL_EARLY), proof proven
  sva    a_full_holds: cover hit, mutation caught (FIFO_BUG_FULL_EARLY), proof proven
  sva    a_full_iff_depth: cover hit, mutation caught (FIFO_BUG_FULL_EARLY), proof proven
  sva    a_no_overflow: cover hit, mutation caught (FIFO_BUG_FULL_EARLY), proof proven
  sva    a_ptr_count: cover hit, mutation caught (FIFO_BUG_DOUBLE_POP), proof proven
```

The `formal` line appears because the example's `dv.toml` has a `[formal]` section
and Yosys is installed. On a machine without a formal tool it becomes a gap that
names what to install, and the verdict rests on simulation alone.

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
| Commands | `/dv-mode`, `/dv-verify`, `/dv-doctor` | Playbook router and entry points; skills are also callable as `/dv:verify`, `/dv:why-fail`, `/dv:correct` |
| Hooks | `no-weaken`, `door-check`, `lint-on-save`, `evidence-gate`, `session-doctor`, `human-relay` | The rules the agent cannot skip; `hooks/adapt.py` runs the same scripts under Cursor and Antigravity |
| Rules | `dv-gate` | Always-on rule file for environments where hooks are weaker |

## What is in the `formal` plugin

Simulation can show an assertion passed. Only formal can show it holds, that its
trigger is reachable, and that it fails when the design is wrong. `control-eda
formal check` asks all four questions of every assertion:

| Check | Pass means | Fail means |
| --- | --- | --- |
| proof | `proven` (induction closed) | `cex`: a counterexample trace in `build/formal/` |
| witness | a cover on its vplan row, or its auto `<label>__antecedent` cover, is reached | `unreachable`: the assertion is vacuous |
| mutation | each fault define on its row gives a counterexample on the row's assertions | `missed`: the assertion cannot see that bug |
| assumes | every `assume` is tied in vplan `assumes:` to the upstream assert that guarantees it | an unchecked assume can hide bugs |

```bash
cd examples/fifo_block
control-eda formal check
```

```
FORMAL: PASS   backend: yosys   depth: 12

  assert a_full_iff_depth  proof=proven  witness=reached  mutation=caught (FIFO_BUG_FULL_EARLY)
  assert a_ptr_count       proof=proven  witness=reached  mutation=caught (FIFO_BUG_DOUBLE_POP)
  ...
  fault  FIFO_BUG_DOUBLE_POP  caught by a_ptr_count
  fault  FIFO_BUG_FULL_EARLY  caught by a_full_iff_depth
```

The simulation gate said `FIFO_BUG_FULL_EARLY` was "caught", but it was the
scoreboard that caught it, not the two original assertions; formal showed neither
could see that bug, which is why the example gained `a_full_iff_depth`.

| Piece | Name | Does |
| --- | --- | --- |
| Skill | `formal:sva` | Write assertions with a cover witness and a fault define, then run the gate |
| Skill | `formal:prove` | Detect the installed tool, run the gate, report it with evidence |
| Skill | `formal:refactor` | Behavior-preserving refactor, done only when `control-eda equiv --base <parent>` is `equivalent` |
| Skill | `formal:review-to-rule` | A repeated review comment becomes a `rules.yaml` lint rule or an SVA assertion, proven to fire |
| Command | `/formal-doctor` | Which formal, equivalence and solver tools this machine has |
| Rules | `formal-gate` | Always-on rule file for Cursor and Antigravity |

The dv plugin's `no-weaken` hook also guards the formal gate: it blocks deleting a
`cover property`, adding an `assume property` that vplan does not tie to an upstream
assert, lowering `[formal].depth`, and setting `accept_bounded = true`.

### Tools are found at run time

Nothing in the plugin assumes a tool. `control-eda formal detect` lists what this
machine has and which backend each command will use:

| Backend | Kind | Needs | Status |
| --- | --- | --- | --- |
| `yosys` | formal, equiv | `yosys`, `yosys-smtbmc`, one of yices / bitwuzla / z3 / boolector | open source, exercised in CI |
| `sby` | formal | SymbiYosys | open source, same log format as `yosys` |
| `eqy` | equiv | Yosys EQY | open source |
| `jasper`, `vcformal`, `questa` | formal | `jg`, `vcf`, `qverify` and a license | templates: check against your flow |
| `conformal`, `formality` | equiv | `lec`, `fm_shell` and a license | templates |
| anything else | either | a command and log regexes in `dv.toml` | `[formal.backends.<name>]` |

Free Yosys reads only single-cycle SVA (`|->`, `|=>`, `$past`, `$rose`, ...);
control-eda rewrites those for it. Sequences (`##`, `[*]`, named properties) need a
native SVA backend; on free Yosys they are reported `unsupported`, so the gate says
`INCONCLUSIVE` instead of passing. With no tool at all, every assertion is
`tool_missing` with the install command. `CONTROL_EDA_HIDE_TOOLS=yosys,sby` shows
what a user without those tools would see.

### control-eda

```
control-eda doctor | lint | sim | cov | wave-query | vplan | scope | door | verify | evidence | install
control-eda formal detect | formal check | equiv --base <ref> | rules list | rules check | rules test
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

- `rtl` plugin: multi-model RTL review for CDC, reset and X-propagation.
- `formal`: confirm the JasperGold, VC Formal, Questa, Conformal and Formality templates on licensed installs.
- `dv-evals` plugin: blinded evals of the skills on historical bugs, across models.

## Develop

```bash
pip install -e "tools/control-eda[dev]"
pytest -q tests
```

MIT licensed.
