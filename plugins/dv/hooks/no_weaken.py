#!/usr/bin/env python3
"""PreToolUse: block edits that weaken the verification gate.

Blocked in HDL and DV config files:
  - removing or commenting out an `assert property`
  - adding `disable iff` to an existing assertion
  - adding coverage exclusions (`ignore_bins`, `illegal_bins` removal, exclude files)
  - adding inline lint waiver pragmas (verilator lint_off, spyglass waive, etc.)
  - removing seeds or lowering coverage targets in dv.toml
Humans do these on purpose; agents do them to get green. The agent is told to ask.
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import dvhook  # noqa: E402

ASSERT_RE = re.compile(r"^\s*(\w+\s*:\s*)?assert\s+property", re.M)
COMMENTED_ASSERT_RE = re.compile(r"^\s*//.*assert\s+property", re.M)
DISABLE_IFF_RE = re.compile(r"disable\s+iff", re.I)
IGNORE_BINS_RE = re.compile(r"\bignore_bins\b")
ILLEGAL_BINS_RE = re.compile(r"\billegal_bins\b")
WAIVER_RE = re.compile(
    r"(verilator\s+lint_off|/\*\s*verilator\s+lint_off|spyglass\s+disable|"
    r"@\s*waive|lint_waive|// *waive[:\s]|synopsys\s+translate_off|"
    r"pragma\s+coverage\s+off|coverage\s+off)",
    re.I,
)
SEEDS_RE = re.compile(r"^\s*seeds\s*=\s*\[(.*?)\]", re.M | re.S)
RANDOM_SEEDS_RE = re.compile(r"^\s*random_seeds\s*=\s*(\d+)", re.M)
COV_FLOOR_RE = re.compile(r"^\s*(min_coverage|coverage_floor|floor)\s*=\s*([\d.]+)", re.M)
DOOR_RE = re.compile(r"^\s*door\s*:\s*(\S+)", re.M)


def count(rx: re.Pattern, text: str) -> int:
    return len(rx.findall(text))


def check_hdl(old: str, new: str, is_write: bool) -> list[str]:
    reasons = []
    if not is_write:
        if count(ASSERT_RE, new) < count(ASSERT_RE, old):
            reasons.append("removes an `assert property`")
        if count(COMMENTED_ASSERT_RE, new) > count(COMMENTED_ASSERT_RE, old):
            reasons.append("comments out an `assert property`")
        if count(DISABLE_IFF_RE, new) > count(DISABLE_IFF_RE, old) and count(ASSERT_RE, new) <= count(ASSERT_RE, old):
            reasons.append("adds `disable iff` to an existing assertion")
        if count(IGNORE_BINS_RE, new) > count(IGNORE_BINS_RE, old):
            reasons.append("adds `ignore_bins` (coverage exclusion)")
        if count(ILLEGAL_BINS_RE, new) < count(ILLEGAL_BINS_RE, old):
            reasons.append("removes `illegal_bins`")
    if count(WAIVER_RE, new) > count(WAIVER_RE, old):
        reasons.append("adds an inline lint or coverage waiver pragma (waivers belong in the central waiver file a human signs)")
    return reasons


def check_dv_toml(old: str, new: str) -> list[str]:
    reasons = []
    o, n = SEEDS_RE.search(old), SEEDS_RE.search(new)
    if o and n:
        old_seeds = {s.strip() for s in o.group(1).split(",") if s.strip()}
        new_seeds = {s.strip() for s in n.group(1).split(",") if s.strip()}
        if old_seeds - new_seeds:
            reasons.append(f"drops fixed seeds {sorted(old_seeds - new_seeds)}")
    o, n = RANDOM_SEEDS_RE.search(old), RANDOM_SEEDS_RE.search(new)
    if o and n and int(n.group(1)) < int(o.group(1)):
        reasons.append("lowers random_seeds")
    o, n = COV_FLOOR_RE.search(old), COV_FLOOR_RE.search(new)
    if o and n and float(n.group(2)) < float(o.group(2)):
        reasons.append(f"lowers the coverage floor {o.group(2)} -> {n.group(2)}")
    return reasons


def check_vplan(old: str, new: str) -> list[str]:
    old_doors, new_doors = DOOR_RE.findall(old), DOOR_RE.findall(new)
    if old_doors.count("one-way") > new_doors.count("one-way") and len(old_doors) == len(new_doors):
        return ["changes a vplan row from one-way to two-way (human decision)"]
    return []


def main() -> None:
    ev = dvhook.read_event()
    if ev.get("tool_name") not in ("Edit", "Write", "MultiEdit"):
        dvhook.allow()
    path = dvhook.tool_file(ev) or ""
    name = path.rsplit("/", 1)[-1]
    new = dvhook.tool_new_text(ev)
    old = dvhook.tool_old_text(ev)
    is_write = ev.get("tool_name") == "Write"

    if is_write:
        # On a full-file Write we can only compare against the file on disk.
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                old = f.read()
            is_write = False
        except OSError:
            old = ""

    reasons: list[str] = []
    if dvhook.is_hdl(path):
        reasons = check_hdl(old, new, is_write)
    elif name == "dv.toml":
        reasons = check_dv_toml(old, new)
    elif name in ("vplan.yaml", "vplan.yml"):
        reasons = check_vplan(old, new)
    elif name.endswith((".waiver", ".waivers", ".el", ".exclude", "_exclusions.txt")):
        if len(new) > len(old):
            reasons = ["adds to a waiver or coverage-exclusion file"]

    if reasons:
        dvhook.block(
            "dv:no-weaken blocked this edit to "
            + path
            + " because it "
            + "; ".join(reasons)
            + ".\nAgents may not weaken the verification gate to get green. "
            "If the gate is wrong, stop and explain why to the human; they make this edit. "
            "If a test fails, run dv:why-fail and fix the root cause instead."
        )
    dvhook.allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail open
        dvhook.warn_and_allow(f"no_weaken: {e}")
