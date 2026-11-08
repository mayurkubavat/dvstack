"""Hook tests: feed each hook the JSON Claude Code would, check exit code and message."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "plugins" / "dv" / "hooks"
EXAMPLE = ROOT / "examples" / "fifo_block"


def run_hook(name: str, event: dict, cwd: Path = EXAMPLE) -> tuple[int, str, str]:
    p = subprocess.run([sys.executable, str(HOOKS / name)], input=json.dumps(event), capture_output=True, text=True, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def edit(path: str, old: str, new: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": old, "new_string": new}, "cwd": str(EXAMPLE)}


RTL = str(EXAMPLE / "rtl" / "fifo.sv")


class TestNoWeaken:
    def test_removing_assert_is_blocked(self):
        code, _, err = run_hook("no_weaken.py", edit(RTL, "a_no_overflow: assert property (@(posedge clk) disable iff (!rst_n) count <= (AW+1)'(DEPTH));", ""))
        assert code == 2
        assert "removes an `assert property`" in err

    def test_commenting_out_assert_is_blocked(self):
        code, _, err = run_hook("no_weaken.py", edit(RTL, "a_x: assert property (p);", "// a_x: assert property (p);"))
        assert code == 2

    def test_adding_disable_iff_is_blocked(self):
        code, _, err = run_hook("no_weaken.py", edit(RTL, "a_x: assert property (@(posedge clk) p);", "a_x: assert property (@(posedge clk) disable iff (busy) p);"))
        assert code == 2
        assert "disable iff" in err

    def test_adding_ignore_bins_is_blocked(self):
        code, _, err = run_hook("no_weaken.py", edit(str(EXAMPLE / "tb" / "cov.sv"), "bins b[] = {[0:3]};", "bins b[] = {[0:3]}; ignore_bins z = {3};"))
        assert code == 2

    def test_inline_waiver_is_blocked(self):
        code, _, err = run_hook("no_weaken.py", edit(RTL, "assign x = y;", "assign x = y; // verilator lint_off WIDTH"))
        assert code == 2
        assert "waiver" in err

    def test_dropping_seed_is_blocked(self):
        code, _, err = run_hook("no_weaken.py", edit(str(EXAMPLE / "dv.toml"), "seeds = [1, 42, 9137]", "seeds = [1, 42]"))
        assert code == 2
        assert "9137" in err

    def test_lowering_floor_is_blocked(self):
        code, _, err = run_hook("no_weaken.py", edit(str(EXAMPLE / "dv.toml"), "floor = 80.0", "floor = 50.0"))
        assert code == 2

    def test_adding_assert_is_allowed(self):
        code, _, _ = run_hook("no_weaken.py", edit(RTL, "endmodule", "a_new: assert property (@(posedge clk) !(full && push && !pop) |-> ##1 full);\nendmodule"))
        assert code == 0

    def test_ordinary_rtl_edit_is_allowed(self):
        code, _, _ = run_hook("no_weaken.py", edit(RTL, "wptr <= wptr + 1'b1;", "wptr <= wptr + AW'(1);"))
        assert code == 0

    def test_non_hdl_file_is_ignored(self):
        code, _, _ = run_hook("no_weaken.py", edit("/tmp/notes.md", "assert property", ""))
        assert code == 0

    def test_garbage_input_fails_open(self):
        p = subprocess.run([sys.executable, str(HOOKS / "no_weaken.py")], input="not json", capture_output=True, text=True)
        assert p.returncode == 0


class TestDoorCheck:
    def test_port_change_is_one_way(self, tmp_path):
        ev = edit(RTL, "input  logic             pop,", "input  logic             pop,\n  input  logic             flush,")
        ev["cwd"] = str(EXAMPLE)
        code, out, _ = run_hook("door_check.py", ev)
        assert code == 0
        assert "ONE-WAY" in out
        marker = EXAMPLE / "evidence" / ".one-way-door"
        assert marker.exists()
        marker.unlink()

    def test_body_change_is_two_way(self):
        code, out, _ = run_hook("door_check.py", edit(RTL, "wptr <= wptr + 1'b1;", "wptr <= wptr + AW'(1);"))
        assert code == 0
        assert "ONE-WAY" not in out


class TestLintOnSave:
    def test_casex_is_flagged(self, tmp_path):
        f = tmp_path / "rtl" / "dec.sv"
        f.parent.mkdir()
        f.write_text("module dec(input logic [1:0] a, output logic y);\nalways_comb casex (a) 2'b1?: y = 1; default: y = 0; endcase\nendmodule\n")
        code, out, _ = run_hook("lint_on_save.py", {"tool_name": "Write", "tool_input": {"file_path": str(f)}, "cwd": str(tmp_path)})
        assert code == 0
        assert "casex" in out

    def test_hier_ref_in_sequence_is_flagged(self, tmp_path):
        f = tmp_path / "tb" / "seq.sv"
        f.parent.mkdir()
        f.write_text("class my_seq;\n task body(); tb_top.dut.u_fifo.count = 0; endtask\nendclass\n")
        code, out, _ = run_hook("lint_on_save.py", {"tool_name": "Write", "tool_input": {"file_path": str(f)}, "cwd": str(tmp_path)})
        assert "hierarchical reference" in out

    def test_hier_ref_in_bind_file_is_fine(self, tmp_path):
        f = tmp_path / "tb" / "fifo_bind.sv"
        f.parent.mkdir()
        f.write_text("bind fifo fifo_sva sva(.*);\nassign x = tb_top.dut.count;\n")
        code, out, _ = run_hook("lint_on_save.py", {"tool_name": "Write", "tool_input": {"file_path": str(f)}, "cwd": str(tmp_path)})
        assert "hierarchical reference" not in out


class TestEvidenceGate:
    @pytest.fixture
    def repo(self, tmp_path):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "dv.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "a.sv").write_text("module a; endmodule\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
        return tmp_path

    def test_no_hdl_change_allows_stop(self, repo):
        code, _, _ = run_hook("evidence_gate.py", {"cwd": str(repo)}, cwd=repo)
        assert code == 0

    def test_hdl_change_without_evidence_blocks(self, repo):
        (repo / "rtl" / "a.sv").write_text("module a; logic x; endmodule\n")
        code, _, err = run_hook("evidence_gate.py", {"cwd": str(repo)}, cwd=repo)
        assert code == 2
        assert "dv:verify" in err

    def test_fail_bundle_blocks(self, repo):
        (repo / "rtl" / "a.sv").write_text("module a; logic x; endmodule\n")
        (repo / "evidence").mkdir()
        (repo / "evidence" / "worktree.json").write_text(json.dumps({"verdict": "FAIL", "files": ["rtl/a.sv"]}))
        code, _, err = run_hook("evidence_gate.py", {"cwd": str(repo)}, cwd=repo)
        assert code == 2
        assert "FAIL" in err

    def test_pass_bundle_allows(self, repo):
        (repo / "rtl" / "a.sv").write_text("module a; logic x; endmodule\n")
        (repo / "evidence").mkdir()
        (repo / "evidence" / "worktree.json").write_text(json.dumps({"verdict": "PASS", "files": ["rtl/a.sv"]}))
        code, _, _ = run_hook("evidence_gate.py", {"cwd": str(repo)}, cwd=repo)
        assert code == 0

    def test_stale_bundle_blocks(self, repo):
        (repo / "rtl" / "a.sv").write_text("module a; logic x; endmodule\n")
        (repo / "rtl" / "b.sv").write_text("module b; endmodule\n")
        (repo / "evidence").mkdir()
        (repo / "evidence" / "worktree.json").write_text(json.dumps({"verdict": "PASS", "files": ["rtl/a.sv"]}))
        code, _, err = run_hook("evidence_gate.py", {"cwd": str(repo)}, cwd=repo)
        assert code == 2
        assert "rtl/b.sv" in err

    def test_second_stop_does_not_loop(self, repo):
        (repo / "rtl" / "a.sv").write_text("module a; logic x; endmodule\n")
        code, _, _ = run_hook("evidence_gate.py", {"cwd": str(repo), "stop_hook_active": True}, cwd=repo)
        assert code == 0


class TestHumanRelay:
    def test_pasted_log_gets_a_nudge(self):
        prompt = "UVM_ERROR @ 100ns: x\nUVM_ERROR @ 120ns: y\nUVM_FATAL @ 130ns: z\n$finish"
        code, out, _ = run_hook("human_relay_check.py", {"prompt": prompt})
        assert code == 0 and "wave-query" in out

    def test_normal_prompt_is_silent(self):
        code, out, _ = run_hook("human_relay_check.py", {"prompt": "add a cover for back-to-back pops"})
        assert code == 0 and out.strip() == ""


def test_hooks_json_points_at_existing_scripts():
    data = json.loads((HOOKS / "hooks.json").read_text())
    for event, groups in data["hooks"].items():
        for g in groups:
            for h in g["hooks"]:
                script = h["command"].split("/hooks/")[1].rstrip('"')
                assert (HOOKS / script).exists(), f"{event}: {script} missing"
