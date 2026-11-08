"""lint: Verilator --lint-only (default) or Verible, parsed to JSON."""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .util import run, which

VERILATOR_MSG = re.compile(r"^%(Error|Warning)(?:-(\w+))?: (?:([^:\s]+):(\d+):(?:\d+:)? )?(.*)$")
VERIBLE_MSG = re.compile(r"^([^:]+):(\d+):(\d+)(?:-\d+)?: (.*)$")


def lint(cfg: Config, files: list[str] | None = None) -> dict:
    tool = cfg.lint.get("tool", "verilator")
    srcs = files or cfg.lint.get("sources") or cfg.sources
    log = cfg.evidence_dir / "lint.log"
    errors, warnings = [], []
    if tool == "verible":
        exe = which("verible-verilog-lint")
        if not exe:
            return {"ok": False, "tool": tool, "error": "verible-verilog-lint not installed", "hint": "set [lint].tool = \"verilator\" or install verible"}
        code, out, secs = run([exe, *cfg.lint.get("flags", []), *srcs], cfg.root, log)
        for line in out.splitlines():
            m = VERIBLE_MSG.match(line)
            if m:
                errors.append({"file": m.group(1), "line": int(m.group(2)), "code": "verible", "message": m.group(4)})
    else:
        exe = which("verilator")
        if not exe:
            return {"ok": False, "tool": tool, "error": "verilator not installed", "hint": "apt-get install verilator"}
        cmd = [exe, "--lint-only", "--timing", *cfg.lint.get("flags", []), *srcs]
        top = cfg.lint.get("top")
        if top:
            cmd += ["--top-module", top]
        code, out, secs = run(cmd, cfg.root, log)
        for line in out.splitlines():
            m = VERILATOR_MSG.match(line)
            if not m or m.group(5).startswith("Exiting due to"):
                continue
            item = {"file": m.group(3), "line": int(m.group(4)) if m.group(4) else None, "code": m.group(2) or "", "message": m.group(5).strip()}
            (errors if m.group(1) == "Error" else warnings).append(item)
    return {
        "ok": not errors,
        "tool": tool,
        "files": srcs,
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "log": str(log.relative_to(cfg.root)),
        "seconds": round(secs, 2),
    }
