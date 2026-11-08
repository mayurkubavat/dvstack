"""formal plugin: SVA rewrite, backend detection, the anti-vacuity gate, equivalence and rules.

Tests that need Yosys skip without it; the tool-missing tests run everywhere, because
"what happens on a machine without the tool" is the case most users hit first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "fifo_block"
sys.path.insert(0, str(ROOT / "tools" / "control-eda"))
from control_eda.formal import sva  # noqa: E402

HAS_YOSYS = all(shutil.which(x) for x in ("yosys", "yosys-smtbmc")) and any(shutil.which(s) for s in ("z3", "yices-smt2", "bitwuzla"))
HIDE_ALL = "yosys,yosys-smtbmc,sby,eqy,jg,vcf,qverify,lec,fm_shell"
needs_yosys = pytest.mark.skipif(not HAS_YOSYS, reason="yosys + yosys-smtbmc + a solver not installed")


def eda(*args: str, cwd: Path = EXAMPLE, env: dict | None = None) -> tuple[int, dict | str]:
    e = dict(os.environ, **(env or {}))
    p = subprocess.run(["control-eda", *args, "--json"], cwd=cwd, capture_output=True, text=True, env=e)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, p.stdout + p.stderr


def copy_example(tmp_path: Path) -> Path:
    dst = tmp_path / "p"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("build", "evidence"))
    return dst


def git_repo(path: Path) -> None:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "base"]):
        subprocess.run(cmd, cwd=path, check=True, env=env, capture_output=True)


# ---------------------------------------------------------------- SVA rewrite (no tools)

class TestSva:
    SRC = textwrap.dedent("""\
        module m(input clk, input rst_n, input req, input gnt, input [1:0] st);
          // b_commented: assert property (@(posedge clk) req);
          a_simple: assert property (@(posedge clk) disable iff (!rst_n) !(req && gnt));
          a_impl: assert property (@(posedge clk) disable iff (!rst_n)
                                   req |-> gnt);
          a_next: assert property (@(posedge clk) req |=> $past(req));
          a_seq: assert property (@(posedge clk) req ##1 gnt |-> st == 0);
          m_env: assume property (@(posedge clk) disable iff (!rst_n) !(req && st == 3));
          c_req: cover property (@(posedge clk) req);
          assert property (@(posedge clk) st != 2);
        endmodule
        """)

    def props(self):
        return {p.label: p for p in sva.extract(self.SRC, "m.sv")}

    def test_extracts_labels_kinds_and_skips_comments(self):
        ps = self.props()
        assert "b_commented" not in ps
        assert ps["a_simple"].kind == "assert" and ps["m_env"].kind == "assume" and ps["c_req"].kind == "cover"
        assert ps["a_impl"].implication == "|->" and ps["a_impl"].antecedent == "req"
        assert ps["a_impl"].disable == "!rst_n" and ps["a_impl"].line == 4
        unlabeled = [p for p in ps.values() if not p.named_label]
        assert len(unlabeled) == 1 and unlabeled[0].label.startswith("__cea_assert_L")

    def test_sequences_are_unsupported_not_guessed(self):
        ps = self.props()
        assert ps["a_seq"].unsupported and "sequence" in ps["a_seq"].unsupported
        assert ps["a_simple"].unsupported is None and ps["a_next"].unsupported is None

    def test_shim_keeps_line_count_and_rewrites(self):
        ps = sva.extract(self.SRC, "m.sv")
        out = sva.shim(self.SRC, ps, translate=True, top="m", reset="!rst_n")
        assert out.count("\n") == self.SRC.count("\n")
        assert "a_impl: assert(!(req) || (gnt));" in out
        assert "a_impl__antecedent: cover(req);" in out
        assert "a_next: assert(!$past(req) || ($past(req)));" in out and "__cea_pv_clk" in out
        assert "a_seq not checked by free Yosys" in out
        assert "initial assume (!rst_n);" in out
        assert "assert property" not in sva.blank_comments(out)

    def test_native_mode_adds_antecedent_cover_only(self):
        ps = sva.extract(self.SRC, "m.sv")
        out = sva.shim(self.SRC, ps, translate=False)
        assert "a_impl__antecedent: cover property (@(posedge clk) disable iff (!rst_n) req);" in out
        assert "a_seq__antecedent: cover property (@(posedge clk) req ##1 gnt);" in out

    def test_strip_removes_every_property(self):
        ps = sva.extract(self.SRC, "m.sv")
        out = sva.shim(self.SRC, ps, translate=False, strip=True)
        assert not sva.extract(out, "m.sv") and out.count("\n") == self.SRC.count("\n")


# ---------------------------------------------------------------- tool missing (runs everywhere)

class TestWithoutTools:
    def test_detect_reports_install_hints(self):
        code, d = eda("formal", "detect", env={"CONTROL_EDA_HIDE_TOOLS": HIDE_ALL})
        assert code == 0 and d["selected"] == {"formal": None, "equiv": None}
        assert any("apt install yosys" in h for h in d["hints"])
        names = {b["name"] for b in d["formal"]}
        assert {"sby", "yosys", "jasper", "vcformal", "questa"} <= names
        assert all(not b["available"] and b["missing"] for b in d["formal"])

    def test_formal_check_is_inconclusive_not_pass(self):
        code, d = eda("formal", "check", env={"CONTROL_EDA_HIDE_TOOLS": HIDE_ALL})
        assert code == 1 and d["verdict"] == "INCONCLUSIVE" and d["status"] == "tool_missing"
        assert d["asserts"] and all(a["proof"] == "tool_missing" for a in d["asserts"])
        assert any("Install" in r for r in d["reasons"])

    def test_equiv_is_inconclusive(self):
        code, d = eda("equiv", "--base", "HEAD", env={"CONTROL_EDA_HIDE_TOOLS": HIDE_ALL})
        assert code == 1 and d["verdict"] == "INCONCLUSIVE" and d["status"] == "tool_missing"

    def test_requested_backend_missing_says_which(self):
        code, d = eda("formal", "check", "--backend", "jasper")
        assert d["status"] == "tool_missing" and any("jasper" in g for g in d["gaps"])

    def test_no_formal_section(self, tmp_path):
        p = copy_example(tmp_path)
        toml = p / "dv.toml"
        text = toml.read_text()
        toml.write_text(text[: text.index("[formal]")] + text[text.index("[tests.tb_fifo]"):])
        code, d = eda("formal", "check", cwd=p)
        assert d["status"] == "not_configured" and d["verdict"] == "INCONCLUSIVE"

    def test_doctor_stays_ok_when_formal_is_optional(self):
        code, d = eda("doctor", env={"CONTROL_EDA_HIDE_TOOLS": HIDE_ALL})
        assert d["formal"]["selected"]["formal"] is None
        assert not any("formal" in p for p in d["problems"]) and any("formal" in h for h in d["hints"])


class TestCustomBackend:
    """A team's own flow plugs in through dv.toml, no Python."""

    def test_command_backend_from_dv_toml(self, tmp_path):
        p = copy_example(tmp_path)
        tool = p / "fake_formal.py"
        tool.write_text(textwrap.dedent("""\
            import sys
            script = open(sys.argv[1]).read()
            mode = script.split("mode=")[1].split()[0]
            if mode in ("prove", "bmc"):
                bug = "FIFO_BUG" in script
                for a in ["a_no_overflow", "a_full_empty_excl", "a_full_iff_depth", "a_ptr_count", "a_full_holds"]:
                    print(("FAILED " if bug and a == "a_ptr_count" else "PROVEN ") + a)
            else:
                for c in ["c_full", "c_push_pop", "c_push_when_full", "a_full_holds__antecedent"]:
                    print("HIT " + c)
            """))
        toml = p / "dv.toml"
        toml.write_text(toml.read_text().replace('backend = "auto"              # auto | sby', 'backend = "inhouse"           # auto | sby', 1) + textwrap.dedent(f"""
            [formal.backends.inhouse]
            exe = "{sys.executable}"
            cmd = ["{{{{exe}}}}", "{tool}", "{{{{script}}}}"]
            script = "mode={{{{mode}}}} defines={{{{defines}}}} top={{{{top}}}}"
            patterns.assert_proven = 'PROVEN (?P<name>\\w+)'
            patterns.assert_cex = 'FAILED (?P<name>\\w+)'
            patterns.cover_reached = 'HIT (?P<name>\\w+)'
            """))
        code, d = eda("formal", "check", cwd=p)
        assert d["backend"] == "inhouse", d
        by = {a["name"]: a for a in d["asserts"]}
        assert by["a_ptr_count"]["proof"] == "proven" and by["a_ptr_count"]["witness"] == "reached"
        faults = {f["fault"]: f["state"] for f in d["faults"]}
        assert faults["FIFO_BUG_DOUBLE_POP"] == "caught"
        # the fake flow reports a_ptr_count for every fault, so FULL_EARLY is caught only outside its row
        assert faults["FIFO_BUG_FULL_EARLY"] == "caught_elsewhere"
        assert d["verdict"] == "INCONCLUSIVE"

    def test_silent_backend_is_unknown_not_pass(self, tmp_path):
        p = copy_example(tmp_path)
        toml = p / "dv.toml"
        toml.write_text(toml.read_text() + textwrap.dedent(f"""
            [formal.backends.quiet]
            exe = "{sys.executable}"
            cmd = ["{{{{exe}}}}", "-c", "print('done')"]
            """))
        code, d = eda("formal", "check", "--backend", "quiet", cwd=p)
        assert d["verdict"] == "INCONCLUSIVE"
        assert all(a["proof"] == "unknown" for a in d["asserts"])


# ---------------------------------------------------------------- with yosys

@needs_yosys
class TestFormalGate:
    def test_reference_block_passes(self):
        code, d = eda("formal", "check")
        assert code == 0 and d["verdict"] == "PASS", d.get("reasons")
        assert all(a["proof"] == "proven" and a["witness"] == "reached" for a in d["asserts"])
        assert {f["fault"]: f["state"] for f in d["faults"]} == {"FIFO_BUG_DOUBLE_POP": "caught", "FIFO_BUG_FULL_EARLY": "caught"}
        assert any(c["auto"] and c["state"] == "reached" for c in d["covers"])

    def test_real_bug_gives_counterexample(self, tmp_path):
        p = copy_example(tmp_path)
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace("wire do_pop  = pop && !empty;", "wire do_pop  = pop;"))
        code, d = eda("formal", "check", "--only", "prove", cwd=p)
        assert d["verdict"] == "FAIL"
        assert any(a["proof"] == "cex" for a in d["asserts"]) and d.get("cex_trace")

    def test_weak_assertion_misses_the_fault(self, tmp_path):
        """The original two flag assertions cannot see FIFO_BUG_FULL_EARLY; formal says so."""
        p = copy_example(tmp_path)
        vp = p / "vplan.yaml"
        vp.write_text(vp.read_text().replace("assertions: [a_full_empty_excl, a_no_overflow, a_full_iff_depth, a_full_holds]",
                                             "assertions: [a_full_empty_excl, a_no_overflow]"))
        code, d = eda("formal", "check", "--only", "mutate", cwd=p)
        f = {x["fault"]: x for x in d["faults"]}["FIFO_BUG_FULL_EARLY"]
        assert f["state"] == "caught_elsewhere" and "a_full_iff_depth" in f["cex"]

    def test_vacuous_assertion_fails(self, tmp_path):
        p = copy_example(tmp_path)
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace(
            "  c_full:", "  a_vacuous: assert property (@(posedge clk) disable iff (!rst_n) (full && empty) |-> wdata == 0);\n  c_full:"))
        vp = p / "vplan.yaml"
        vp.write_text(vp.read_text().replace("a_full_iff_depth, a_full_holds]", "a_full_iff_depth, a_full_holds, a_vacuous]"))
        code, d = eda("formal", "check", "--only", "prove,cover", cwd=p)
        by = {a["name"]: a for a in d["asserts"]}
        assert by["a_vacuous"]["proof"] == "proven" and by["a_vacuous"]["witness"] in ("reached", "unreachable")
        cov = {c["name"]: c["state"] for c in d["covers"]}
        assert cov["a_vacuous__antecedent"] == "unreachable"

    def test_vacuous_assertion_alone_on_row_fails(self, tmp_path):
        p = copy_example(tmp_path)
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace(
            "  c_full:", "  a_vacuous: assert property (@(posedge clk) disable iff (!rst_n) (full && empty) |-> wdata == 0);\n  c_full:"))
        vp = p / "vplan.yaml"
        vp.write_text(vp.read_text() + textwrap.dedent("""\
              - id: fifo.vacuous
                summary: an assertion whose trigger can never happen
                rtl: [rtl/fifo.sv]
                tests: [tb_fifo]
                assertions: [a_vacuous]
                covers: []
                faults: [FIFO_BUG_FULL_EARLY]
                door: two-way
                status: written
            """))
        code, d = eda("formal", "check", cwd=p)
        assert d["verdict"] == "FAIL", d.get("reasons")
        assert {a["name"]: a for a in d["asserts"]}["a_vacuous"]["witness"] == "unreachable"
        assert any("vacuous" in r for r in d["reasons"])

    def test_unlisted_assume_is_inconclusive(self, tmp_path):
        p = copy_example(tmp_path)
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace("  c_full:", "  m_no_pop_empty: assume property (@(posedge clk) disable iff (!rst_n) !(pop && empty));\n  c_full:"))
        code, d = eda("formal", "check", "--only", "assumes", cwd=p)
        assert {s["name"]: s["state"] for s in d["assumes"]} == {"m_no_pop_empty": "unlisted"}
        vp = p / "vplan.yaml"
        vp.write_text(vp.read_text().replace("    faults: [FIFO_BUG_FULL_EARLY]", "    faults: [FIFO_BUG_FULL_EARLY]\n    assumes: {m_no_pop_empty: a_no_overflow}", 1))
        code, d = eda("formal", "check", "--only", "assumes", cwd=p)
        assert {s["name"]: s["state"] for s in d["assumes"]} == {"m_no_pop_empty": "matched"}

    def test_verify_folds_formal_into_the_bundle(self, tmp_path):
        if not shutil.which("verilator"):
            pytest.skip("verilator not installed")
        p = copy_example(tmp_path)
        code, d = eda("verify", "--claim", "[measured] gate", cwd=p)
        assert d["verdict"] == "PASS", d
        assert d["formal"]["verdict"] == "PASS" and all(s["proof"] == "proven" for s in d["sva"])
        code, d = eda("verify", "--claim", "[measured] gate", cwd=p, env={"CONTROL_EDA_HIDE_TOOLS": "yosys,yosys-smtbmc,sby"})
        assert d["verdict"] == "PASS" and d["formal"]["status"] == "tool_missing"
        assert any("formal not run" in g for g in d["gaps"])
        toml = p / "dv.toml"
        toml.write_text(toml.read_text().replace("required = false", "required = true"))
        code, d = eda("verify", "--claim", "[measured] gate", cwd=p, env={"CONTROL_EDA_HIDE_TOOLS": "yosys,yosys-smtbmc,sby"})
        assert d["verdict"] == "INCONCLUSIVE"


