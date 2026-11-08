"""cov: parse, merge and diff Verilator coverage.dat files.

Verilator writes lines like:
  C '<SEP>f<SEP>path<SEP>l<SEP>58<SEP>n<SEP>12<SEP>page<SEP>v_user/fifo<SEP>o<SEP>c_full<SEP>...' 444
where <SEP> is \\x01 for keys and \\x02 for values. A point is covered when its
count is >= 1 (we keep the raw count so cover properties can be reported).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config

LINE_RE = re.compile(r"^C '(.*)' (\d+)$")


def parse_dat(path: Path) -> dict[str, dict]:
    points: dict[str, dict] = {}
    for line in path.read_text(errors="replace").splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        body, count = m.group(1), int(m.group(2))
        attrs: dict[str, str] = {}
        for pair in body.split("\x01"):
            if "\x02" in pair:
                k, v = pair.split("\x02", 1)
                attrs[k] = v
        key = body
        points[key] = {"attrs": attrs, "count": count}
    return points


def merge(paths: list[Path]) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for p in paths:
        for k, v in parse_dat(p).items():
            if k in merged:
                merged[k]["count"] += v["count"]
            else:
                merged[k] = {"attrs": dict(v["attrs"]), "count": v["count"]}
    return merged


def summarize(points: dict[str, dict]) -> dict:
    by_type: dict[str, dict] = {}
    for v in points.values():
        page = v["attrs"].get("page", "unknown")
        typ = page.split("/", 1)[0]
        t = by_type.setdefault(typ, {"total": 0, "covered": 0, "points": []})
        t["total"] += 1
        covered = v["count"] >= 1
        t["covered"] += int(covered)
        if typ == "v_user" or not covered:
            t["points"].append({
                "name": v["attrs"].get("o") or v["attrs"].get("h") or v["attrs"].get("c", ""),
                "file": v["attrs"].get("f"),
                "line": int(v["attrs"]["l"]) if "l" in v["attrs"] else None,
                "count": v["count"],
                "covered": covered,
            })
    total = sum(t["total"] for t in by_type.values())
    covered = sum(t["covered"] for t in by_type.values())
    return {
        "total": total,
        "covered": covered,
        "percent": round(100.0 * covered / total, 2) if total else None,
        "by_type": {k: {"total": t["total"], "covered": t["covered"], "percent": round(100.0 * t["covered"] / t["total"], 2) if t["total"] else None} for k, t in by_type.items()},
        "user_points": by_type.get("v_user", {}).get("points", []),
        "uncovered": [p for t in by_type.values() for p in t["points"] if not p["covered"]],
    }


def cov_files(cfg: Config) -> list[Path]:
    """Coverage from normal runs only; fault-injection (expect_fail) runs are excluded."""
    sim_dir = cfg.evidence_dir / "sim"
    if not sim_dir.exists():
        return []
    out = []
    for dat in sorted(sim_dir.glob("*.cov.dat")):
        meta = sim_dir / (dat.name[: -len(".cov.dat")] + ".json")
        if meta.exists():
            try:
                if json.loads(meta.read_text()).get("expect_fail"):
                    continue
            except json.JSONDecodeError:
                pass
        out.append(dat)
    return out


def merge_project(cfg: Config, write: bool = True) -> dict:
    files = cov_files(cfg)
    if not files:
        return {"ok": False, "error": "no coverage data", "hint": "run `control-eda sim --test <t> --seed <s> --cov` first"}
    merged = merge(files)
    summary = summarize(merged)
    summary.update({"ok": True, "inputs": [str(f.relative_to(cfg.root)) for f in files], "floor": cfg.cov.get("floor")})
    if write:
        out = cfg.evidence_dir / "cov.json"
        out.write_text(json.dumps(summary, indent=2))
        summary["report"] = str(out.relative_to(cfg.root))
    return summary


def diff(cfg: Config, base_report: Path) -> dict:
    cur = merge_project(cfg, write=False)
    if not cur.get("ok"):
        return cur
    try:
        base = json.loads(base_report.read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": True, "before": None, "after": cur["percent"], "note": f"no base report at {base_report}"}
    before, after = base.get("percent"), cur["percent"]
    base_unc = {(p.get("file"), p.get("line"), p.get("name")) for p in base.get("uncovered", [])}
    cur_unc = {(p.get("file"), p.get("line"), p.get("name")) for p in cur.get("uncovered", [])}
    return {
        "ok": True,
        "before": before,
        "after": after,
        "delta": round(after - before, 2) if before is not None and after is not None else None,
        "newly_covered": sorted(str(x) for x in base_unc - cur_unc),
        "newly_uncovered": sorted(str(x) for x in cur_unc - base_unc),
        "dropped": before is not None and after is not None and after < before,
    }
