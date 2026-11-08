#!/usr/bin/env python3
"""PreToolUse: block edits that weaken the verification gate.

Blocked in HDL and DV config files:
  - removing or commenting out an `assert property`
  - adding `disable iff` to an existing assertion
  - adding coverage exclusions (`ignore_bins`, `illegal_bins` removal, exclude files)
  - adding inline lint waiver pragmas (verilator lint_off, spyglass waive, etc.)
  - removing a `cover property` (a cover is the witness that an assert is not vacuous)
  - adding an `assume property` that vplan.yaml does not tie to an upstream assert
  - removing seeds or lowering coverage targets in dv.toml
  - lowering [formal] depth, setting accept_bounded = true or required = false in dv.toml
Humans do these on purpose; agents do them to get green. The agent is told to ask.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import dvhook  # noqa: E402

ASSERT_RE = re.compile(r"^\s*(\w+\s*:\s*)?assert\s+property", re.M)
COMMENTED_ASSERT_RE = re.compile(r"^\s*//.*assert\s+property", re.M)
COVER_RE = re.compile(r"^\s*(\w+\s*:\s*)?cover\s+property", re.M)
ASSUME_LABEL_RE = re.compile(r"(?:\b(\w+)\s*:\s*)?\bassume\s+property")
FORMAL_SECTION_RE = re.compile(r"^\s*\[formal\]\s*$(.*?)(?=^\s*\[|\Z)", re.M | re.S)
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
        if count(COVER_RE, new) < count(COVER_RE, old):
            reasons.append("removes a `cover property` (the witness that an assertion is not vacuous)")
    if count(WAIVER_RE, new) > count(WAIVER_RE, old):
        reasons.append("adds an inline lint or coverage waiver pragma (waivers belong in the central waiver file a human signs)")
    return reasons


def check_assumes(old: str, new: str, vplan_text: str | None) -> list[str]:
    """A new assume constrains the proof. It must be listed in vplan `assumes:` with its upstream assert."""
    added = [m.group(1) for m in ASSUME_LABEL_RE.finditer(new)]
    before = [m.group(1) for m in ASSUME_LABEL_RE.finditer(old)]
    if len(added) <= len(before):
        return []
    fresh = [a for a in added if a not in before] or [None]
    vp = vplan_text or ""
    unlisted = [a for a in fresh if not a or not re.search(rf"^\s*{re.escape(a)}\s*:", vp, re.M)]
    if not unlisted:
        return []
    names = ", ".join(a or "(unlabeled)" for a in unlisted)
    return [f"adds `assume property` {names} that vplan.yaml does not list under `assumes:` with the upstream assert that guarantees it "
            "(an unchecked assume can hide every bug it excludes)"]


def _formal(text: str) -> str:
    m = FORMAL_SECTION_RE.search(text)
    return m.group(1) if m else ""


def check_formal_toml(old: str, new: str) -> list[str]:
    reasons = []
    o, n = _formal(old), _formal(new)
    if o and not n:
        return ["removes the [formal] section"]
    od, nd = re.search(r"^\s*depth\s*=\s*(\d+)", o, re.M), re.search(r"^\s*depth\s*=\s*(\d+)", n, re.M)
    if od and (not nd or int(nd.group(1)) < int(od.group(1))):
        reasons.append(f"lowers [formal] depth {od.group(1)} -> {nd.group(1) if nd else 'default'}")
    for key, bad in (("accept_bounded", "true"), ("required", "false")):
        ov = re.search(rf"^\s*{key}\s*=\s*(\w+)", o, re.M)
        nv = re.search(rf"^\s*{key}\s*=\s*(\w+)", n, re.M)
        if nv and nv.group(1).lower() == bad and (not ov or ov.group(1).lower() != bad):
            reasons.append(f"sets [formal] {key} = {bad}")
    return reasons


def check_dv_toml(old: str, new: str) -> list[str]:
    reasons = check_formal_toml(old, new)
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
        vplan_text = None
        for d in [Path(path).resolve().parent, *Path(path).resolve().parents]:
            if (d / "vplan.yaml").exists():
                vplan_text = (d / "vplan.yaml").read_text(errors="replace")
                break
            if (d / "dv.toml").exists():
                break
        reasons += check_assumes(old, new, vplan_text)
    elif name == "dv.toml":
        reasons = check_dv_toml(old, new)
        # [formal] settings need the whole file to know which section an Edit touches
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                disk = f.read()
        except OSError:
            disk = ""
        if disk and old and old in disk and ev.get("tool_name") != "Write":
            full_new = disk.replace(old, new, 1)
            reasons = [r for r in reasons if "[formal]" not in r] + check_formal_toml(disk, full_new)
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
