#!/usr/bin/env python3
"""Stop / SubagentStop: refuse to finish when HDL changed and no fresh evidence exists.

Fresh means: evidence/<HEAD short sha>.json exists, validates, and its verdict is
PASS or INCONCLUSIVE (INCONCLUSIVE is allowed to stop, but the agent must have
said so). A FAIL bundle blocks the stop too: the agent must report the failure
rather than silently end.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import dvhook  # noqa: E402


def main() -> None:
    ev = dvhook.read_event()
    if ev.get("stop_hook_active"):
        dvhook.allow()  # already looped once; do not trap the agent forever
    root = dvhook.project_root(ev)
    if not (root / "dv.toml").exists():
        dvhook.allow()  # not a dv-enabled project
    changed = dvhook.changed_hdl_files(root)
    if not changed:
        dvhook.allow()

    ev_dir = root / "evidence"
    sha = dvhook.git_head(root)
    candidates = []
    if sha:
        candidates.append(ev_dir / f"{sha}.json")
    candidates.append(ev_dir / "worktree.json")
    bundle = next((c for c in candidates if c.exists()), None)

    if bundle is None:
        dvhook.block(
            "dv:evidence-gate: RTL/testbench files changed ("
            + ", ".join(changed[:6])
            + (" ..." if len(changed) > 6 else "")
            + ") but no evidence bundle exists at "
            + " or ".join(str(c.relative_to(root)) for c in candidates)
            + ".\nRun the dv:verify skill (or `control-eda evidence bundle`) before finishing, "
            "or state plainly that the change is unverified and why."
        )

    try:
        data = json.loads(bundle.read_text())
    except json.JSONDecodeError as e:
        dvhook.block(f"dv:evidence-gate: {bundle} is not valid JSON ({e}). Regenerate it with dv:verify.")

    verdict = data.get("verdict")
    if verdict == "FAIL":
        dvhook.block(
            f"dv:evidence-gate: {bundle.name} records verdict FAIL. Do not finish as if the change were done. "
            "Report the failure with the evidence, or fix the root cause and rerun dv:verify."
        )
    if verdict not in ("PASS", "INCONCLUSIVE"):
        dvhook.block(f"dv:evidence-gate: {bundle.name} has no verdict. Run `control-eda evidence check {bundle}`.")

    # Stale check: bundle must cover the changed files.
    covered = set(data.get("files", []))
    missing = [f for f in changed if f not in covered]
    if covered and missing:
        dvhook.block(
            "dv:evidence-gate: the evidence bundle does not cover "
            + ", ".join(missing[:6])
            + ". Rerun dv:verify so the bundle matches the working tree."
        )
    dvhook.allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        dvhook.warn_and_allow(f"evidence_gate: {e}")
