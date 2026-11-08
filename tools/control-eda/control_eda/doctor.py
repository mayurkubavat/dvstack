"""doctor: what tools are here, and is the project configured."""
from __future__ import annotations

from pathlib import Path

from . import config as cfgmod
from .util import tool_version

TOOLS = {
    "verilator": ("verilator", "--version"),
    "iverilog": ("iverilog", "-V"),
    "verible": ("verible-verilog-lint", "--version"),
    "yosys": ("yosys", "-V"),
    "sby": ("sby", "--help"),
    "yosys-smtbmc": ("yosys-smtbmc", "-h"),
    "z3": ("z3", "--version"),
    "fst2vcd": ("fst2vcd", "-h"),
    "git": ("git", "--version"),
}


def run_doctor(start: Path | None = None) -> dict:
    tools = {}
    for key, (exe, flag) in TOOLS.items():
        v = tool_version(exe, flag)
        tools[key] = {"ok": v is not None, "version": v}
    result: dict = {"tools": tools, "project": {}, "problems": [], "hints": []}
    try:
        cfg = cfgmod.load(start)
        result["project"] = {
            "root": str(cfg.root),
            "name": cfg.project.get("name"),
            "sim_tool": cfg.sim.get("tool"),
            "lint_tool": cfg.lint.get("tool"),
            "tests": cfg.tests,
            "vplan": (cfg.root / "vplan.yaml").exists(),
            "door_policy": (cfg.root / "door-policy.yaml").exists(),
        }
        for src in cfg.sources:
            if not (cfg.root / src).exists():
                result["problems"].append(f"source missing: {src}")
        if not cfg.tests:
            result["problems"].append("dv.toml has no [tests.<name>] tables")
        if not (cfg.root / "vplan.yaml").exists():
            result["problems"].append("vplan.yaml missing")
            result["hints"].append("create vplan.yaml (see examples/fifo_block/vplan.yaml), then `control-eda vplan check`")
        sim_tool = cfg.sim.get("tool", "verilator")
        if sim_tool in ("verilator", "icarus") and not tools["verilator" if sim_tool == "verilator" else "iverilog"]["ok"]:
            result["problems"].append(f"[sim].tool is {sim_tool} but it is not installed")
            result["hints"].append("apt-get install verilator  (or brew install verilator)")
        from .formal import check as formalmod
        fd = formalmod.detect(cfg)
        result["formal"] = {"configured": fd["configured"], "selected": fd["selected"], "solvers": fd["solvers"]}
        if cfg.raw.get("formal") and not fd["selected"]["formal"]:
            msg = "[formal] is configured but no formal tool is installed; `control-eda formal detect` lists the options"
            if (cfg.raw.get("formal") or {}).get("required"):
                result["problems"].append(msg)
            else:
                result["hints"].append(msg + " (formal results will be INCONCLUSIVE; simulation still gates)")
    except cfgmod.ConfigError as e:
        result["problems"].append(str(e))
    if not tools["verilator"]["ok"] and not tools["iverilog"]["ok"]:
        result["problems"].append("no simulator found (verilator or iverilog)")
    result["ok"] = not result["problems"]
    return result


def text_report(d: dict) -> str:
    lines = ["control-eda doctor", ""]
    for k, v in d["tools"].items():
        lines.append(f"  {'ok     ' if v['ok'] else 'missing'}  {k:10} {v['version'] or ''}")
    p = d.get("project") or {}
    if p:
        lines += ["", f"  project {p.get('name')} at {p.get('root')}", f"  tests: {', '.join(p.get('tests', [])) or '(none)'}"]
    f = d.get("formal")
    if f:
        lines.append(f"  formal: {f['selected']['formal'] or 'no backend'}   equiv: {f['selected']['equiv'] or 'no backend'}   solvers: {', '.join(f['solvers']) or 'none'}")
    if d["problems"]:
        lines += ["", "problems:"] + [f"  - {x}" for x in d["problems"]]
    if d["hints"]:
        lines += ["hints:"] + [f"  - {x}" for x in d["hints"]]
    lines += ["", "OK" if d["ok"] else "NOT OK"]
    return "\n".join(lines)
