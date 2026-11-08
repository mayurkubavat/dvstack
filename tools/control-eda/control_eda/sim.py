"""sim: compile and run one test with one seed. Verilator (default) or Icarus."""
from __future__ import annotations

import hashlib
import os
import random
import re
import shutil
from pathlib import Path

from .config import Config, ConfigError
from .util import run, which

FAIL_TIME_RE = re.compile(r"@\s*(\d+)(\s*(?:ps|ns|us))?")


def _build_key(cfg: Config, defines: list[str]) -> str:
    h = hashlib.sha1()
    for s in cfg.sources:
        p = cfg.root / s
        h.update(s.encode())
        if p.exists():
            h.update(p.read_bytes())
    h.update(" ".join(sorted(defines)).encode())
    h.update(" ".join(cfg.sim.get("verilator_flags", [])).encode())
    return h.hexdigest()[:12]


def compile_verilator(cfg: Config, top: str, defines: list[str], cov: bool, trace: bool, log: Path) -> tuple[Path | None, dict]:
    exe = which("verilator")
    if not exe:
        return None, {"ok": False, "error": "verilator not installed", "hint": "apt-get install verilator"}
    key = _build_key(cfg, defines) + ("_c" if cov else "") + ("_t" if trace else "") + f"_{top}"
    bdir = cfg.build_dir / f"verilator_{key}"
    binary = bdir / "sim"
    if binary.exists():
        return binary, {"ok": True, "cached": True, "build_dir": str(bdir.relative_to(cfg.root))}
    template = Path(__file__).parent / "templates" / "verilator_main.cpp"
    cmd = [
        exe, "--cc", "--exe", "--build", "--prefix", "Vtop", "--assert", "--timing",
        "--Mdir", str(bdir), "-o", "sim", "--top-module", top,
        *cfg.sim.get("verilator_flags", []),
    ]
    if cov:
        cmd.append("--coverage")
    if trace:
        cmd += ["--trace", "--trace-structs"]
    cmd += [f"-D{d}" for d in defines]
    cmd += [str(cfg.root / s) for s in cfg.sources] + [str(template)]
    code, out, secs = run(cmd, cfg.root, log, timeout=cfg.sim.get("compile_timeout", 1200))
    if code != 0 or not binary.exists():
        return None, {"ok": False, "error": "verilator build failed", "log": str(log.relative_to(cfg.root)), "tail": out[-1500:]}
    return binary, {"ok": True, "cached": False, "build_dir": str(bdir.relative_to(cfg.root)), "seconds": round(secs, 1)}


def compile_icarus(cfg: Config, top: str, defines: list[str], log: Path) -> tuple[Path | None, dict]:
    exe = which("iverilog")
    if not exe:
        return None, {"ok": False, "error": "iverilog not installed", "hint": "apt-get install iverilog"}
    key = _build_key(cfg, defines) + f"_{top}"
    bdir = cfg.build_dir / f"icarus_{key}"
    bdir.mkdir(parents=True, exist_ok=True)
    out_vvp = bdir / "sim.vvp"
    if out_vvp.exists():
        return out_vvp, {"ok": True, "cached": True}
    cmd = [exe, "-g2012", "-s", top, "-o", str(out_vvp), *[f"-D{d}" for d in defines], *[str(cfg.root / s) for s in cfg.sources]]
    code, out, secs = run(cmd, cfg.root, log)
    if code != 0:
        return None, {"ok": False, "error": "iverilog failed", "log": str(log.relative_to(cfg.root)), "tail": out[-1500:]}
    return out_vvp, {"ok": True, "cached": False, "seconds": round(secs, 1)}