@needs_yosys
class TestEquiv:
    def test_refactor_equivalent_and_bug_not(self, tmp_path):
        p = copy_example(tmp_path)
        git_repo(p)
        rtl = p / "rtl" / "fifo.sv"
        orig = rtl.read_text()
        rtl.write_text(orig.replace("assign empty = (count == '0);", "assign empty = ~|count;").replace("// Properties.", "// Props."))
        code, d = eda("equiv", "--base", "HEAD", cwd=p)
        assert code == 0 and d["status"] == "equivalent", d
        rtl.write_text(orig.replace("assign empty = (count == '0);", "assign empty = (count <= 1);"))
        code, d = eda("equiv", "--base", "HEAD", cwd=p)
        assert code == 1 and d["status"] == "not_proven" and d["unproven"] == ["empty"]

    def test_changed_assertions_do_not_matter(self, tmp_path):
        p = copy_example(tmp_path)
        git_repo(p)
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace("  c_full:", "  a_extra: assert property (@(posedge clk) 1);\n  c_full:"))
        code, d = eda("equiv", "--base", "HEAD", cwd=p)
        assert d["status"] == "equivalent"


# ---------------------------------------------------------------- rules (no tools for lint)

class TestRules:
    def test_example_rules_fire(self):
        if HAS_YOSYS:
            eda("formal", "check", "--only", "mutate")
        code, d = eda("rules", "test")
        states = {r["id"]: r["state"] for r in d["rules"]}
        assert states["no-casex"] == "fires" and states["no-delay-in-rtl"] == "fires"
        assert states["fifo-full-exact"] == ("fires" if HAS_YOSYS else states["fifo-full-exact"])

    def test_rule_that_never_fires_is_draft(self, tmp_path):
        p = copy_example(tmp_path)
        rules = p / "rules.yaml"
        rules.write_text(rules.read_text().replace("bad: [\"casex (sel)\"", "bad: [\"case (sel)\""))
        code, d = eda("rules", "test", cwd=p)
        assert code == 1 and {r["id"]: r["state"] for r in d["rules"]}["no-casex"] == "draft"

    def test_sva_rule_without_formal_is_unverified(self, tmp_path):
        p = copy_example(tmp_path)
        code, d = eda("rules", "test", cwd=p)
        assert {r["id"]: r["state"] for r in d["rules"]}["fifo-full-exact"] == "unverified"

    def test_lint_enforces_tested_rules(self, tmp_path):
        p = copy_example(tmp_path)
        rtl = p / "rtl" / "fifo.sv"
        rtl.write_text(rtl.read_text().replace("  assign empty", "  // casex in a comment is fine\n  always_comb casex (wptr) default: ; endcase\n  assign empty"))
        code, d = eda("rules", "check", cwd=p)
        assert d["violation_count"] == 1 and d["violations"][0]["rule"] == "no-casex"
        if shutil.which("verilator"):
            code, d = eda("lint", cwd=p)
            assert code == 1 and any(e["code"] == "rule:no-casex" for e in d["errors"])
