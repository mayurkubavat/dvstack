from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def which(name: str) -> str | None:
    """shutil.which, except tools in CONTROL_EDA_HIDE_TOOLS (comma list) count as absent, so
    CONTROL_EDA_HIDE_TOOLS=verilator shows what every command does on a machine without it."""
    hidden = {x.strip() for x in os.environ.get("CONTROL_EDA_HIDE_TOOLS", "").split(",") if x.strip()}
    if name in hidden or Path(name).name in hidden:
        return None
    return shutil.which(name)


def tool_version(name: str, flag: str = "--version") -> str | None:
    path = which(name)
    if not path:
        return None
    try:
        p = subprocess.run([path, flag], capture_output=True, text=True, timeout=15)
        out = (p.stdout or p.stderr).strip().splitlines()
        return out[0] if out else "present"
    except (subprocess.TimeoutExpired, OSError):
        return "present"


def run(cmd: list[str], cwd: Path, log: Path | None = None, timeout: int = 1800, env: dict | None = None) -> tuple[int, str, float]:
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        out = p.stdout + p.stderr
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + (e.stderr or "") + f"\n[control-eda] timed out after {timeout}s\n"
        code = 124
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("$ " + " ".join(cmd) + "\n\n" + out)
    return code, out, time.time() - t0


def emit(data: dict, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=False))
    else:
        print(text if text is not None else json.dumps(data, indent=2))


def fail(msg: str, hint: str | None = None, as_json: bool = False, code: int = 1) -> None:
    data = {"ok": False, "error": msg}
    if hint:
        data["hint"] = hint
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        sys.stderr.write(f"error: {msg}\n")
        if hint:
            sys.stderr.write(f"hint: {hint}\n")
    sys.exit(code)


def parse_time(s: str) -> int:
    """'18450ns' -> picoseconds. Bare ints are picoseconds."""
    s = s.strip().lower().replace(" ", "")
    units = {"fs": 1e-3, "ps": 1, "ns": 1e3, "us": 1e6, "ms": 1e9, "s": 1e12}
    for u, mult in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if s.endswith(u):
            return int(float(s[: -len(u)]) * mult)
    return int(float(s))


def fmt_time(ps: int) -> str:
    if ps % 1_000_000 == 0:
        return f"{ps // 1_000_000}us"
    if ps % 1000 == 0:
        return f"{ps // 1000}ns"
    return f"{ps}ps"
