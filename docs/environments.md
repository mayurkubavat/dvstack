# Every plugin, three agent environments

The logic lives in two places that every environment can run: `control-eda`
(a CLI with JSON output) and the hook scripts in `plugins/dv/hooks/`. Skills,
agents and commands are Markdown with the same frontmatter everywhere. Each
environment gets a thin adapter, nothing more.

| | Claude Code | Cursor | Google Antigravity |
| --- | --- | --- | --- |
| Install | `/plugin marketplace add mayurkubavat/dvstack` then `/plugin install dv@dvstack` (and `formal@dvstack`, `rtl@dvstack`) | `control-eda install --env cursor` (writes `.cursor/`, all plugins; `--plugins dv,formal,rtl` to choose) or the marketplace once listed | `control-eda install --env antigravity` (writes `.agents/`, same `--plugins`) |
| Manifest | `.claude-plugin/plugin.json` | `.cursor-plugin/plugin.json` | none (folder convention) |
| Skills | `skills/<name>/SKILL.md` | same | `.agents/skills/<name>/SKILL.md`, same frontmatter |
| Subagents | `agents/*.md` | `agents/*.md` | installed as skills (no subagent concept) |
| Commands | `commands/*.md` | `commands/*.md` | installed as `.agents/workflows/*.md` |
| Rules | not needed (hooks) | `rules/dv-gate.mdc`, `rules/formal-gate.mdc`, `rules/rtl-review.mdc`, `alwaysApply: true` | `.agents/rules/*.md`, `trigger: always_on` |
| Hooks file | `hooks/hooks.json` | `hooks/cursor-hooks.json` (named in the manifest) | `.agents/hooks.json` |
| Hook protocol | stdin JSON, exit 2 blocks, stderr is the reason | stdin JSON, stdout `{"permission":"deny"}` | stdin JSON, stdout `{"decision":"deny"}` |
| Adapter | none | `hooks/adapt.py --env cursor <hook>` | `hooks/adapt.py --env antigravity <hook>` |

Hooks ship only in the `dv` plugin, and they guard the formal gate too (covers,
assumes, `[formal]` settings), so `install` always includes `dv`. The `formal`
plugin is skills, a command and a rule file over `control-eda`, which behaves the
same in every environment: it finds the formal tools on the machine at run time
and reports `tool_missing` with an install hint when there are none.

The `rtl` plugin's reviewers are agents with a `model:` line (opus, sonnet,
haiku) so they run on different models. In Cursor, set those lines to two models
your plan allows. Antigravity has no subagents, so the reviewers install as skills:
run the reviewer skill twice in fresh conversations, and `control-eda review merge`
will mark the result `single_model` rather than present it as independent
agreement.

## Event mapping

| dv hook | Claude Code | Cursor | Antigravity |
| --- | --- | --- | --- |
| `session_doctor` | SessionStart | sessionStart | SessionStart (`injectSteps`) |
| `no_weaken`, `door_check` | PreToolUse Edit/Write | preToolUse Write/Edit | PreToolUse (any tool; adapter maps `write_to_file`, `replace_file_content`, `multi_replace_file_content`) |
| `lint_on_save` | PostToolUse | afterFileEdit (`additional_context`) | PostToolUse (context to stderr) |
| `human_relay_check` | UserPromptSubmit | beforeSubmitPrompt | not available (rule covers it) |
| `evidence_gate` | Stop, SubagentStop | stop (`followup_message`) | Stop (`decision: continue`) |

## Confirming the mappings with one real session

The tool names Cursor and Antigravity send to hooks are mapped from their docs.
One agent session per tool confirms them:

```bash
control-eda install --env antigravity --probe     # or --env cursor
# open the project in the tool and ask its agent to:
#   1. make a harmless RTL edit (e.g. rename a local signal)
#   2. remove an assert property   (should be blocked)
#   3. finish without running control-eda verify   (should be refused)
control-eda probe report          # tools seen, unmapped edit tools, what was blocked
control-eda probe off
```

If `edit_tools_not_mapped` is non-empty, send the report; `hooks/adapt.py` learns
the new names and the tests get a case for them.

## What is weaker outside Claude Code

- Antigravity's `PostToolUse` has no documented way to inject context, so lint
  results after a save go to the hook's stderr only. The agent still gets lint
  through `control-eda verify` and the `evidence_gate` hook.
- Cursor's and Antigravity's edit tool names and argument keys are mapped from
  their docs and from Windsurf lineage; if a tool name is not recognised the hook
  sees no file and allows the edit. `dv-gate` rules and CI are the backstop.
- Loop protection: Claude Code passes `stop_hook_active`; the adapter uses
  Cursor's `loop_count` and Antigravity's `executionNum` for the same purpose.

Sources: [Cursor plugin reference](https://cursor.com/docs/reference/plugins.md),
[Cursor hooks](https://cursor.com/docs/agent/hooks),
[Antigravity rules](https://antigravity.google/docs/rules),
[Antigravity skills](https://antigravity.google/docs/skills),
[Antigravity hooks](https://antigravity.google/docs/hooks).
