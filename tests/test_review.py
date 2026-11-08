"""rtl plugin: the structural review scan (no EDA tool needed) and the multi-reviewer merge."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fifo_block"
sys.path.insert(0, str(ROOT / "tools" / "control-eda"))
from control_eda import review  # noqa: E402

CDC = textwrap.dedent("""\
    module cdc (
      input  logic       clk_a, clk_b, rst_n, irq,
      input  logic [3:0] data_a,
      output logic [3:0] bus_b,
      output logic       flag_b, bad_b, irq_q
    );
      logic [3:0] cnt_a;
      logic       req_a, s1, s2, b1, b2;
      always_ff @(posedge clk_a or negedge rst_n)
        if (!rst_n) begin cnt_a <= '0; req_a <= 1'b0; end
        else begin cnt_a <= cnt_a + data_a; req_a <= ~req_a; end
      // req_a: a correct two-flop synchronizer
      always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) begin s1 <= 1'b0; s2 <= 1'b0; end
        else begin s1 <= req_a; s2 <= s1; end
      always_ff @(posedge clk_b) begin b1 <= cnt_a; b2 <= b1; end
      assign bus_b = b2;
      always_ff @(posedge clk_b or negedge rst_n)
        if (!rst_n) bad_b <= 1'b0; else bad_b <= req_a & s2;
      always_ff @(posedge clk_b) irq_q <= irq;
      assign flag_b = s2;
      logic [1:0] sel; logic y;
      always_comb case (sel) 2'd0: y = 1'b0; 2'd1: y = 1'b1; endcase
      assign sel = (cnt_a[1:0] === 2'bx1) ? 2'd1 : 2'd0;
      always_ff @(posedge clk_a or negedge rst_n) begin
        y_q <= y;
        if (!rst_n) y_q <= 1'b0;
      end
    endmodule
    """)


def rules_of(findings):
    return {(f["rule"], f["line"]) for f in findings}


CONTROL_EDA = shutil.which("control-eda") or "control-eda"


def eda(*args: str, cwd: Path, env: dict | None = None):
    p = subprocess.run([CONTROL_EDA, *args, "--json"], cwd=cwd, capture_output=True, text=True, env=dict(os.environ, **(env or {})))
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, p.stdout + p.stderr


def project(tmp_path: Path) -> Path:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "cdc.sv").write_text(CDC)
    (tmp_path / "dv.toml").write_text('[project]\nname = "cdc"\nrtl_globs = ["rtl/*.sv"]\n\n[review]\nasync_inputs = ["irq"]\nverilator = false\n')
    return tmp_path


class TestScan:
    def setup_method(self):
        self.f = review.scan_text(CDC, "rtl/cdc.sv", async_inputs=["irq"])
        self.rules = {f["rule"] for f in self.f}

    def test_finds_each_crossing_kind(self):
        by = {(f["rule"], f["message"].split("`")[1]) for f in self.f if f["category"] == "cdc"}
        assert ("cdc-multibit-2ff", "cnt_a") in by
        assert ("cdc-unsynchronized", "req_a") in by
        assert ("cdc-single-flop", "irq") in by

    def test_correct_two_flop_sync_is_quiet(self):
        assert not any(f["category"] == "cdc" and f["line"] == 15 for f in self.f)

    def test_reset_findings(self):
        assert {"reset-multi-domain", "reset-async-not-first"} <= self.rules
        miss = [f for f in self.f if f["rule"] == "reset-missing"]
        assert any("b1" in f["message"] for f in miss)

    def test_xprop_findings(self):
        assert {"xprop-case-equality", "xprop-case-no-default"} <= self.rules

    def test_reference_fifo_is_clean(self):
        text = (EXAMPLE / "rtl" / "fifo.sv").read_text()
        assert review.scan_text(text, "rtl/fifo.sv") == []

    def test_comments_do_not_count(self):
        assert review.scan_text("module m(input a);\n// casex (a)\n/* x === y */\nendmodule\n", "m.sv") == []

    def test_cli_runs_without_any_eda_tool(self, tmp_path):
        p = project(tmp_path)
        code, d = eda("review", "scan", cwd=p, env={"PATH": str(Path(sys.executable).parent)})
        assert code == 0 and d["high"] >= 2 and (p / "evidence" / "review-scan.json").exists()
        assert [t["name"] for t in d["tools"]] == ["scan"]

    def test_custom_cdc_tool_from_dv_toml(self, tmp_path):
        p = project(tmp_path)
        tool = p / "fake_cdc.py"
        tool.write_text("import sys\nfor f in sys.argv[1:]:\n    print(f'{f}:19: CDC_UNSYNC req_a crosses clk_a->clk_b')\n")
        with (p / "dv.toml").open("a") as fh:
            fh.write(f'\n[review.tools.sitecdc]\nexe = "{sys.executable}"\ncmd = ["{{{{exe}}}}", "{tool}", "{{{{files}}}}"]\n'
                     "pattern = '(?P<file>[^:\\s]+):(?P<line>\\d+): (?P<rule>\\w+) (?P<message>.*)'\nseverity = \"high\"\n"
                     '\n[review.tools.missing]\nexe = "no-such-cdc-tool"\n')
        code, d = eda("review", "scan", cwd=p)
        site = [f for f in d["findings"] if f["source"] == "sitecdc"]
        assert site and site[0]["line"] == 19 and site[0]["rule"] == "sitecdc-CDC_UNSYNC"
        status = {t["name"]: t for t in d["tools"]}
        assert status["missing"]["available"] is False and "not on PATH" in status["missing"]["install"]


def write(p: Path, name: str, model: str, findings: list[dict], cleared: list[dict] | None = None) -> Path:
    (p / "reviews").mkdir(exist_ok=True)
    f = p / "reviews" / f"{name}.json"
    f.write_text(json.dumps({"reviewer": name, "model": model, "findings": findings, "cleared": cleared or []}))
    return f


class TestMerge:
    def test_statuses_and_verdict(self, tmp_path):
        p = project(tmp_path)
        eda("review", "scan", cwd=p)
        write(p, "a", "opus", [
            {"file": "rtl/cdc.sv", "line": 16, "category": "cdc", "severity": "high", "claim": "cnt_a multi-bit through 2 flops"},
            {"file": "rtl/cdc.sv", "line": 19, "category": "cdc", "severity": "high", "claim": "req_a raw in clk_b"},
            {"file": "rtl/cdc.sv", "line": 23, "category": "xprop", "severity": "low", "claim": "no default"}])
        write(p, "b", "sonnet", [
            {"file": "rtl/cdc.sv", "line": 17, "category": "cdc", "severity": "high", "claim": "bus skew on cnt_a"}],
            cleared=[{"file": "rtl/cdc.sv", "line": 23, "category": "xprop", "claim": "sel is only 0 or 1"}])
        code, d = eda("review", "merge", cwd=p)
        st = {(f["line"], f["category"]): f["status"] for f in d["findings"]}
        assert st[(16, "cdc")] == "agreed"
        assert st[(23, "xprop")] == "contested"
        assert d["verdict"] == "CHANGES_REQUESTED" and not d["single_model"]
        assert all(f["needs_human"] for f in d["findings"] if f["status"] in ("single", "contested"))
        assert (p / "evidence" / "review.md").read_text().startswith("## RTL review: CHANGES_REQUESTED")
        # irq single-flop and the reset findings were raised by no reviewer
        assert any(t["rule"] == "cdc-single-flop" for t in d["tool_only"]) or any(f["tool_backed"] for f in d["findings"])

    def test_disagreement_needs_human(self, tmp_path):
        p = project(tmp_path)
        write(p, "a", "opus", [{"file": "rtl/cdc.sv", "line": 19, "category": "cdc", "severity": "medium", "claim": "x"}])
        write(p, "b", "sonnet", [])
        code, d = eda("review", "merge", "--no-scan", cwd=p)
        assert d["findings"][0]["status"] == "single" and d["verdict"] == "NEEDS_HUMAN"

    def test_same_model_is_flagged(self, tmp_path):
        p = project(tmp_path)
        f = [{"file": "rtl/cdc.sv", "line": 19, "category": "cdc", "severity": "high", "claim": "x"}]
        write(p, "a", "opus", f)
        write(p, "b", "opus", f)
        code, d = eda("review", "merge", "--no-scan", cwd=p)
        assert d["single_model"] and "same model" in d["note"]

    def test_one_reviewer_is_single_model(self, tmp_path):
        p = project(tmp_path)
        write(p, "a", "gemini", [{"file": "rtl/cdc.sv", "line": 19, "category": "cdc", "severity": "high", "claim": "x"}])
        code, d = eda("review", "merge", "--no-scan", cwd=p)
        assert d["findings"][0]["status"] == "single_model" and d["findings"][0]["needs_human"]

    def test_clean(self, tmp_path):
        p = project(tmp_path)
        write(p, "a", "opus", [])
        write(p, "b", "sonnet", [])
        code, d = eda("review", "merge", "--no-scan", cwd=p)
        assert d["verdict"] == "CLEAN"

    def test_no_reviews_is_a_clear_error(self, tmp_path):
        p = project(tmp_path)
        code, d = eda("review", "merge", cwd=p)
        assert code == 1 and "reviews/" in d["hint"]


def test_reviewer_agents_share_one_body_and_differ_in_model():
    agents = sorted((ROOT / "plugins" / "rtl" / "agents").glob("rtl-reviewer-*.md"))
    assert len(agents) >= 2
    bodies, models = set(), []
    for a in agents:
        text = a.read_text()
        front, body = text.split("---\n", 2)[1:]
        bodies.add(body)
        models.append(re.search(r"^model:\s*(\S+)", front, re.M).group(1))
    assert len(bodies) == 1, "reviewer prompts drifted apart; keep them identical"
    assert len(set(models)) == len(models), "reviewers must run on different models"


def test_install_puts_rtl_in_cursor_and_antigravity(tmp_path):
    for env, root in (("cursor", ".cursor"), ("antigravity", ".agents")):
        d = tmp_path / env
        d.mkdir()
        p = subprocess.run(["control-eda", "install", "--env", env, "--project", str(d), "--plugins", "rtl", "--json"], capture_output=True, text=True)
        r = json.loads(p.stdout)
        assert r["plugins"] == ["dv", "rtl"]
        assert (d / root / "skills" / "review" / "SKILL.md").exists()
    assert (tmp_path / "cursor" / ".cursor" / "agents" / "rtl-reviewer-a.md").exists()
    assert (tmp_path / "cursor" / ".cursor" / "rules" / "rtl-review.mdc").exists()
    assert (tmp_path / "antigravity" / ".agents" / "skills" / "rtl-reviewer-b" / "SKILL.md").exists()
