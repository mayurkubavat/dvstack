"""control-eda triage: signatures, the known store, the main recheck and shrinking.

The signature and store tests need no simulator (they read logs, with Verilator hidden).
The rerun tests need Verilator and use real bugs in a throwaway git repo.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "control-eda"))
from control_eda import triage  # noqa: E402

EXAMPLE = ROOT / "examples" / "fifo_block"
HAS_VERILATOR = shutil.which("verilator") is not None
needs_verilator = pytest.mark.skipif(not HAS_VERILATOR, reason="verilator not installed")


def eda(*args: str, cwd: Path, env: dict | None = None) -> tuple[int, dict | str]:
    p = subprocess.run(["control-eda", *args, "--json"], cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})})
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, p.stdout + p.stderr


def project(tmp_path: Path) -> Path:
    p = tmp_path / "p"
    shutil.copytree(EXAMPLE, p, ignore=shutil.ignore_patterns("build", "evidence", "triage"))
    return p


NO_SIM = {"CONTROL_EDA_HIDE_TOOLS": "verilator,iverilog"}


class TestSignature:
    def test_same_check_different_seed_is_one_signature(self):
        a = triage.signature({"first_failure": "[65] %Error: fifo.sv:59: Assertion failed in TOP.tb_fifo.dut.a_ptr_count: 'assert' failed."})
        b = triage.signature({"first_failure": "[1205] %Error: /home/ci/run7/rtl/fifo.sv:61: Assertion failed in TOP.tb_fifo.dut.a_ptr_count: 'assert' failed."})
        assert a["id"] == b["id"] and a["kind"] == "assertion"
        assert "a_ptr_count" in a["text"] and "65" not in a["text"]

    def test_values_and_times_do_not_split_buckets(self):
        a = triage.signature({"first_failure": "ERROR @18450: rdata=b1 expected=fe"})
        b = triage.signature({"first_failure": "ERROR @ 925ns: rdata=3c expected=07"})
        assert a["id"] == b["id"] and a["text"] == "ERROR @<t>: rdata=<v> expected=<v>"

    def test_different_checks_are_different_signatures(self):
        a = triage.signature({"first_failure": "ERROR @100: full=1 model_size=3"})
        b = triage.signature({"first_failure": "ERROR @100: empty=1 model_size=3"})
        assert a["id"] != b["id"]

    def test_uvm_style_line(self):
        a = triage.signature({"first_failure": "UVM_ERROR /proj/tb/sb.sv(212) @ 1000: uvm_test_top.env.sb [SB] mismatch exp=8'h3c act=8'h00"})
        b = triage.signature({"first_failure": "UVM_ERROR /other/tb/sb.sv(230) @ 77000: uvm_test_top.env.sb [SB] mismatch exp=8'hff act=8'h01"})
        assert a["id"] == b["id"] and "sb.sv(<l>)" in a["text"]

    def test_project_rule_runs_first(self):
        rules = [{"pattern": r"txn#\d+", "replace": "txn#"}]
        a = triage.signature({"first_failure": "ERROR: txn#12 dropped"}, rules)
        b = triage.signature({"first_failure": "ERROR: txn#907 dropped"}, rules)
        assert a["id"] == b["id"]

    def test_no_failure_line_uses_exit_code(self):
        assert triage.signature({"first_failure": None, "exit_code": 124})["kind"] == "timeout"
        assert triage.signature({"first_failure": None, "exit_code": -6})["text"] == "no pass line, exit code -6"


def write_logs(p: Path) -> None:
    logs = p / "nightly"
    logs.mkdir()
    (logs / "tb_fifo_101.log").write_text("ERROR @18450: rdata=b1 expected=fe\nTEST FAILED seed=101 errors=1\n")
    (logs / "tb_fifo_202.log").write_text("ERROR @925: rdata=3c expected=07\nTEST FAILED seed=202 errors=4\n")
    (logs / "tb_fifo_303.log").write_text("ERROR @100: full=1 model_size=3\nTEST FAILED seed=303 errors=1\n")
    (logs / "tb_fifo_404.log").write_text("TEST PASSED seed=404 pushes=10 pops=9\n")


class TestWithoutSimulator:
    def test_logs_bucket_and_degrade(self, tmp_path):
        p = project(tmp_path)
        write_logs(p)
        code, r = eda("triage", "run", "--logs", "nightly/*.log", "--no-main", cwd=p, env=NO_SIM)
        assert code == 1 and r["verdict"] == "NEEDS_HUMAN", r
        assert r["failures"] == 3 and r["signatures"] == 2 and r["new"] == 2
        big = r["buckets"][0]
        assert big["count"] == 2 and big["seeds"] == [101, 202]
        assert big["reproduce"]["state"] == "tool_missing" and "install" in big["reproduce"]
        assert big["shrink"]["state"] == "tool_missing"
        assert big["action"] == "draft_pr" and big["pr"]["branch"] == f"triage/{big['signature']}"
        assert big["representative"]["seed"] == 202  # earliest failure time
        assert "control-eda sim --test tb_fifo --seed 202" in big["pr"]["body"]
        assert (p / "triage" / "report.md").read_text().count("## triage:") == 2

    def test_record_makes_it_known_then_regressed(self, tmp_path):
        p = project(tmp_path)
        write_logs(p)
        code, r = eda("triage", "run", "--logs", "nightly/*.log", "--no-main", cwd=p, env=NO_SIM)
        sigs = [b["signature"] for b in r["buckets"]]
        for s in sigs:
            code, rec = eda("triage", "record", "--sig", s, "--status", "open", "--link", "https://example.com/pr/1", cwd=p)
            assert code == 0 and rec["entry"]["status"] == "open"
        code, r = eda("triage", "run", "--logs", "nightly/*.log", "--no-main", cwd=p, env=NO_SIM)
        assert code == 0 and r["verdict"] == "CLEAN" and r["known"] == 2, r
        assert all(b["action"] == "none" and b["reproduce"]["state"] == "skipped" for b in r["buckets"])
        eda("triage", "record", "--sig", sigs[0], "--status", "fixed", cwd=p)
        code, r = eda("triage", "run", "--logs", "nightly/*.log", "--no-main", cwd=p, env=NO_SIM)
        assert code == 1 and r["regressed"] == 1 and r["buckets"][0]["status"] == "regressed" and r["buckets"][0]["action"] == "human"
        code, lst = eda("triage", "list", cwd=p)
        assert set(lst["signatures"]) == set(sigs)

    def test_record_unknown_signature_needs_text(self, tmp_path):
        p = project(tmp_path)
        code, r = eda("triage", "record", "--sig", "deadbeef00", "--status", "open", cwd=p)
        assert code == 1 and "--text" in r["error"]

    def test_no_failures_is_clean(self, tmp_path):
        p = project(tmp_path)
        code, r = eda("triage", "run", "--no-main", cwd=p, env=NO_SIM)
        assert code == 0 and r["verdict"] == "CLEAN" and r["failures"] == 0

    def test_regress_without_simulator_is_inconclusive(self, tmp_path):
        p = project(tmp_path)
        code, r = eda("triage", "run", "--regress", "2", "--no-main", cwd=p, env=NO_SIM)
        assert code == 1 and r["verdict"] == "INCONCLUSIVE" and "verilator" in r["inconclusive"][0]

    def test_main_check_outside_git_is_not_available(self, tmp_path):
        p = project(tmp_path)
        write_logs(p)
        code, r = eda("triage", "run", "--logs", "nightly/*.log", "--main", "main", cwd=p, env={**NO_SIM, "GIT_CEILING_DIRECTORIES": str(tmp_path)})
        assert all(b["main"]["state"] == "not_available" for b in r["buckets"]), r["buckets"][0]["main"]


def git(p: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=p, capture_output=True, text=True, check=True).stdout


@needs_verilator
class TestWithVerilator:
    def test_plusarg_override(self, tmp_path):
        p = project(tmp_path)
        code, r = eda("sim", "--test", "tb_fifo", "--seed", "3", "--plusarg", "+cycles=50", cwd=p)
        assert code == 0 and r["plusargs"] == ["+cycles=50"], r
        assert "cycles50" in r["log"]

    def test_fault_regress_shrinks_and_repro_still_fails(self, tmp_path):
        p = project(tmp_path)
        code, r = eda("triage", "run", "--regress", "0", "--define", "FIFO_BUG_DOUBLE_POP", "--no-main", cwd=p)
        assert code == 1 and r["regress"]["runs"] == 3 and r["signatures"] == 1, r
        b = r["buckets"][0]
        assert b["count"] == 3 and b["reproduce"]["state"] == "reproduced"
        sh = b["shrink"]
        assert sh["state"] == "shrunk" and sh["from"] == 2000 and sh["to"] < 100, sh
        code, again = eda("sim", "--test", "tb_fifo", "--seed", str(b["representative"]["seed"]), "--define", "FIFO_BUG_DOUBLE_POP",
                          "--plusarg", f"+cycles={sh['to']}", cwd=p)
        assert again["status"] == "fail" and triage.signature(again)["id"] == b["signature"]

    def test_already_fixed_on_main_is_not_shown(self, tmp_path):
        """A nightly commit with a real bug; main has moved on without it. Nobody needs to look."""
        p = project(tmp_path)
        git(p, "init", "-q", "-b", "main")
        git(p, "add", "-A")
        git(p, "commit", "-qm", "good")
        git(p, "checkout", "-qb", "nightly")
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace("assign empty = (count == '0);", "assign empty = (count <= (AW+1)'(1));"))
        git(p, "commit", "-qam", "bad")
        code, r = eda("triage", "run", "--regress", "0", "--main", "main", "--no-shrink", cwd=p)
        assert r["failures"] >= 1, r
        assert all(b["main"]["state"] == "fixed" and b["action"] == "none" for b in r["buckets"]), [b["main"] for b in r["buckets"]]
        assert code == 0 and r["verdict"] == "CLEAN" and r["fixed_on_main"] == r["signatures"]
        assert len(git(p, "worktree", "list").strip().splitlines()) == 1  # the temporary worktree is gone

    def test_still_failing_on_main_gets_a_draft_pr(self, tmp_path):
        p = project(tmp_path)
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace("assign empty = (count == '0);", "assign empty = (count <= (AW+1)'(1));"))
        git(p, "init", "-q", "-b", "main")
        git(p, "add", "-A")
        git(p, "commit", "-qm", "bad on main")
        git(p, "checkout", "-qb", "nightly")
        (p / "README").write_text("x\n")
        git(p, "add", "-A")
        git(p, "commit", "-qm", "unrelated")
        code, r = eda("triage", "run", "--regress", "0", "--main", "main", cwd=p)
        assert code == 1 and r["new"] >= 1
        b = r["buckets"][0]
        assert b["main"]["state"] == "same_commit" or b["main"]["state"] == "still_failing", b["main"]
        assert b["action"] == "draft_pr" and b["shrink"]["state"] in ("shrunk", "minimal")
