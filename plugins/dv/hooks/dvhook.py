"""Shared helpers for dv plugin hooks.

Every hook reads one JSON object from stdin, decides, and exits:
  0  allow (stdout may carry context for the agent)
  2  block (stderr carries the reason the agent sees)
Hooks fail open: on any internal error they print a warning and exit 0, so a
broken tool install never freezes a session. CI runs the same checks and will
still fail the PR.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

RTL_GLOBS = ("*.sv", "*.svh", "*.v", "*.vh", "*.vhd", "*.vhdl")
TB_HINTS = ("tb", "test", "uvm", "sva", "bind")


def read_event() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def project_root(event: dict) -> Path:
    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(cwd)


def tool_file(event: dict) -> str | None:
    ti = event.get("tool_input") or {}
    return ti.get("file_path") or ti.get("path") or ti.get("notebook_path")


def tool_new_text(event: dict) -> str:
    """Text being written by Edit/Write/MultiEdit."""
    ti = event.get("tool_input") or {}
    if "content" in ti:
        return ti.get("content") or ""
    if "new_string" in ti:
        return ti.get("new_string") or ""
    if "edits" in ti:
        return "\n".join(e.get("new_string", "") for e in ti["edits"])
    return ""


def tool_old_text(event: dict) -> str:
    ti = event.get("tool_input") or {}
    if "old_string" in ti:
        return ti.get("old_string") or ""
    if "edits" in ti:
        return "\n".join(e.get("old_string", "") for e in ti["edits"])
    return ""


def is_hdl(path: str | None) -> bool:
    return bool(path) and any(fnmatch.fnmatch(Path(path).name, g) for g in RTL_GLOBS)


def is_testbench(path: str | None) -> bool:
    if not path:
        return False
    parts = [p.lower() for p in Path(path).parts]
    return any(h in part for part in parts for h in TB_HINTS)


def block(reason: str) -> None:
    sys.stderr.write(reason.rstrip() + "\n")
    sys.exit(2)


def allow(context: str | None = None) -> None:
    if context:
        sys.stdout.write(context.rstrip() + "\n")
    sys.exit(0)


def warn_and_allow(msg: str) -> None:
    sys.stdout.write(f"[dv hook] warning: {msg}\n")
    sys.exit(0)


def run(cmd: list[str], cwd: Path, timeout: int = 50) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def have_control_eda() -> bool:
    code, _, _ = run(["control-eda", "--version"], Path.cwd(), timeout=10)
    return code == 0


def changed_hdl_files(root: Path) -> list[str]:
    """RTL/TB files changed vs HEAD plus untracked, as the evidence gate sees them."""
    code, out, _ = run(["git", "status", "--porcelain", "--untracked-files=all"], root)
    if code != 0:
        return []
    files = []
    for line in out.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if is_hdl(path):
            files.append(path)
    return files


def git_head(root: Path) -> str | None:
    code, out, _ = run(["git", "rev-parse", "--short", "HEAD"], root)
    return out.strip() if code == 0 and out.strip() else None
