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
```

Open-source tools first: Verilator for lint, sim and coverage; Icarus as a sim
fallback; VCD and FST (via `fst2vcd`) for waves. Commercial tools plug in as
adapters under `control_eda/adapters/` and are selected by `dv.toml`.
