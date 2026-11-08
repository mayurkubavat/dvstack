#!/usr/bin/env python3
"""PreToolUse: classify an HDL edit as a one-way or two-way door.

One-way doors are allowed, but the session is marked so nothing downstream can
treat the change as auto-mergeable. The classification comes from
`door-policy.yaml` at the project root (falling back to the plugin default), so
teams tune it without touching this script.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import dvhook  # noqa: E402

DEFAULT_POLICY = {
    "one_way": {
        "paths": ["**/csr/**", "**/regs/**", "**/*_regmap*", "**/clk*/**", "**/rst*/**", "**/waivers/**", "**/*.waiver", "**/eco/**"],
        "patterns": [
            r"^\s*(input|output|inout)\b",          # port list change
            r"\bmodule\s+\w+\s*#?\s*\(",            # module header
            r"\b(always_ff|always)\s*@\s*\(\s*posedge\s+\w*clk", # new clock domain
            r"\brst_n?\b.*<=",                       # reset tree edit
            r"\bBASE_ADDR\b|\bADDR_\w+\s*=",         # CSR map
        ],
    }
}


def load_policy(root: Path) -> dict:
    for name in ("door-policy.yaml", "door-policy.yml", "door-policy.json"):
        p = root / name
        if p.exists():
            text = p.read_text()
            if p.suffix == ".json":
                return json.loads(text)
            try:
                import yaml  # type: ignore
                return yaml.safe_load(text) or DEFAULT_POLICY
            except ImportError:
                return DEFAULT_POLICY
    return DEFAULT_POLICY


def classify(path: str, old: str, new: str, policy: dict) -> tuple[str, list[str]]:
    one = policy.get("one_way", {})
    hits = []
    from fnmatch import fnmatch
    for g in one.get("paths", []):
        if fnmatch(path, g) or fnmatch("/" + path, g):
            hits.append(f"path matches {g}")
    for pat in one.get("patterns", []):
        rx = re.compile(pat, re.M)
        if len(rx.findall(new)) != len(rx.findall(old)):
            hits.append(f"changes lines matching /{pat}/")
    return ("one-way" if hits else "two-way"), hits


def main() -> None:
    ev = dvhook.read_event()
    path = dvhook.tool_file(ev) or ""
    if not dvhook.is_hdl(path):
        dvhook.allow()
    root = dvhook.project_root(ev)
    new = dvhook.tool_new_text(ev)
    old = dvhook.tool_old_text(ev)
    if ev.get("tool_name") == "Write":
        try:
            old = Path(path).read_text(errors="replace")
        except OSError:
            old = ""
    door, hits = classify(str(Path(path).resolve().relative_to(root) if Path(path).is_absolute() and str(Path(path).resolve()).startswith(str(root)) else path), old, new, load_policy(root))
    if door == "one-way":
        marker = root / "evidence" / ".one-way-door"
        marker.parent.mkdir(exist_ok=True)
        with marker.open("a") as f:
            f.write(f"{path}: {'; '.join(hits)}\n")
        dvhook.allow(
            f"[dv:door-check] {path} is a ONE-WAY door ({'; '.join(hits)}). "
            "Proceed, but this change can only reach merge-ready; a human merges it. "
            "Say so in your final report."
        )
    dvhook.allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        dvhook.warn_and_allow(f"door_check: {e}")