def run_test(cfg: Config, test: str, seed: int | str, defines: list[str] | None = None, cov: bool = False, trace: bool = False, expect_fail: bool = False, dry_run: bool = False, compile_only: bool = False) -> dict:
    defines = list(defines or [])
    tcfg = cfg.test(test)
    top = tcfg.get("top") or cfg.project.get("top")
    if not top:
        raise ConfigError(f"test '{test}' has no top and [project] has no top")
    if seed == "random":
        seed = random.randint(1, 2**31 - 1)
    seed = int(seed)
    tool = cfg.sim.get("tool", "verilator")
    sim_dir = cfg.evidence_dir / "sim"
    sim_dir.mkdir(exist_ok=True)
    tag = f"{test}_{seed}" + ("_" + "_".join(defines) if defines else "")
    log = sim_dir / f"{tag}.log"
    build_log = sim_dir / f"build_{top}{'_' + '_'.join(defines) if defines else ''}.log"
    wave = sim_dir / f"{tag}.vcd"
    plusargs = [f"+seed={seed}", *tcfg.get("plusargs", []), *cfg.sim.get("plusargs", [])]
    if trace:
        plusargs.append("+trace")
    result: dict = {"test": test, "seed": seed, "defines": defines, "tool": tool, "expect_fail": expect_fail, "log": str(log.relative_to(cfg.root))}

    if tool == "verilator":
        if dry_run:
            result.update({"dry_run": True, "would_run": ["verilator --cc --exe --build ...", f"sim {' '.join(plusargs)}"]})
            return result
        binary, binfo = compile_verilator(cfg, top, defines, cov, trace, build_log)
        result["build"] = binfo
        if binary is None:
            result.update({"status": "error", "ok": False})
            return result
        if compile_only:
            result.update({"status": "compiled", "ok": True})
            return result
        env = dict(os.environ)
        cov_file = sim_dir / f"{tag}.cov.dat"
        env["CONTROL_EDA_COV_FILE"] = str(cov_file)
        code, out, secs = run([str(binary), *plusargs], sim_dir, log, timeout=cfg.sim.get("run_timeout", 1800), env=env)
        dumped = sim_dir / "sim.vcd"
        if trace and dumped.exists():
            shutil.move(str(dumped), str(wave))
        if cov and cov_file.exists():
            result["cov_file"] = str(cov_file.relative_to(cfg.root))
    elif tool == "icarus":
        if dry_run:
            result.update({"dry_run": True, "would_run": ["iverilog -g2012 ...", f"vvp sim.vvp {' '.join(plusargs)}"]})
            return result
        vvp, binfo = compile_icarus(cfg, top, defines, build_log)
        result["build"] = binfo
        if vvp is None:
            result.update({"status": "error", "ok": False})
            return result
        if compile_only:
            result.update({"status": "compiled", "ok": True})
            return result
        code, out, secs = run([which("vvp"), str(vvp), *plusargs], sim_dir, log, timeout=cfg.sim.get("run_timeout", 1800))
        dumped = sim_dir / "sim.vcd"
        if trace and dumped.exists():
            shutil.move(str(dumped), str(wave))
    else:
        raise ConfigError(f"[sim].tool '{tool}' has no adapter yet (verilator, icarus)")

    pass_re = re.compile(cfg.sim.get("pass_pattern", "TEST PASSED"))
    fail_re = re.compile(cfg.sim.get("fail_pattern", "TEST FAILED|ERROR|UVM_FATAL|%Error|Assertion failed"))
    passed = bool(pass_re.search(out))
    failed = bool(fail_re.search(out)) or code != 0
    raw_status = "pass" if passed and not failed else "fail"
    first_fail = None
    fail_time = None
    for line in out.splitlines():
        if fail_re.search(line):
            first_fail = line.strip()
            m = FAIL_TIME_RE.search(line)
            if m:
                fail_time = m.group(1) + (m.group(2) or "").strip()
            break
    ok = (raw_status == "fail") if expect_fail else (raw_status == "pass")
    result.update({
        "status": raw_status,
        "ok": ok,
        "exit_code": code,
        "seconds": round(secs, 2),
        "first_failure": first_fail,
        "fail_time": fail_time,
        "wave": str(wave.relative_to(cfg.root)) if wave.exists() else None,
        "summary": next((l.strip() for l in out.splitlines() if pass_re.search(l) or "TEST FAILED" in l), None),
    })
    if expect_fail and raw_status == "pass":
        result["note"] = "expected a failure (fault injection) but the test passed: the test or assertion cannot catch this fault"
    return result
