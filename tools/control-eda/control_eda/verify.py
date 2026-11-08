"""verify: the whole evidence gate in one command, so a reviewer can rerun it.

Steps: lint -> sim every vplan test on fixed + random seeds -> fault injection for
every vplan fault -> coverage merge -> bundle -> verdict. Nothing here can weaken
the gate: seeds and floors come from dv.toml, faults and tests from vplan.yaml.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from . import cov as covmod
from . import door as doormod
from . import evidence as evmod
from . import lint as lintmod
from . import sim as simmod
from . import vplan as vplanmod
from .config import Config


def run_verify(cfg: Config, base: str | None, files: list[str] | None, claims: list[str], gaps: list[str], door: str | None, keep: bool, progress=None) -> dict:
    say = progress or (lambda *_: None)
    steps: dict = {}

    # 1. lint
    say("lint")
    lint = lintmod.lint(cfg)
    (cfg.evidence_dir / "lint.json").write_text(json.dumps(lint, indent=2))
    steps["lint"] = {"ok": lint.get("ok"), "errors": lint.get("error_count"), "warnings": lint.get("warning_count")}

    # 2. scope -> vplan rows
    changed = files if files is not None else evmod.changed_files(cfg, base)
    hdl = [f for f in changed if f.endswith((".sv", ".svh", ".v", ".vh"))]
    look = vplanmod.lookup(cfg, files=hdl) if hdl else vplanmod.lookup(cfg)
    tests = look.get("tests") or cfg.tests
    faults = look.get("faults") or []
    rows = look.get("matches") or []
    all_gaps = list(gaps)
    covered_rtl = {r for row in rows for r in row.get("rtl") or []}
    for f in hdl:
        if f not in covered_rtl and not any(h in f for h in ("tb", "test")):
            all_gaps.append(f"{f} has no vplan row")
    steps["scope"] = {"changed": changed, "tests": tests, "faults": faults, "rows": [r.get("id") for r in rows]}

    # 3. sims
    if not keep:
        sim_dir = cfg.evidence_dir / "sim"
        if sim_dir.exists():
            for p in sim_dir.glob("*"):
                if p.name.startswith("build_"):
                    continue
                p.unlink()
    seeds = list(cfg.sim.get("seeds", [1]))
    rng = random.Random()
    seeds += [rng.randint(1, 2**31 - 1) for _ in range(int(cfg.sim.get("random_seeds", 0)))]
    runs = []
    for t in tests:
        for s in seeds:
            say(f"sim {t} seed {s}")
            r = simmod.run_test(cfg, t, s, cov=True, trace=True)
            _save_run(cfg, r)
            runs.append({"test": t, "seed": s, "status": r.get("status"), "ok": r.get("ok")})
    # 4. fault injection (first fixed seed, each fault, each test that lists it)
    for row in rows:
        for fault in row.get("faults") or []:
            for t in row.get("tests") or tests:
                say(f"fault {fault} via {t}")
                r = simmod.run_test(cfg, t, seeds[0], defines=[fault], cov=False, trace=False, expect_fail=True)
                _save_run(cfg, r)
                runs.append({"test": t, "seed": seeds[0], "fault": fault, "status": r.get("status"), "ok": r.get("ok")})
    steps["sim"] = {"runs": len(runs), "failed": sum(1 for r in runs if not r.get("ok"))}

    # 5. coverage
    say("coverage")
    covr = covmod.merge_project(cfg)
    steps["cov"] = {"percent": covr.get("percent"), "floor": cfg.cov.get("floor")}

    # 6. bundle
    say("bundle")
    d = door or doormod.classify(cfg, base)["door"]
    b = evmod.bundle(cfg, base, None, d, claims, all_gaps)
    out = cfg.evidence_dir / f"{b['sha']}.json"
    out.write_text(json.dumps(b, indent=2))
    if b["sha"] != "worktree":
        (cfg.evidence_dir / "worktree.json").write_text(json.dumps(b, indent=2))
    return {"ok": b["verdict"] == "PASS", "verdict": b["verdict"], "bundle": str(out.relative_to(cfg.root)), "steps": steps, "gaps": all_gaps, "sva": b["sva"]}


def _save_run(cfg: Config, r: dict) -> None:
    if r.get("status") in ("pass", "fail", "error"):
        tag = f"{r['test']}_{r['seed']}" + ("_" + "_".join(r["defines"]) if r.get("defines") else "")
        (cfg.evidence_dir / "sim" / f"{tag}.json").write_text(json.dumps(r, indent=2))


def text_report(r: dict) -> str:
    s = r["steps"]
    lines = [f"VERDICT: {r['verdict']}   bundle: {r['bundle']}", ""]
    lines.append(f"  lint   {s['lint']['errors']} errors, {s['lint']['warnings']} warnings")
    lines.append(f"  sim    {s['sim']['runs']} runs, {s['sim']['failed']} not ok   tests={s['scope']['tests']} faults={s['scope']['faults']}")
    lines.append(f"  cov    {s['cov']['percent']}% (floor {s['cov']['floor']})")
    for a in r["sva"]:
        lines.append(f"  sva    {a['name']}: cover {a['cover']}, mutation {a['mutation']}" + (f" ({a['fault']})" if a.get("fault") else ""))
    if r["gaps"]:
        lines += ["", "gaps:"] + [f"  - {g}" for g in r["gaps"]]
    return "\n".join(lines)
