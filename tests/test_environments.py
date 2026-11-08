"""Cursor and Antigravity adapters and the installer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "dv" / "hooks"
EXAMPLE = ROOT / "examples" / "fifo_block"
RTL = str(EXAMPLE / "rtl" / "fifo.sv")


def adapt(env: str, hook: str, payload: dict) -> dict:
    p = subprocess.run([sys.executable, str(HOOKS / "adapt.py"), "--env", env, hook], input=json.dumps(payload), capture_output=True, text=True, cwd=EXAMPLE)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


class TestCursor:
    def test_pre_tool_use_denies_assert_removal(self):
        r = adapt("cursor", "no_weaken", {"hook_event_name": "preToolUse", "tool_name": "StrReplace", "cwd": str(EXAMPLE),
                                          "tool_input": {"path": RTL, "old_string": "a_x: assert property (p);", "new_string": ""}})
        assert r["permission"] == "deny" and "assert" in r["user_message"]

    def test_pre_tool_use_allows_normal_edit(self):
        r = adapt("cursor", "no_weaken", {"hook_event_name": "preToolUse", "tool_name": "StrReplace", "cwd": str(EXAMPLE),
                                          "tool_input": {"path": RTL, "old_string": "wptr <= wptr + 1'b1;", "new_string": "wptr <= wptr + AW'(1);"}})
        assert r == {"permission": "allow"}

    def test_after_file_edit_runs_lint(self, tmp_path):
        f = tmp_path / "rtl" / "x.sv"
        f.parent.mkdir()
        f.write_text("module x(input logic [1:0] a, output logic y);\nalways_comb casex (a) 2'b1?: y = 1; default: y = 0; endcase\nendmodule\n")
        r = adapt("cursor", "lint_on_save", {"hook_event_name": "afterFileEdit", "file_path": str(f), "edits": [], "workspace_roots": [str(tmp_path)]})
        assert "casex" in r.get("additional_context", "")

    def test_stop_blocks_without_evidence(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "dv.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "a.sv").write_text("module a; endmodule\n")
        r = adapt("cursor", "evidence_gate", {"hook_event_name": "stop", "status": "completed", "loop_count": 0, "workspace_roots": [str(tmp_path)]})
        assert "dv:verify" in r.get("followup_message", "")

    def test_prompt_hook_continues(self):
        r = adapt("cursor", "human_relay_check", {"hook_event_name": "beforeSubmitPrompt", "prompt": "hello"})
        assert r == {"continue": True}


class TestAntigravity:
    def test_replace_file_content_denied(self):
        r = adapt("antigravity", "no_weaken", {"workspacePaths": [str(EXAMPLE)], "stepIdx": 3,
                                               "toolCall": {"name": "replace_file_content", "args": {"TargetFile": RTL, "TargetContent": "a_x: assert property (@(posedge clk) p);", "ReplacementContent": "a_x: assert property (@(posedge clk) disable iff (busy) p);"}}})
        assert r["decision"] == "deny" and "disable iff" in r["reason"]

    def test_multi_replace_waiver_denied(self):
        r = adapt("antigravity", "no_weaken", {"workspacePaths": [str(EXAMPLE)], "stepIdx": 3,
                                               "toolCall": {"name": "multi_replace_file_content", "args": {"TargetFile": RTL, "ReplacementChunks": [{"TargetContent": "assign x = y;", "ReplacementContent": "assign x = y; // verilator lint_off WIDTH"}]}}})
        assert r["decision"] == "deny"

    def test_unknown_tool_allows(self):
        r = adapt("antigravity", "no_weaken", {"workspacePaths": [str(EXAMPLE)], "stepIdx": 1, "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}})
        assert r == {"decision": "allow"}

    def test_stop_blocks_without_evidence(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "dv.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "a.sv").write_text("module a; endmodule\n")
        r = adapt("antigravity", "evidence_gate", {"workspacePaths": [str(tmp_path)], "executionNum": 1, "terminationReason": "done", "fullyIdle": True})
        assert r["decision"] == "continue" and "dv:verify" in r["reason"]

    def test_session_start_injects_context(self):
        r = adapt("antigravity", "session_doctor", {"workspacePaths": [str(EXAMPLE)], "conversationId": "c"})
        assert r["injectSteps"][0]["systemMessage"].startswith("[dv plugin]")


class TestInstall:
    def run(self, env: str, project: Path, *extra: str) -> dict:
        p = subprocess.run(["control-eda", "install", "--env", env, "--project", str(project), "--json", *extra], capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
        return json.loads(p.stdout)

    def test_cursor_layout(self, tmp_path):
        self.run("cursor", tmp_path)
        assert (tmp_path / ".cursor" / "skills" / "verify" / "SKILL.md").exists()
        assert (tmp_path / ".cursor" / "agents" / "dv-verifier.md").exists()
        assert (tmp_path / ".cursor" / "rules" / "dv-gate.mdc").exists()
        hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
        assert hooks["version"] == 1 and "preToolUse" in hooks["hooks"]
        assert all(".cursor/hooks/dv/adapt.py" in e["command"] for es in hooks["hooks"].values() for e in es)
        assert (tmp_path / ".cursor" / "hooks" / "dv" / "no_weaken.py").exists()

    def test_cursor_merges_existing_hooks(self, tmp_path):
        (tmp_path / ".cursor").mkdir()
        (tmp_path / ".cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"afterFileEdit": [{"command": "./mine.sh"}]}}))
        self.run("cursor", tmp_path)
        hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())["hooks"]
        assert "stop" in hooks and hooks["afterFileEdit"][0]["command"].endswith("lint_on_save") or any("mine.sh" in e["command"] for e in hooks["afterFileEdit"])

    def test_antigravity_layout(self, tmp_path):
        r = self.run("antigravity", tmp_path)
        assert (tmp_path / ".agents" / "skills" / "verify" / "SKILL.md").exists()
        assert (tmp_path / ".agents" / "skills" / "dv-verifier" / "SKILL.md").exists()
        assert (tmp_path / ".agents" / "workflows" / "dv-mode.md").exists()
        rule = (tmp_path / ".agents" / "rules" / "dv-gate.md").read_text()
        assert "trigger: always_on" in rule
        hooks = json.loads((tmp_path / ".agents" / "hooks.json").read_text())
        assert "PreToolUse" in hooks["dv-gate"] and "Stop" in hooks["dv-gate"]

    def test_dry_run_writes_nothing(self, tmp_path):
        r = self.run("antigravity", tmp_path, "--dry-run")
        assert r["dry_run"] and not (tmp_path / ".agents").exists() and len(r["actions"]) > 5


def test_cursor_manifests_are_valid_json():
    for p in (ROOT / ".cursor-plugin" / "marketplace.json", ROOT / "plugins" / "dv" / ".cursor-plugin" / "plugin.json", HOOKS / "cursor-hooks.json", HOOKS / "antigravity-hooks.json"):
        json.loads(p.read_text())
    m = json.loads((ROOT / ".cursor-plugin" / "marketplace.json").read_text())
    for entry in m["plugins"]:
        assert (ROOT / entry["source"] / ".cursor-plugin" / "plugin.json").exists()
