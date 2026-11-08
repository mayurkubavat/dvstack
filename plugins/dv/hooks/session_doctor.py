#!/usr/bin/env python3
"""SessionStart: load tool status, dv.toml and a vplan summary into context."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import dvhook  # noqa: E402


def main() -> None:
    ev = dvhook.read_event()
    root = dvhook.project_root(ev)
    if not (root / "dv.toml").exists():
        dvhook.allow()
    lines = ["[dv plugin] This project is dv-enabled (dv.toml found)."]
    if dvhook.have_control_eda():
        code, out, _ = dvhook.run(["control-eda", "doctor", "--json"], root, timeout=25)
        try:
            d = json.loads(out)
            tools = ", ".join(f"{k}={'ok' if v.get('ok') else 'missing'}" for k, v in d.get("tools", {}).items())
            lines.append(f"Tools: {tools}")
            if not d.get("ok"):
                lines.append("Doctor reports problems; run /dv-doctor before verifying anything.")
        except (json.JSONDecodeError, AttributeError):
            lines.append("control-eda doctor produced no JSON.")
        code, out, _ = dvhook.run(["control-eda", "vplan", "check", "--json"], root, timeout=15)
        try:
            v = json.loads(out)
            lines.append(
                f"vplan: {v.get('features', 0)} features, {len(v.get('orphan_rtl', []))} orphan RTL files, "
                f"{len(v.get('rows_without_tests', []))} rows without tests."
            )
        except json.JSONDecodeError:
            pass
    else:
        lines.append("control-eda is not installed: `pip install -e tools/control-eda` (from the dvstack repo).")
    lines.append(
        "Rules: run dv:verify before calling any RTL/TB change done; never weaken assertions, "
        "exclude bins or add waivers; label claims measured/inferred/guessed."
    )
    dvhook.allow("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        dvhook.warn_and_allow(f"session_doctor: {e}")
