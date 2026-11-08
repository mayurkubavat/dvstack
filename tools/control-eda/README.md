# control-eda

One door between agents and EDA tools. Few composable commands, `--json` on every
one, `--dry-run` on anything that writes, and errors that name the command to run
instead.

```bash
pip install -e tools/control-eda
cd examples/fifo_block
control-eda doctor --json
control-eda lint --json
control-eda sim --test tb_fifo --seed 42 --cov --trace --json
control-eda sim --test tb_fifo --seed 42 --define FIFO_BUG_DOUBLE_POP --expect-fail --json
control-eda cov report --json
control-eda wave-query evidence/sim/tb_fifo_42.vcd --at 18450ns --window 8 --signals 'tb_fifo.dut.*' --json
control-eda vplan check --json
control-eda evidence bundle --json > evidence/worktree.json
control-eda evidence check evidence/worktree.json
control-eda formal detect --json          # which formal / equivalence tools this machine has
control-eda formal check --json           # proof, cover witnesses, fault counterexamples, assume audit
control-eda equiv --base HEAD --json      # working tree equivalent to HEAD?
control-eda rules test --json             # rules.yaml: does every rule fire on its bad examples?
control-eda review scan --base main --json  # CDC / reset / X-prop structural scan, no EDA tool needed
control-eda review merge --json           # merge reviews/*.json into agreed / contested / single findings
```

Open-source tools first: Verilator for lint, sim and coverage; Icarus as a sim
fallback; VCD and FST (via `fst2vcd`) for waves; Yosys + yosys-smtbmc (or
SymbiYosys, EQY) for formal and equivalence. Formal backends live in
`control_eda/formal/backends.py` and are found at run time; JasperGold, VC Formal,
Questa, Conformal and Formality are data-only templates, and a project can add its
own backend in `dv.toml` (`[formal.backends.<name>]`). A missing tool is reported as
`tool_missing` with an install hint, never as a pass. Commercial tools plug in as
adapters under `control_eda/adapters/` and are selected by `dv.toml`.
