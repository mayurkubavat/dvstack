"""evidence: assemble and validate the bundle dv:verify writes and dv-verifier reads."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

SCHEMA = Path(__file__).resolve().parents[3] / "schemas" / "evidence.schema.json"
PROOF_STATES = ("pass", "fail", "not_run", "proven", "bounded", "cex", "unknown", "unsupported", "tool_missing")
LABEL_RE = re.compile(r"^\s*\[(measured|inferred|guessed)\]\s*(.+)$", re.I)


def git(cfg: Config, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=cfg.root, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def changed_files(cfg: Config, base: str | None) -> list[str]:
    files = set()
    if base:
        files.update(git(cfg, "diff", "--name-only", base).splitlines())
    files.update(git(cfg, "diff", "--name-only", "HEAD").splitlines())
    files.update(git(cfg, "ls-files", "--others", "--exclude-standard").splitlines())
    # repo-relative -> project-relative when the project is a subdir of the repo
    top = git(cfg, "rev-parse", "--show-toplevel")
    out = []
    for f in sorted(files):
        if not f.endswith((".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl", ".toml", ".yaml")):
            continue
        if top and str(cfg.root) != top:
            rel = cfg.root.relative_to(top)
            if f.startswith(str(rel) + "/"):
                f = f[len(str(rel)) + 1:]
            else:
                continue
        out.append(f)
    return out


def bundle(cfg: Config, base: str | None, block: str | None, door: str, claims: list[str], gaps: list[str], sva_fault_map: dict[str, str] | None = None) -> dict:
    sim_dir = cfg.evidence_dir / "sim"
    runs, failures = [], []
    for log in sorted(sim_dir.glob("*.log")) if sim_dir.exists() else []:
        if log.name.startswith("build_"):
            continue
        meta = sim_dir / (log.stem + ".json")
        if meta.exists():
            r = json.loads(meta.read_text())
            run = {k: r.get(k) for k in ("test", "seed", "status", "expect_fail", "defines", "log", "wave", "fail_time")}
            run["status"] = run["status"] if run["status"] in ("pass", "fail", "error") else "error"
            runs.append(run)
            if r.get("status") == "fail" and not r.get("expect_fail"):
                failures.append({"test": r.get("test"), "seed": r.get("seed"), "first_failure": r.get("first_failure"), "wave": r.get("wave")})
    failed = sum(1 for r in runs if (r["status"] != "pass") != bool(r.get("expect_fail")))

    lint_json = cfg.evidence_dir / "lint.json"
    lint = json.loads(lint_json.read_text()) if lint_json.exists() else {"tool": "none", "error_count": None, "warning_count": None}
    cov_json = cfg.evidence_dir / "cov.json"
    cov = json.loads(cov_json.read_text()) if cov_json.exists() else {}
    base_cov = cfg.evidence_dir / "cov.base.json"
    before = json.loads(base_cov.read_text()).get("percent") if base_cov.exists() else None

    # SVA: every assert label gets a cover and a mutation state. The vplan says
    # which covers and faults belong to which assertion; without a vplan row we
    # fall back to "any cover with hits" / "any fault run" for the block.
    label_re = re.compile(r"^\s*(\w+)\s*:\s*(assert|cover)\s+property", re.M)
    asserts, covers = {}, set()
    for g in cfg.project.get("rtl_globs", []):
        for p in cfg.root.glob(g):
            for m in label_re.finditer(p.read_text(errors="replace")):
                if m.group(2) == "assert":
                    asserts[m.group(1)] = str(p.relative_to(cfg.root))
                else:
                    covers.add(m.group(1))
    cov_hits = {pt["name"]: pt["count"] for pt in cov.get("user_points", [])}
    fault_runs = {d: r for r in runs for d in (r.get("defines") or []) if r.get("expect_fail")}
    rows_by_assert: dict[str, dict] = {}
    vplan_path = cfg.root / "vplan.yaml"
    if vplan_path.exists():
        try:
            import yaml
            for row in (yaml.safe_load(vplan_path.read_text()) or {}).get("features", []) or []:
                for a in row.get("assertions") or []:
                    rows_by_assert[a] = row
        except Exception:  # a broken vplan is reported by `vplan check`, not here
            rows_by_assert = {}
    formal, formal_asserts = _formal_evidence(cfg)
    equiv = _equiv_evidence(cfg)
    sva = []
    for name, f in sorted(asserts.items()):
        row = rows_by_assert.get(name)
        row_covers = [c for c in (row.get("covers") or []) if c in covers] if row else sorted(covers)
        row_faults = list(row.get("faults") or []) if row else list(fault_runs)
        if not row_covers:
            cover_state = "missing"
        else:
            cover_state = "hit" if any(cov_hits.get(c, 0) > 0 for c in row_covers) else "not_hit"
        fault = (sva_fault_map or {}).get(name) or next((x for x in row_faults if x in fault_runs), None)
        if fault:
            mutation = "caught" if fault_runs[fault]["status"] != "pass" else "missed"
        elif any(x in fault_runs and fault_runs[x]["status"] == "pass" for x in row_faults):
            fault = next(x for x in row_faults if x in fault_runs)
            mutation = "missed"
        else:
            mutation = "not_available"
        entry = {"name": name, "file": f, "proof": "not_run", "cover": cover_state, "mutation": mutation, "fault": fault, "vplan": row.get("id") if row else None}
        fa = formal_asserts.get(name)
        if fa:
            entry.update({"proof": fa["proof"], "witness": fa.get("witness"), "formal_mutation": fa.get("mutation")})
        sva.append(entry)

    files = changed_files(cfg, base)
    sha = git(cfg, "rev-parse", "--short", "HEAD") or "worktree"
    parsed_claims = []
    for c in claims:
        m = LABEL_RE.match(c)
        parsed_claims.append({"text": m.group(2).strip(), "label": m.group(1).lower()} if m else {"text": c, "label": "guessed"})

    b = {
        "version": 1,
        "sha": sha if len(sha) >= 7 else sha.ljust(7, "0"),
        "base_sha": base,
        "block": block or cfg.project.get("name", "unknown"),
        "door": door,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
        "lint": {"tool": lint.get("tool", "none"), "errors": lint.get("error_count") or 0, "warnings": lint.get("warning_count") or 0, "log": lint.get("log", "")},
        "sim": {"tool": cfg.sim.get("tool", "verilator"), "runs": runs, "failed": failed, "failures": failures},
        "cov": {"tool": "verilator", "before": before, "after": cov.get("percent"), "floor": cfg.cov.get("floor"), "excluded_bins_added": 0, "report": "evidence/cov.json" if cov else ""},
        "sva": sva,
        "waivers_added": 0,
        "claims": parsed_claims,
        "gaps": list(gaps),
    }
    if formal is not None:
        b["formal"] = formal
    if equiv is not None:
        b["equiv"] = equiv
    b["verdict"] = verdict(b)
    return b


def _formal_evidence(cfg: Config) -> tuple[dict | None, dict]:
    """Summary of evidence/formal.json if it matches the current sources, else a 'stale' marker."""
    p = cfg.evidence_dir / "formal.json"
    if not p.exists():
        return None, {}
    try:
        r = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"verdict": "INCONCLUSIVE", "status": "unreadable", "required": False}, {}
    from .formal.check import source_hash
    stale = r.get("status") == "ran" and source_hash(cfg, r.get("files") or []) != r.get("source_hash")
    summary = {"verdict": "INCONCLUSIVE" if stale else r.get("verdict"), "status": "stale" if stale else r.get("status"),
               "backend": r.get("backend"), "depth": r.get("depth"), "required": bool(r.get("required")),
               "faults": [{"fault": f["fault"], "state": f["state"]} for f in r.get("faults", [])], "report": "evidence/formal.json"}
    return summary, ({} if stale else {a["name"]: a for a in r.get("asserts", [])})


def _equiv_evidence(cfg: Config) -> dict | None:
    p = cfg.evidence_dir / "equiv.json"
    if not p.exists():
        return None
    try:
        r = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"verdict": "INCONCLUSIVE", "status": "unreadable"}
    from .formal.check import source_hash
    stale = source_hash(cfg, r.get("files") or []) != r.get("source_hash")
    return {"verdict": "INCONCLUSIVE" if stale else r.get("verdict"), "status": "stale" if stale else r.get("status"),
            "base": r.get("base"), "backend": r.get("backend"), "unproven": r.get("unproven", []), "report": "evidence/equiv.json"}


def verdict(b: dict) -> str:
    if b["lint"]["errors"] is None:
        return "INCONCLUSIVE"
    if b["lint"]["errors"] > 0 or b["sim"]["failed"] > 0:
        return "FAIL"
    if not b["sim"]["runs"]:
        return "INCONCLUSIVE"
    cov = b["cov"]
    if cov.get("after") is None:
        return "INCONCLUSIVE"
    if cov.get("floor") is not None and cov["after"] < cov["floor"]:
        return "FAIL"
    if cov.get("before") is not None and cov["after"] < cov["before"]:
        return "FAIL"
    if cov.get("excluded_bins_added", 0) > 0 or b.get("waivers_added", 0) > 0:
        return "FAIL"
    if any(c["label"] == "guessed" for c in b["claims"]):
        return "INCONCLUSIVE"
    f, e = b.get("formal"), b.get("equiv")
    if f and f.get("verdict") == "FAIL":
        return "FAIL"
    if e and e.get("verdict") == "FAIL":
        return "FAIL"
    if any(s.get("proof") in ("cex", "fail") for s in b["sva"]):
        return "FAIL"
    for s in b["sva"]:
        if s["cover"] == "not_hit" or s["mutation"] == "missed":
            return "FAIL"
        if s["cover"] == "missing" or s["mutation"] == "not_available":
            return "INCONCLUSIVE"
    if f and f.get("verdict") != "PASS" and (f.get("required") or f.get("status") not in ("tool_missing",)):
        return "INCONCLUSIVE"
    if e and e.get("verdict") != "PASS":
        return "INCONCLUSIVE"
    return "PASS"


def check(path: Path) -> dict:
    try:
        b = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"cannot read {path}: {e}"}
    problems = _validate(b)
    expected = verdict(b) if not problems else None
    if expected and b.get("verdict") != expected:
        problems.append(f"verdict says {b.get('verdict')} but the evidence computes to {expected}")
    return {"ok": not problems, "verdict": b.get("verdict"), "computed": expected, "problems": problems, "path": str(path)}


def _validate(b: dict) -> list[str]:
    """Minimal structural validation without a jsonschema dependency."""
    try:
        schema = json.loads(SCHEMA.read_text())
    except OSError:
        schema = {"required": ["version", "sha", "verdict", "lint", "sim", "cov", "sva", "claims"]}
    problems = [f"missing {k}" for k in schema.get("required", []) if k not in b]
    if b.get("version") != 1:
        problems.append("version must be 1")
    if b.get("verdict") not in ("PASS", "FAIL", "INCONCLUSIVE"):
        problems.append("verdict must be PASS, FAIL or INCONCLUSIVE")
    if b.get("door") not in ("two-way", "one-way"):
        problems.append("door must be two-way or one-way")
    for c in b.get("claims", []):
        if c.get("label") not in ("measured", "inferred", "guessed"):
            problems.append(f"claim '{c.get('text')}' has no valid label")
    for r in b.get("sim", {}).get("runs", []):
        if r.get("status") not in ("pass", "fail", "error"):
            problems.append(f"run {r.get('test')}/{r.get('seed')} has bad status")
    for s in b.get("sva", []):
        if s.get("proof", "not_run") not in PROOF_STATES:
            problems.append(f"sva {s.get('name')} has bad proof state")
        if s.get("cover") not in ("hit", "not_hit", "missing") or s.get("mutation") not in ("caught", "missed", "not_available"):
            problems.append(f"sva {s.get('name')} has bad cover/mutation state")
    return problems
