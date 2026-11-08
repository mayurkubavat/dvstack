"""vplan: check and look up the agent-readable verification plan."""
from __future__ import annotations

import glob
import re
from pathlib import Path

import yaml

from .config import Config

REQUIRED = ("id", "summary", "rtl", "tests", "door", "status")
DOORS = ("two-way", "one-way")
STATUSES = ("planned", "written", "verified")


def load_vplan(cfg: Config) -> dict:
    p = cfg.root / "vplan.yaml"
    if not p.exists():
        raise FileNotFoundError("vplan.yaml not found; see examples/fifo_block/vplan.yaml")
    data = yaml.safe_load(p.read_text()) or {}
    return data


def check(cfg: Config) -> dict:
    try:
        data = load_vplan(cfg)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    problems: list[str] = []
    feats = data.get("features") or []
    if data.get("version") != 1:
        problems.append("version must be 1")
    ids = set()
    rows_without_tests, rows_without_covers = [], []
    for i, f in enumerate(feats):
        for k in REQUIRED:
            if k not in f:
                problems.append(f"features[{i}] missing '{k}'")
        fid = f.get("id", f"#{i}")
        if fid in ids:
            problems.append(f"duplicate id {fid}")
        ids.add(fid)
        if fid != f"#{i}" and not re.match(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$", fid):
            problems.append(f"{fid}: id must be lowercase dotted")
        if f.get("door") not in DOORS:
            problems.append(f"{fid}: door must be one of {DOORS}")
        if f.get("status") not in STATUSES:
            problems.append(f"{fid}: status must be one of {STATUSES}")
        if not f.get("tests"):
            rows_without_tests.append(fid)
        for t in f.get("tests") or []:
            if t not in cfg.tests:
                problems.append(f"{fid}: test '{t}' is not in dv.toml [tests]")
        for r in f.get("rtl") or []:
            if not (cfg.root / r).exists():
                problems.append(f"{fid}: rtl file missing {r}")
        if f.get("assertions") and not f.get("covers"):
            rows_without_covers.append(fid)
    covered_rtl = {r for f in feats for r in (f.get("rtl") or [])}
    all_rtl = set()
    for g in cfg.project.get("rtl_globs", []):
        all_rtl.update(str(Path(p).relative_to(cfg.root)) for p in glob.glob(str(cfg.root / g), recursive=True))
    orphans = sorted(all_rtl - covered_rtl)
    # assertion labels exist in RTL?
    label_re = re.compile(r"^\s*(\w+)\s*:\s*(assert|cover|assume)\s+property", re.M)
    labels: dict[str, str] = {}
    for r in all_rtl | covered_rtl:
        p = cfg.root / r
        if p.exists():
            for m in label_re.finditer(p.read_text(errors="replace")):
                labels[m.group(1)] = m.group(2)
    for f in feats:
        for a in f.get("assertions") or []:
            if a not in labels:
                problems.append(f"{f.get('id')}: assertion label '{a}' not found in RTL")
        for c in f.get("covers") or []:
            if c not in labels:
                problems.append(f"{f.get('id')}: cover label '{c}' not found in RTL")
        assumes = f.get("assumes") or {}
        if not isinstance(assumes, dict):
            problems.append(f"{f.get('id')}: assumes must map an assume label to its upstream assert, e.g. {{m_valid_stable: producer.a_valid_stable}}")
            continue
        for a in assumes:
            if labels.get(a) != "assume":
                problems.append(f"{f.get('id')}: assume label '{a}' not found in RTL")
    return {
        "ok": not problems,
        "block": data.get("block"),
        "features": len(feats),
        "problems": problems,
        "orphan_rtl": orphans,
        "rows_without_tests": rows_without_tests,
        "assertions_without_covers": rows_without_covers,
        "status_counts": {s: sum(1 for f in feats if f.get("status") == s) for s in STATUSES},
    }


def lookup(cfg: Config, files: list[str] | None = None, query: str | None = None, block: str | None = None) -> dict:
    try:
        data = load_vplan(cfg)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    feats = data.get("features") or []
    hits = []
    norm_files = {str(Path(f)).lstrip("./") for f in (files or [])}
    for f in feats:
        match = False
        if norm_files and any(str(Path(r)).lstrip("./") in norm_files or Path(r).name in {Path(x).name for x in norm_files} for r in f.get("rtl") or []):
            match = True
        if query:
            words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 2]
            text = (f.get("id", "") + " " + f.get("summary", "")).lower()
            if words and sum(w in text for w in words) >= max(1, len(words) // 2):
                match = True
        if block and data.get("block") == block:
            match = True
        if not (files or query or block):
            match = True
        if match:
            hits.append(f)
    tests = sorted({t for f in hits for t in f.get("tests") or []})
    faults = sorted({x for f in hits for x in f.get("faults") or []})
    return {"ok": True, "block": data.get("block"), "matches": hits, "tests": tests, "faults": faults, "count": len(hits)}
