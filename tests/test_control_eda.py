"""control-eda tests against examples/fifo_block. Needs verilator on PATH for the sim tests."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fifo_block"
HAS_VERILATOR = shutil.which("verilator") is not None


def eda(*args: str, cwd: Path = EXAMPLE, check: bool = False) -> tuple[int, dict | str]:
    p = subprocess.run(["control-eda", *args, "--json"], cwd=cwd, capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, p.stdout + p.stderr


def test_version():
    p = subprocess.run(["control-eda", "--version"], capture_output=True, text=True)
    assert p.returncode == 0 and "control-eda" in p.stdout


def test_doctor_json_shape():
    code, d = eda("doctor")
    assert isinstance(d, dict) and "tools" in d and "ok" in d
    assert d["project"]["name"] == "fifo_block"


def test_no_dv_toml_is_a_clear_error(tmp_path):
    code, d = eda("lint", cwd=tmp_path)
    assert code == 1 and d["ok"] is False and "dv.toml" in d["error"]


def test_vplan_check_passes_on_example():
    code, d = eda("vplan", "check")
    assert code == 0, d
    assert d["features"] == 2 and d["orphan_rtl"] == [] and d["problems"] == []


def test_vplan_lookup_by_file_and_query():
    code, d = eda("vplan", "lookup", "--files", "rtl/fifo.sv")
    assert d["count"] == 2 and d["tests"] == ["tb_fifo"]
    code, d = eda("vplan", "lookup", "--query", "full flag occupancy")
    assert [m["id"] for m in d["matches"]] == ["fifo.flags"]


def test_vplan_check_catches_bad_label(tmp_path):
    shutil.copytree(EXAMPLE, tmp_path / "p", ignore=shutil.ignore_patterns("build", "evidence"))
    vp = tmp_path / "p" / "vplan.yaml"
    vp.write_text(vp.read_text().replace("a_no_overflow", "a_does_not_exist"))
    code, d = eda("vplan", "check", cwd=tmp_path / "p")
    assert code == 1 and any("a_does_not_exist" in p for p in d["problems"])


def test_door_classify_two_way_on_clean_tree():
    code, d = eda("door", "classify")
    assert d["door"] in ("two-way", "one-way")


def test_wave_query_on_fixture_vcd(tmp_path):
    vcd = tmp_path / "t.vcd"
    vcd.write_text(
        "$timescale 1ns $end\n$scope module top $end\n$var wire 1 ! clk $end\n$var wire 4 \" cnt [3:0] $end\n$upscope $end\n$enddefinitions $end\n"
        "#0\n0!\nb0000 \"\n#5\n1!\n#10\n0!\nb0001 \"\n#15\n1!\n#20\n0!\nb0010 \"\n#25\n1!\n#30\n0!\n#35\n1!\n"
    )
    code, d = eda("wave-query", str(vcd), "--at", "15ns", "--window", "1", "--signals", "top.*", cwd=tmp_path)
    assert code == 0, d
    assert d["clock"] == "top.clk"
    rows = d["rows"]
    assert [r["time"] for r in rows] == ["5ns", "15ns", "25ns"]
    assert rows[1]["signals"]["top.cnt"] == "0x1" and "top.cnt" in rows[1]["changed"]


def test_wave_query_no_match_lists_signals(tmp_path):
    vcd = tmp_path / "t.vcd"
    vcd.write_text("$timescale 1ns $end\n$scope module top $end\n$var wire 1 ! clk $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#5\n1!\n")
    code, d = eda("wave-query", str(vcd), "--at", "5ns", "--signals", "nothing.*", cwd=tmp_path)
    assert code == 1 and d["available_sample"] == ["top.clk"]


def test_evidence_check_rejects_bad_bundle(tmp_path):
    b = tmp_path / "e.json"
    b.write_text(json.dumps({"version": 1, "verdict": "PASS", "door": "sideways", "claims": [{"text": "x", "label": "vibes"}]}))
    code, d = eda("evidence", "check", str(b), cwd=tmp_path)
    assert code == 1 and any("door" in p for p in d["problems"]) and any("label" in p for p in d["problems"])


@pytest.mark.skipif(not HAS_VERILATOR, reason="verilator not installed")
class TestWithVerilator:
    def test_lint_clean(self):
        code, d = eda("lint")
        assert code == 0 and d["error_count"] == 0, d

    def test_sim_pass_and_trace(self):
        code, d = eda("sim", "--test", "tb_fifo", "--seed", "42", "--cov", "--trace")
        assert code == 0 and d["status"] == "pass" and d["wave"] and d["cov_file"], d

    def test_fault_injection_must_fail(self):
        code, d = eda("sim", "--test", "tb_fifo", "--seed", "42", "--define", "FIFO_BUG_DOUBLE_POP", "--expect-fail")
        assert code == 0 and d["status"] == "fail" and d["ok"] is True, d
        assert d["first_failure"] and "rdata" in d["first_failure"]

    def test_unknown_test_names_known_ones(self):
        code, d = eda("sim", "--test", "nope", "--seed", "1")
        assert code == 1 and "tb_fifo" in d["error"]

    def test_full_verify_passes(self):
        code, d = eda("verify", "--claim", "[measured] all seeds pass")
        assert code == 0, d
        assert d["verdict"] == "PASS"
        assert d["steps"]["sim"]["failed"] == 0
        assert all(a["cover"] == "hit" and a["mutation"] == "caught" for a in d["sva"]), d["sva"]
        bundle = EXAMPLE / d["bundle"]
        code, chk = eda("evidence", "check", str(bundle))
        assert code == 0 and chk["verdict"] == "PASS"

    def test_wave_query_on_real_dump(self):
        eda("sim", "--test", "tb_fifo", "--seed", "42", "--trace")
        code, d = eda("wave-query", "evidence/sim/tb_fifo_42.vcd", "--at", "95ps", "--window", "2", "--signals", "tb_fifo.dut.full,tb_fifo.dut.count")
        assert code == 0 and len(d["rows"]) == 5 and set(d["signals"]) == {"TOP.tb_fifo.dut.full", "TOP.tb_fifo.dut.count"}

    def test_guessed_claim_is_inconclusive(self):
        code, d = eda("verify", "--keep", "--claim", "this probably works")
        assert d["verdict"] == "INCONCLUSIVE"

    def test_verify_catches_a_real_bug(self, tmp_path):
        """Break the RTL for real (no define) and the gate must say FAIL."""
        shutil.copytree(EXAMPLE, tmp_path / "p", ignore=shutil.ignore_patterns("build", "evidence"))
        rtl = tmp_path / "p" / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace("assign empty = (count == '0);", "assign empty = (count <= (AW+1)'(1));"))
        code, d = eda("verify", "--files", "rtl/fifo.sv", cwd=tmp_path / "p")
        assert code == 1 and d["verdict"] == "FAIL", d
