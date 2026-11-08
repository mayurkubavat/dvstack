# dvstack

Claude Code plugin marketplace for RTL design and verification engineers. One
repo, several plugins, one shared CLI.

## Layout

- `.claude-plugin/marketplace.json`: the marketplace; lists every plugin under `plugins/`.
- `plugins/<name>/`: one plugin each. `skills/<skill>/SKILL.md`, `agents/*.md`,
  `commands/*.md`, `hooks/hooks.json` + `hooks/*.py`. Skills are invoked as `<plugin>:<skill>`.
- `tools/control-eda/`: Python CLI every skill and hook calls. `pip install -e tools/control-eda`.
- `schemas/`: JSON schemas for `evidence`, `vplan` and `door-policy` files.
- `examples/fifo_block/`: the reference project every test runs against. Has
  injectable faults (`FIFO_BUG_*`) so the gate can prove it is able to fail.
- `tests/`: pytest for hooks and control-eda. Run `pytest -q tests`.

## Rules for working here

- Hooks fail open and never edit files. Blocking is exit 2 with the reason on stderr.
- `control-eda` takes `--json` on every subcommand and never prompts.
- Add a skill by adding a directory under `plugins/<plugin>/skills/`; no registry to update.
- A new plugin needs `.claude-plugin/plugin.json` and an entry in `marketplace.json`.
- Anything the gate checks (seeds, floors, faults) lives in `dv.toml` or `vplan.yaml`
  of the target project, never in plugin code.
- Before a push: `pytest -q tests` and `cd examples/fifo_block && control-eda verify`.

## Author

Commits are authored by Mayur Kubavat only. Do not add co-author trailers.
