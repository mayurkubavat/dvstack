"""rules: review comments promoted to checks that run everywhere.

`rules.yaml` at the project root holds the project's own rules. It needs no EDA
tool: lint rules are regexes run by control-eda itself, so every user gets them.

    version: 1
    rules:
      - id: no-casex
        review: "Don't use casex, it hides X"         # the comment, in the reviewer's words
        seen: [https://github.com/org/repo/pull/12#r1, ...]  # where it was said (rule of three)
        kind: lint
        files: ["rtl/**/*.sv"]
        pattern: '\\bcasex\\b'
        message: "casex treats X as don't-care; use `case inside`"
        examples:
          bad:  ["casex (sel)"]
          good: ["case (sel) inside"]
      - id: fifo-full-exact
        review: "full must mean DEPTH entries, not almost-full"
        kind: sva
        assertion: a_full_iff_depth     # must exist in RTL and on a vplan row
        fault: FIFO_BUG_FULL_EARLY      # formal must show this fault breaks the assertion

`rules test` proves each rule can fire: a lint rule must match every bad example
and no good one; an sva rule must have a formal counterexample for its fault in
evidence/formal.json. A rule that never fired is a draft. `control-eda lint`
reports violations of tested lint rules as errors, so `verify` gates on them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config
from .formal import sva

DEFAULT_FILES = ["**/*.sv", "**/*.svh", "**/*.v", "**/*.vh"]


def _load(cfg: Config) -> tuple[list[dict], list[str]]:
    p = cfg.root / "rules.yaml"
    if not p.exists():
        return [], []
    try:
        import yaml
        data = yaml.safe_load(p.read_text()) or {}
    except Exception as e:
        return [], [f"rules.yaml is not valid YAML: {e}"]
    problems = []
    rules = []
    seen = set()
    for i, r in enumerate(data.get("rules") or []):
        if not isinstance(r, dict) or not r.get("id"):
            problems.append(f"rule #{i + 1} has no id")
            continue
        if r["id"] in seen:
            problems.append(f"duplicate rule id {r['id']}")
        seen.add(r["id"])
        r.setdefault("kind", "lint")
        if r["kind"] not in ("lint", "sva"):
            problems.append(f"{r['id']}: kind must be lint or sva")
        rules.append(r)
    return rules, problems


def list_rules(cfg: Config) -> dict:
    rules, problems = _load(cfg)
    return {"ok": not problems, "rules": rules, "problems": problems, "path": "rules.yaml" if (cfg.root / "rules.yaml").exists() else None}


def _compile(r: dict) -> re.Pattern | None:
    try:
        return re.compile(r["pattern"], re.M | (re.I if r.get("ignore_case") else 0))
    except (re.error, KeyError, TypeError):
        return None


def _files(cfg: Config, globs: list[str], only: list[str] | None) -> list[Path]:
    out = []
    for g in globs:
        for p in sorted(cfg.root.glob(g)):
            rel = str(p.relative_to(cfg.root))
            if any(part in ("build", "evidence", ".git") for part in p.relative_to(cfg.root).parts):
                continue
            if only is not None and rel not in only:
                continue
            if p.is_file():
                out.append(p)
    return sorted(set(out))


def check(cfg: Config, only: list[str] | None = None, tested_only: bool = False) -> dict:
    rules, problems = _load(cfg)
    status = {x["id"]: x["state"] for x in test(cfg)["rules"]} if tested_only else {}
    violations = []
    for r in rules:
        if r["kind"] != "lint":
            continue
        if tested_only and status.get(r["id"]) != "fires":
            continue
        rx = _compile(r)
        if rx is None:
            problems.append(f"{r['id']}: pattern does not compile")
            continue
        for p in _files(cfg, r.get("files") or DEFAULT_FILES, only):
            text = p.read_text(errors="replace")
            hay = sva.blank_comments(text) if r.get("ignore_comments", True) else text
            for m in rx.finditer(hay):
                line = text.count("\n", 0, m.start()) + 1
                violations.append({"rule": r["id"], "file": str(p.relative_to(cfg.root)), "line": line,
                                   "message": r.get("message") or r.get("review") or r["id"], "severity": r.get("severity", "error"),
                                   "text": text.splitlines()[line - 1].strip()[:160]})
    errors = [v for v in violations if v["severity"] == "error"]
    return {"ok": not errors and not problems, "violations": violations, "violation_count": len(violations), "error_count": len(errors), "problems": problems}


def test(cfg: Config) -> dict:
    rules, problems = _load(cfg)
    formal = {}
    fj = cfg.evidence_dir / "formal.json"
    if fj.exists():
        try:
            formal = json.loads(fj.read_text())
        except json.JSONDecodeError:
            formal = {}
    labels: dict[str, str] = {}
    for g in cfg.project.get("rtl_globs", []) + cfg.project.get("tb_globs", []):
        for p in cfg.root.glob(g):
            for prop in sva.extract(p.read_text(errors="replace"), str(p.relative_to(cfg.root))):
                labels[prop.label] = prop.kind
    vplan_text = (cfg.root / "vplan.yaml").read_text() if (cfg.root / "vplan.yaml").exists() else ""
    out = []
    for r in rules:
        res = {"id": r["id"], "kind": r["kind"], "problems": []}
        if not r.get("review"):
            res["problems"].append("no `review:` text; say which comment this rule replaces")
        if r["kind"] == "lint":
            rx = _compile(r)
            ex = r.get("examples") or {}
            bad, good = list(ex.get("bad") or []), list(ex.get("good") or [])
            if rx is None:
                res["problems"].append("pattern missing or does not compile")
            else:
                if not bad:
                    res["problems"].append("no bad examples: a rule that has never fired is a draft")
                prep = (lambda t: sva.blank_comments(t)) if r.get("ignore_comments", True) else (lambda t: t)
                for b in bad:
                    if not rx.search(prep(b)):
                        res["problems"].append(f"does not fire on bad example: {b!r}")
                for g in good:
                    if rx.search(prep(g)):
                        res["problems"].append(f"fires on good example: {g!r}")
            res["state"] = "fires" if not res["problems"] else "draft"
        else:
            a, fault = r.get("assertion"), r.get("fault")
            if not a or labels.get(a) != "assert":
                res["problems"].append(f"assertion {a!r} is not an assert property in the RTL")
            if a and a not in vplan_text:
                res["problems"].append(f"assertion {a!r} is not on any vplan row")
            if not fault:
                res["problems"].append("no `fault:` define that the assertion must catch")
            state = "draft" if res["problems"] else None
            if state is None:
                hit = next((f for f in formal.get("faults", []) if f.get("fault") == fault), None)
                stale = formal and formal.get("source_hash") and _stale(cfg, formal)
                if not formal or formal.get("status") != "ran":
                    state = "unverified"
                    res["problems"].append("no formal run in evidence/formal.json (run `control-eda formal check`; if no formal tool is installed this stays unverified)")
                elif stale:
                    state = "unverified"
                    res["problems"].append("evidence/formal.json is older than the RTL; rerun `control-eda formal check`")
                elif hit and a in hit.get("cex", []):
                    state = "fires"
                else:
                    state = "draft"
                    res["problems"].append(f"formal found no counterexample on {a} for {fault}")
            res["state"] = state
        out.append(res)
    ok = not problems and all(x["state"] == "fires" for x in out)
    return {"ok": ok, "rules": out, "problems": problems,
            "summary": {s: sum(1 for x in out if x["state"] == s) for s in ("fires", "draft", "unverified")}}


def _stale(cfg: Config, formal: dict) -> bool:
    from .formal.check import source_hash
    return source_hash(cfg, formal.get("files") or []) != formal.get("source_hash")

