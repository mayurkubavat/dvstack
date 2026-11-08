"""Formal and equivalence backends, found at run time.

Nothing here assumes a tool is installed. Each backend reports what it needs
(`detect`), and the gate picks one that is present. Built in:

  formal  sby        SymbiYosys (OSS CAD Suite)                 open source, tested via yosys
          yosys      yosys + yosys-smtbmc + an SMT solver        open source, tested in CI
          jasper     Cadence JasperGold  (jg)                    template, check against your flow
          vcformal   Synopsys VC Formal  (vcf)                   template, check against your flow
          questa     Siemens Questa Formal (qverify)             template, check against your flow
  equiv   eqy        Yosys EQY (OSS CAD Suite)                   open source
          yosys      yosys equiv_make / equiv_induct              open source, tested in CI
          conformal  Cadence Conformal LEC (lec)                 template
          formality  Synopsys Formality (fm_shell)               template

Commercial backends are data, not code: a command, a Tcl template and the
regexes that read the log. A project can override any field, or add its own
backend, in dv.toml:

    [formal.backends.jasper]
    script = "my_flow/jg.tcl"            # a file, or inline Tcl
    patterns.assert_proven = '...(?P<name>\\w+)...'

    [formal.backends.inhouse]            # a new backend, no Python needed
    exe = "run_formal.sh"
    cmd = ["{{exe}}", "{{script}}"]
    script = "..."
    patterns.assert_cex = 'FAIL (?P<name>\\w+)'

Anything a backend's log does not mention is reported `unknown`, never pass.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

SOLVERS = ["yices-smt2", "bitwuzla", "z3", "boolector"]
SOLVER_NAME = {"yices-smt2": "yices", "bitwuzla": "bitwuzla", "z3": "z3", "boolector": "boolector"}

OSS_HINT = ("apt install yosys z3 (Debian/Ubuntu), brew install yosys z3 (macOS), or the OSS CAD Suite "
            "(https://github.com/YosysHQ/oss-cad-suite-build), which also ships sby, eqy and yices")
SBY_HINT = "SymbiYosys ships in the OSS CAD Suite (https://github.com/YosysHQ/oss-cad-suite-build)"
EQY_HINT = "EQY ships in the OSS CAD Suite (https://github.com/YosysHQ/oss-cad-suite-build)"
LICENSE_RE = re.compile(r"licen[cs]e.*(fail|denied|not available|unavailable|cannot|could not|error)|"
                        r"(fail|unable|cannot|could not).*licen[cs]e", re.I)


def _which(name: str) -> str | None:
    """shutil.which, except tools listed in CONTROL_EDA_HIDE_TOOLS (comma list) are treated as absent.
    Lets anyone see what the gate does on a machine without a tool: CONTROL_EDA_HIDE_TOOLS=yosys,sby."""
    hidden = {x.strip() for x in os.environ.get("CONTROL_EDA_HIDE_TOOLS", "").split(",") if x.strip()}
    if name in hidden or Path(name).name in hidden:
        return None
    return shutil.which(name)


@dataclass
class Job:
    mode: str                      # prove | cover | bmc | equiv
    top: str
    workdir: Path
    files: list[Path] = field(default_factory=list)        # formal: files to read
    gold: list[Path] = field(default_factory=list)         # equiv
    gate: list[Path] = field(default_factory=list)         # equiv
    depth: int = 20
    defines: list[str] = field(default_factory=list)
    include_dirs: list[str] = field(default_factory=list)
    clock: str | None = None
    reset: str | None = None       # expression true during reset
    asserts: list[str] = field(default_factory=list)
    covers: list[str] = field(default_factory=list)
    timeout: int = 900
    solver: str | None = None
    extra_flags: list[str] = field(default_factory=list)


def _version(exe: str, flag: str = "--version") -> str | None:
    path = _which(exe)
    if not path:
        return None
    try:
        p = subprocess.run([path, flag], capture_output=True, text=True, timeout=20)
        out = (p.stdout or p.stderr).strip().splitlines()
        return out[0][:120] if out else "present"
    except (subprocess.TimeoutExpired, OSError):
        return "present"


def _run(cmd: list[str], cwd: Path, log: Path, timeout: int) -> tuple[int, str, float]:
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out, code = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        out += f"\n[control-eda] timed out after {timeout}s\n"
        code = 124
    except OSError as e:
        out, code = f"[control-eda] could not start {cmd[0]}: {e}\n", 127
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write("$ " + " ".join(cmd) + "\n" + out + "\n")
    return code, out, time.time() - t0


def available_solvers() -> list[str]:
    return [s for s in SOLVERS if _which(s)]


def pick_solver(wanted: str | None) -> str | None:
    have = available_solvers()
    if wanted:
        exe = {v: k for k, v in SOLVER_NAME.items()}.get(wanted, wanted)
        return SOLVER_NAME.get(exe, wanted) if exe in have else None
    return SOLVER_NAME[have[0]] if have else None


def _label_in(line: str, labels: list[str]) -> str | None:
    for lb in sorted(labels, key=len, reverse=True):
        if re.search(rf"(?<![\w$]){re.escape(lb)}(?![\w$])", line):
            return lb
    return None


class Backend:
    name = "base"
    kind = "formal"        # formal | equiv
    family = "oss"         # oss | commercial | custom
    title = ""
    native_sva = False
    tested = True

    def detect(self) -> dict:
        raise NotImplementedError

    def run(self, job: Job) -> dict:
        raise NotImplementedError

    def info(self) -> dict:
        d = self.detect()
        d.update({"name": self.name, "kind": self.kind, "family": self.family, "title": self.title,
                  "native_sva": self.native_sva, "tested": self.tested})
        return d


# ---------------------------------------------------------------- yosys-smtbmc

class YosysSmtbmc(Backend):
    name, kind, family, title = "yosys", "formal", "oss", "Yosys + yosys-smtbmc"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self._verific: bool | None = None

    def has_verific(self) -> bool:
        if self._verific is None:
            self._verific = False
            if _which("yosys"):
                try:
                    # the `verific` command exists as a stub in builds without it; a stub errors out
                    p = subprocess.run(["yosys", "-p", "verific -help"], capture_output=True, text=True, timeout=20)
                    out = p.stdout + p.stderr
                    self._verific = p.returncode == 0 and "without Verific" not in out and "ERROR" not in out
                except (subprocess.TimeoutExpired, OSError):
                    pass
        return self._verific

    @property
    def native_sva(self) -> bool:  # type: ignore[override]
        return self.has_verific()

    def detect(self) -> dict:
        missing = [x for x in ("yosys", "yosys-smtbmc") if not _which(x)]
        solvers = available_solvers()
        if not solvers:
            missing.append("an SMT solver (yices-smt2, bitwuzla, z3 or boolector)")
        return {"available": not missing, "version": _version("yosys", "-V"), "missing": missing,
                "solvers": solvers, "install": OSS_HINT if missing else None}

    def _model(self, job: Job, log: Path) -> tuple[Path | None, str]:
        read = "read -formal -sv" if self.has_verific() else "read_verilog -formal -sv"
        defs = " ".join(f"-D{d}" for d in job.defines)
        incs = " ".join(f"-I{i}" for i in job.include_dirs)
        files = " ".join(f'"{f}"' for f in job.files)
        smt2 = job.workdir / "model.smt2"
        script = job.workdir / "model.ys"
        script.write_text(f"{read} {defs} {incs} {files}\nprep -top {job.top}\nasync2sync\ndffunmap\nwrite_smt2 -wires \"{smt2}\"\n")
        code, out, _ = _run(["yosys", "-q", "-s", str(script)], job.workdir, log, job.timeout)
        return (smt2 if code == 0 and smt2.exists() else None), out

    def _smtbmc(self, job: Job, args: list[str], log: Path) -> tuple[int, str]:
        solver = pick_solver(job.solver) or "z3"
        flags = list(self.cfg.get("smtbmc_flags", ["--unroll"] if solver == "z3" else []))
        cmd = ["yosys-smtbmc", "-s", solver, *flags, *job.extra_flags, *args, str(job.workdir / "model.smt2")]
        code, out, _ = _run(cmd, job.workdir, log, job.timeout)
        return code, out

    def run(self, job: Job) -> dict:
        t0 = time.time()
        job.workdir.mkdir(parents=True, exist_ok=True)
        log = job.workdir / f"{job.mode}.log"
        log.write_text("")
        res = {"backend": self.name, "mode": job.mode, "status": "error", "asserts": {}, "covers": {}, "log": str(log)}
        smt2, out = self._model(job, log)
        if smt2 is None:
            res["detail"] = "yosys could not read the design: " + _tail(out)
            return _done(res, t0)
        if job.mode in ("prove", "bmc"):
            code, out = self._smtbmc(job, ["--keep-going", "-t", str(job.depth), "--dump-vcd", str(job.workdir / "cex.vcd")], log)
            if "Status: PASSED" not in out and "Status: FAILED" not in out:
                res["detail"] = "yosys-smtbmc did not finish: " + _tail(out)
                return _done(res, t0)
            cex = {lb for line in out.splitlines() if "Assert failed" in line for lb in [_label_in(line, job.asserts)] if lb}
            res["asserts"] = {a: ("cex" if a in cex else "bounded") for a in job.asserts}
            if (job.workdir / "cex.vcd").exists() and cex:
                res["trace"] = str(job.workdir / "cex.vcd")
            if cex or "Status: FAILED" in out:
                res["status"] = "fail"
                if "Status: FAILED" in out and not cex:
                    res["detail"] = "an assertion failed that has no label; label every assert"
                return _done(res, t0)
            res["status"] = "pass"
            if job.mode == "prove":
                code, out = self._smtbmc(job, ["-i", "-t", str(job.depth)], log)
                if "Status: PASSED" in out:
                    res["asserts"] = {a: "proven" for a in job.asserts}
                else:
                    res["detail"] = f"no counterexample within {job.depth} cycles, but induction at depth {job.depth} did not close"
            return _done(res, t0)
        if job.mode == "cover":
            code, out = self._smtbmc(job, ["-c", "-t", str(job.depth), "--dump-vcd", str(job.workdir / "cover_%.vcd")], log)
            if "Status: PASSED" not in out and "Status: FAILED" not in out:
                res["detail"] = "yosys-smtbmc did not finish: " + _tail(out)
                return _done(res, t0)
            reached = {}
            for line in out.splitlines():
                if "Reached cover statement" in line:
                    lb = _label_in(line, job.covers)
                    m = re.search(r"in step (\d+)", line)
                    if lb:
                        reached[lb] = int(m.group(1)) if m else None
            res["covers"] = {c: ("reached" if c in reached else "unreachable") for c in job.covers}
            res["steps"] = reached
            res["witnesses"] = sorted(str(p) for p in job.workdir.glob("cover_*.vcd"))
            res["status"] = "pass" if len(reached) == len(job.covers) else "fail"
            return _done(res, t0)
        raise ValueError(job.mode)


# ---------------------------------------------------------------- SymbiYosys

class Sby(YosysSmtbmc):
    name, kind, family, title = "sby", "formal", "oss", "SymbiYosys"
    tested = False   # parsed like yosys-smtbmc, which CI exercises; sby itself is not in CI

    def detect(self) -> dict:
        d = super().detect()
        if not _which("sby"):
            d["missing"].insert(0, "sby")
            d["available"] = False
            d["install"] = SBY_HINT
        d["version"] = _version("sby", "--version") or d.get("version")
        return d

    def run(self, job: Job) -> dict:
        t0 = time.time()
        job.workdir.mkdir(parents=True, exist_ok=True)
        log = job.workdir / f"{job.mode}.log"
        log.write_text("")
        solver = pick_solver(job.solver) or "z3"
        unroll = " --unroll" if solver == "z3" else ""
        read = "read -formal" if self.has_verific() else "read_verilog -formal -sv"
        defs = " ".join(f"-D{d}" for d in job.defines)
        mode = {"prove": "prove", "bmc": "bmc", "cover": "cover"}[job.mode]
        sby = job.workdir / "job.sby"
        sby.write_text(
            f"[options]\nmode {mode}\ndepth {job.depth}\n\n[engines]\nsmtbmc{unroll} {solver}\n\n"
            f"[script]\n{read} {defs} " + " ".join(f.name for f in job.files) + f"\nprep -top {job.top}\n\n"
            "[files]\n" + "\n".join(str(f) for f in job.files) + "\n")
        code, out, _ = _run(["sby", "-f", str(sby)], job.workdir, log, job.timeout)
        res = {"backend": self.name, "mode": job.mode, "status": "error", "asserts": {}, "covers": {}, "log": str(log)}
        m = re.search(r"DONE \((PASS|FAIL|UNKNOWN|TIMEOUT|ERROR)", out)
        if not m:
            res["detail"] = "sby did not finish: " + _tail(out)
            return _done(res, t0)
        done = m.group(1)
        if job.mode == "cover":
            reached = {lb for line in out.splitlines() if "Reached cover statement" in line for lb in [_label_in(line, job.covers)] if lb}
            res["covers"] = {c: ("reached" if c in reached else ("unreachable" if done in ("PASS", "FAIL") else "unknown")) for c in job.covers}
            res["status"] = "pass" if done == "PASS" and len(reached) == len(job.covers) else ("fail" if done in ("PASS", "FAIL") else "unknown")
            return _done(res, t0)
        cex = {lb for line in out.splitlines() if "Assert failed" in line and "induction" not in line
               for lb in [_label_in(line, job.asserts)] if lb}
        if done == "PASS":
            state = "proven" if job.mode == "prove" else "bounded"
            res["asserts"] = {a: state for a in job.asserts}
            res["status"] = "pass"
        elif cex or done == "FAIL":
            res["asserts"] = {a: ("cex" if a in cex else "unknown") for a in job.asserts}
            res["status"] = "fail"
        else:
            res["asserts"] = {a: ("bounded" if done == "UNKNOWN" else "unknown") for a in job.asserts}
            res["status"] = "pass" if done == "UNKNOWN" else "unknown"
            res["detail"] = f"sby returned {done}"
        return _done(res, t0)


# ---------------------------------------------------------------- yosys equivalence

class YosysEquiv(Backend):
    name, kind, family, title = "yosys", "equiv", "oss", "Yosys equiv_make / equiv_induct"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    def detect(self) -> dict:
        missing = [] if _which("yosys") else ["yosys"]
        return {"available": not missing, "version": _version("yosys", "-V"), "missing": missing, "install": OSS_HINT if missing else None}

    def run(self, job: Job) -> dict:
        t0 = time.time()
        job.workdir.mkdir(parents=True, exist_ok=True)
        log = job.workdir / "equiv.log"
        log.write_text("")
        defs = " ".join(f"-D{d}" for d in job.defines)
        incs = " ".join(f"-I{i}" for i in job.include_dirs)
        seq = int(self.cfg.get("seq", job.depth))

        def side(name: str, files: list[Path]) -> str:
            fl = " ".join(f'"{f}"' for f in files)
            return (f"read_verilog -sv {defs} {incs} {fl}\nprep -top {job.top}\nmemory_map\nopt_clean\n"
                    f"rename {job.top} {name}\ndesign -stash {name}\n")
        script = job.workdir / "equiv.ys"
        script.write_text(side("gold", job.gold) + side("gate", job.gate) +
                          "design -copy-from gold -as gold gold\ndesign -copy-from gate -as gate gate\n"
                          "equiv_make gold gate equiv\nhierarchy -top equiv\nasync2sync\n"
                          f"equiv_simple -seq {seq}\nequiv_induct -seq {seq}\nequiv_status\n")
        code, out, _ = _run(["yosys", "-s", str(script)], job.workdir, log, job.timeout)
        res = {"backend": self.name, "mode": "equiv", "status": "error", "log": str(log), "unproven": []}
        m = re.search(r"Of those cells (\d+) are proven and (\d+) are unproven", out)
        if code != 0 or not m:
            res["detail"] = "yosys equivalence run failed: " + _tail(out)
            return _done(res, t0)
        unproven = re.findall(r"Unproven \$equiv \S+: (\S+) (\S+)\s*$", out, re.M)
        res["unproven"] = sorted({re.sub(r"_gold$", "", a.lstrip("\\")) for a, _ in unproven})
        res["cells"] = {"proven": int(m.group(1)), "unproven": int(m.group(2))}
        res["status"] = "equivalent" if int(m.group(2)) == 0 else "not_proven"
        if res["status"] == "not_proven":
            res["detail"] = ("these signals were not proven equal. Either the change is not behavior-preserving, or "
                             "internal names changed so cells could not be matched; try eqy or a commercial LEC")
        return _done(res, t0)


class Eqy(Backend):
    name, kind, family, title = "eqy", "equiv", "oss", "Yosys EQY"
    tested = False

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    def detect(self) -> dict:
        missing = [x for x in ("eqy", "yosys") if not _which(x)]
        return {"available": not missing, "version": _version("eqy", "--version"), "missing": missing, "install": EQY_HINT if missing else None}

    def run(self, job: Job) -> dict:
        t0 = time.time()
        job.workdir.mkdir(parents=True, exist_ok=True)
        log = job.workdir / "equiv.log"
        log.write_text("")
        defs = " ".join(f"-D{d}" for d in job.defines)
        cfgf = job.workdir / "job.eqy"
        cfgf.write_text(
            "[gold]\n" + f"read_verilog -sv {defs} " + " ".join(f'"{f}"' for f in job.gold) + f"\nprep -top {job.top}\nmemory_map\n\n"
            "[gate]\n" + f"read_verilog -sv {defs} " + " ".join(f'"{f}"' for f in job.gate) + f"\nprep -top {job.top}\nmemory_map\n\n"
            f"[strategy sby]\nuse sby\ndepth {job.depth}\nengine smtbmc {pick_solver(job.solver) or 'z3'}\n")
        code, out, _ = _run(["eqy", "-f", str(cfgf)], job.workdir, log, job.timeout)
        res = {"backend": self.name, "mode": "equiv", "log": str(log), "unproven": []}
        if code == 0 and re.search(r"equivalen", out, re.I) and not re.search(r"not equivalent|FAIL", out):
            res["status"] = "equivalent"
        elif re.search(r"not equivalent|FAIL|unproven", out, re.I):
            res["status"] = "not_proven"
            res["unproven"] = sorted(set(re.findall(r"(?:failed|unproven)[^\n]*?([A-Za-z_][\w.]*)\s*$", out, re.M)))
        else:
            res["status"] = "error"
            res["detail"] = "eqy result not recognised: " + _tail(out)
        return _done(res, t0)


# ---------------------------------------------------------------- command presets

JASPER_TCL = """\
analyze -sv12 {{defines_plus}} {{files}}
elaborate -top {{top}}
clock {{clock}}
reset -expression {{reset}}
set_prove_time_limit {{timeout}}s
set_max_trace_length {{depth}}
{{mode_cmd}}
report -summary -force -result
exit -force
"""
VCF_TCL = """\
set_fml_appmode FPV
read_file -top {{top}} -format sverilog -sva -vcs {-sverilog {{defines_plus}} {{files}}}
create_clock {{clock}} -period 100
create_reset {{reset_signal}} -sense {{reset_sense}}
sim_run -stable
sim_save_reset
set_fml_var fml_max_time {{timeout}}S
{{mode_cmd}}
report_fv -list
quit
"""
QUESTA_DO = """\
netlist clock {{clock}}
netlist reset {{reset_signal}} -active_{{reset_sense}}
formal compile -d {{top}} -cuname cea
formal verify -timeout {{timeout}}s
exit
"""
CONFORMAL_DO = """\
read design -golden -sv {{gold}}
read design -revised -sv {{gate}}
set system mode lec
add compared points -all
compare
report verification
exit -force
"""
FORMALITY_TCL = """\
read_sverilog -r {{gold}}
set_top r:/WORK/{{top}}
read_sverilog -i {{gate}}
set_top i:/WORK/{{top}}
match
verify
exit
"""

# Generic readers: "<name> ... <status word>" on one line. Overridable per project.
STATUS = {
    "assert_proven": r"(?P<name>[\w.:\[\]$]+)\b[^\n]*?\b(proven|proved|pass(ed)?|safe)\b",
    "assert_cex": r"(?P<name>[\w.:\[\]$]+)\b[^\n]*?\b(cex|falsified|fail(ed)?|violated)\b",
    "assert_bounded": r"(?P<name>[\w.:\[\]$]+)\b[^\n]*?\b(undetermined|bounded|inconclusive)\b",
    "cover_reached": r"(?P<name>[\w.:\[\]$]+)\b[^\n]*?\b(covered|reached|witness)\b",
    "cover_unreachable": r"(?P<name>[\w.:\[\]$]+)\b[^\n]*?\b(unreachable|uncoverable|unreached)\b",
}
EQUIV_STATUS = {
    "equivalent": r"Verification SUCCEEDED|\bNon-?equivalent\s*[:=]?\s*0\b|\bEquivalent\b(?!.*\bNon)",
    "not_equivalent": r"Verification FAILED|\bNon-?equivalent\s*[:=]?\s*[1-9]|\bNon-?equivalent points?\b",
}

PRESETS: dict[str, dict] = {
    "jasper": {"kind": "formal", "title": "Cadence JasperGold", "exe": "jg",
               "cmd": ["{{exe}}", "-batch", "-no_gui", "-tcl", "{{script}}"], "script": JASPER_TCL,
               "mode_cmd": {"prove": "prove -all", "bmc": "prove -all -bg", "cover": "prove -all -cover"}, "patterns": STATUS},
    "vcformal": {"kind": "formal", "title": "Synopsys VC Formal", "exe": "vcf",
                 "cmd": ["{{exe}}", "-f", "{{script}}", "-batch"], "script": VCF_TCL,
                 "mode_cmd": {"prove": "check_fv -block", "bmc": "check_fv -block", "cover": "check_fv -block"}, "patterns": STATUS},
    "questa": {"kind": "formal", "title": "Siemens Questa Formal", "exe": "qverify",
               "cmd": ["{{exe}}", "-c", "-od", "{{workdir}}/qverify_out", "-do", "{{script}}"], "script": QUESTA_DO,
               "mode_cmd": {}, "patterns": STATUS},
    "conformal": {"kind": "equiv", "title": "Cadence Conformal LEC", "exe": "lec",
                  "cmd": ["{{exe}}", "-nogui", "-xl", "-dofile", "{{script}}"], "script": CONFORMAL_DO, "patterns": EQUIV_STATUS},
    "formality": {"kind": "equiv", "title": "Synopsys Formality", "exe": "fm_shell",
                  "cmd": ["{{exe}}", "-f", "{{script}}"], "script": FORMALITY_TCL, "patterns": EQUIV_STATUS},
}


def _fill(tpl: str, values: dict) -> str:
    def sub(m):
        return str(values.get(m.group(1), m.group(0)))
    return re.sub(r"\{\{(\w+)\}\}", sub, tpl)


class Command(Backend):
    """A backend described entirely by data: exe, command, script template, log regexes."""

    def __init__(self, name: str, spec: dict, family: str, root: Path | None = None):
        self.name = name
        self.spec = spec
        self.kind = spec.get("kind", "formal")
        self.family = family
        self.title = spec.get("title", name)
        self.native_sva = bool(spec.get("native_sva", True))
        self.tested = bool(spec.get("tested", False))
        self.root = root or Path.cwd()

    def detect(self) -> dict:
        exe = self.spec.get("exe")
        path = _which(exe) if exe else None
        if not path and exe and (self.root / exe).exists():
            path = str(self.root / exe)
        missing = [] if path else [exe or "exe (not set)"]
        hint = None
        if missing:
            hint = (f"{self.title} is not on PATH. If your site has it, load its module or set [{self.kind}.backends.{self.name}].exe in dv.toml"
                    if self.family == "commercial" else f"set [{self.kind}.backends.{self.name}].exe in dv.toml to a program that exists")
        return {"available": bool(path), "version": None, "missing": missing, "install": hint, "exe": path}

    def run(self, job: Job) -> dict:
        t0 = time.time()
        job.workdir.mkdir(parents=True, exist_ok=True)
        log = job.workdir / f"{job.mode}.log"
        log.write_text("")
        reset = job.reset or "1'b0"
        rsig = re.sub(r"[!~()\s]", "", reset)
        values = {
            "exe": self.detect().get("exe") or self.spec.get("exe"),
            "top": job.top, "depth": job.depth, "timeout": job.timeout, "workdir": job.workdir,
            "files": " ".join(str(f) for f in job.files), "gold": " ".join(str(f) for f in job.gold),
            "gate": " ".join(str(f) for f in job.gate), "clock": job.clock or "clk", "reset": reset,
            "reset_signal": rsig, "reset_sense": "low" if reset.strip().startswith(("!", "~")) else "high",
            "defines_plus": " ".join(f"+define+{d}" for d in job.defines), "defines": " ".join(job.defines),
            "mode": job.mode, "mode_cmd": (self.spec.get("mode_cmd") or {}).get(job.mode, ""),
        }
        script_src = self.spec.get("script", "")
        if script_src and "\n" not in script_src and (self.root / script_src).exists():
            script_src = (self.root / script_src).read_text()
        script = job.workdir / f"{self.name}_{job.mode}.tcl"
        script.write_text(_fill(script_src, values))
        values["script"] = script
        cmd = [_fill(c, values) for c in self.spec.get("cmd", ["{{exe}}", "{{script}}"])]
        code, out, _ = _run(cmd, job.workdir, log, int(self.spec.get("timeout", job.timeout)) + 60)
        res = {"backend": self.name, "mode": job.mode, "status": "error", "asserts": {}, "covers": {}, "log": str(log), "exit_code": code}
        if code != 0 and LICENSE_RE.search(out):
            res["detail"] = f"{self.title} could not check out a license"
            return _done(res, t0)
        pats = {**(STATUS if self.kind == "formal" else EQUIV_STATUS), **(self.spec.get("patterns") or {})}
        if self.kind == "equiv":
            if re.search(pats["not_equivalent"], out, re.I | re.M):
                res["status"] = "not_proven"
            elif re.search(pats["equivalent"], out, re.I | re.M) and code == 0:
                res["status"] = "equivalent"
            else:
                res["detail"] = "the log did not say equivalent or not equivalent: " + _tail(out)
            res["unproven"] = []
            return _done(res, t0)
        found_a: dict[str, str] = {}
        found_c: dict[str, str] = {}
        order = [("assert_cex", "cex"), ("assert_bounded", "bounded"), ("assert_proven", "proven")]
        for line in out.splitlines():
            for key, state in order:
                rx = pats.get(key)
                if rx and re.search(rx, line, re.I):
                    lb = _label_in(line, job.asserts)
                    if lb and lb not in found_a:
                        found_a[lb] = state
                    break
            for key, state in (("cover_unreachable", "unreachable"), ("cover_reached", "reached")):
                rx = pats.get(key)
                if rx and re.search(rx, line, re.I):
                    lb = _label_in(line, job.covers)
                    if lb and lb not in found_c:
                        found_c[lb] = state
                    break
        if job.mode in ("prove", "bmc"):
            res["asserts"] = {a: found_a.get(a, "unknown") for a in job.asserts}
            states = set(res["asserts"].values())
            res["status"] = "fail" if "cex" in states else ("unknown" if "unknown" in states or not job.asserts and code != 0 else "pass")
        else:
            res["covers"] = {c: found_c.get(c, "unknown") for c in job.covers}
            states = set(res["covers"].values())
            res["status"] = "fail" if "unreachable" in states else ("unknown" if "unknown" in states else "pass")
        if res["status"] == "unknown":
            res["detail"] = f"{self.title} log did not report every property; anything not reported is unknown, never pass"
        return _done(res, t0)


def _tail(out: str, n: int = 600) -> str:
    out = out.strip()
    return out[-n:] if out else "(no output)"


def _done(res: dict, t0: float) -> dict:
    res["seconds"] = round(time.time() - t0, 2)
    return res


# ---------------------------------------------------------------- registry

FORMAL_ORDER = ["sby", "yosys", "jasper", "vcformal", "questa"]
EQUIV_ORDER = ["eqy", "yosys", "conformal", "formality"]


def registry(kind: str, section: dict, root: Path) -> dict[str, Backend]:
    """All backends of a kind: built-ins, presets (with dv.toml overrides) and custom ones."""
    user = dict(section.get("backends") or {})
    out: dict[str, Backend] = {}
    if kind == "formal":
        out["sby"] = Sby(section)
        out["yosys"] = YosysSmtbmc(section)
    else:
        out["eqy"] = Eqy(section)
        out["yosys"] = YosysEquiv(section)
    for name, spec in PRESETS.items():
        if spec["kind"] != kind:
            continue
        merged = {**spec, **user.pop(name, {})}
        if "patterns" in merged and spec.get("patterns") is not merged["patterns"]:
            merged["patterns"] = {**spec.get("patterns", {}), **merged["patterns"]}
        out[name] = Command(name, merged, "commercial", root)
    for name, spec in user.items():
        if name in out:   # override knobs for a built-in (e.g. smtbmc_flags) live in the section itself
            continue
        out[name] = Command(name, {"kind": kind, **spec}, "custom", root)
    return out


def select(kind: str, section: dict, root: Path, need_native: bool = False) -> tuple[Backend | None, list[dict]]:
    """Pick the backend to use. Returns (backend or None, detect() of every backend)."""
    reg = registry(kind, section, root)
    infos = [b.info() for b in reg.values()]
    avail = {i["name"] for i in infos if i["available"]}
    wanted = section.get("backend", "auto")
    if wanted and wanted != "auto":
        b = reg.get(wanted)
        return (b if b and wanted in avail else None), infos
    custom = [n for n, b in reg.items() if b.family == "custom"]
    order = custom + (FORMAL_ORDER if kind == "formal" else EQUIV_ORDER)
    if need_native:
        native_first = [n for n in order if n in avail and reg[n].native_sva]
        if native_first:
            return reg[native_first[0]], infos
    for n in order:
        if n in avail:
            return reg[n], infos
    return None, infos
