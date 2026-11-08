"""door: classify the working-tree change as a one-way or two-way door."""
from __future__ import annotations

import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

import yaml

from .config import Config

DEFAULT = {
    "one_way": {
        "paths": ["**/csr/**", "**/regs/**", "**/*_regmap*", "**/waivers/**", "**/*.waiver", "**/eco/**"],
        "patterns": [r"^\s*(input|output|inout)\b", r"\bmodule\s+\w+\s*#?\s*\(", r"\balways_ff\s*@\s*\(\s*posedge\s+\w*clk", r"\bnegedge\s+rst"],
    }
}


def policy(cfg: Config) -> dict:
    p = cfg.root / "door-policy.yaml"
    if p.exists():
        return yaml.safe_load(p.read_text()) or DEFAULT
    return DEFAULT


def classify(cfg: Config, base: str | None = None) -> dict:
    pol = policy(cfg).get("one_way", {})
    args = ["git", "diff", "--unified=0", base or "HEAD", "--"]
    p = subprocess.run(args, cwd=cfg.root, capture_output=True, text=True)
    diff = p.stdout
    reasons = []
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            for g in pol.get("paths", []):
                if fnmatch(current, g):
                    reasons.append(f"{current}: path matches {g}")
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            body = line[1:]
            for pat in pol.get("patterns", []):
                if re.search(pat, body):
                    reasons.append(f"{current}: {'added' if line[0] == '+' else 'removed'} line matches /{pat}/: {body.strip()[:80]}")
    marker = cfg.evidence_dir / ".one-way-door"
    if marker.exists():
        reasons.append(f"hook marker {marker.relative_to(cfg.root)} present")
    return {"ok": True, "door": "one-way" if reasons else "two-way", "reasons": reasons}
