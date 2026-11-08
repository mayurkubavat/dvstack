"""formal check: the anti-vacuity gate for SVA.

For every assertion in the formal scope it asks four questions, each answered
by a tool run, never by reading the code:

  1. proof     Does it hold?   proven (induction closed) | bounded (no cex to depth N) | cex
  2. witness   Can its trigger happen?  each assert needs a reachable cover: one of
               its vplan `covers`, or the auto-generated `<label>__antecedent` cover
               of an implication. An assert whose covers are unreachable is vacuous.
  3. mutation  Can it fail?  every vplan fault define listed on the assert's row must
               produce a counterexample on one of that row's assertions.
  4. assumes   Every `assume` must be listed in vplan `assumes:` with the upstream
               assert that guarantees it, and that assert must exist.

Verdict: FAIL on a cex, an unreachable witness or a missed fault. INCONCLUSIVE when
a tool is missing, a property is unsupported by the chosen backend, a proof is only
bounded (unless dv.toml says accept_bounded = true), an assert has no witness or no
fault, or an assume has no upstream assert. PASS otherwise.

Settings come from [formal] in dv.toml, never from the agent.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from . import backends as bk
from . import sva


def formal_section(cfg: Config) -> dict:
    return dict(cfg.raw.get("formal") or {})


def _sources(cfg: Config, sec: dict) -> list[str]:
    if sec.get("sources"):
        return list(sec["sources"])
    out = []
    for g in cfg.project.get("rtl_globs", []):
        out += sorted(str(p.relative_to(cfg.root)) for p in cfg.root.glob(g))
    return out


def source_hash(cfg: Config, files: list[str]) -> str:
    h = hashlib.sha1()
    for f in sorted(files):
        p = cfg.root / f
        h.update(f.encode())
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def _vplan_rows(cfg: Config) -> list[dict]:
    vp = cfg.root / "vplan.yaml"
    if not vp.exists():
        return []
    try:
        import yaml
        return list((yaml.safe_load(vp.read_text()) or {}).get("features") or [])
    except Exception:
        return []


def _all_labels(cfg: Config, sec: dict) -> dict[str, str]:
    """Every assert label in the project (rtl_globs, tb_globs, formal sources, upstream_globs) -> file."""
    globs = list(cfg.project.get("rtl_globs", [])) + list(cfg.project.get("tb_globs", [])) + list(sec.get("upstream_globs", []))
    files = {str(p.relative_to(cfg.root)) for g in globs for p in cfg.root.glob(g)} | set(_sources(cfg, sec))
    out = {}
    for f in sorted(files):
        p = cfg.root / f
        if p.is_file():
            for prop in sva.extract(p.read_text(errors="replace"), f):
                if prop.kind == "assert":
                    out[prop.label] = f
    return out


def detect(cfg: Config | None) -> dict:
    sec = formal_section(cfg) if cfg else {}
    esec = dict(cfg.raw.get("equiv") or {}) if cfg else {}
    root = cfg.root if cfg else Path.cwd()
    fb, finfo = bk.select("formal", sec, root)
    eb, einfo = bk.select("equiv", esec, root)
    hints = []
    if not fb:
        hints.append("no formal backend found: " + bk.OSS_HINT + "; or put JasperGold (jg), VC Formal (vcf) or Questa Formal (qverify) on PATH")
    if not eb:
        hints.append("no equivalence backend found: install yosys, or put Conformal (lec) or Formality (fm_shell) on PATH")
    return {"ok": True, "formal": finfo, "equiv": einfo, "solvers": bk.available_solvers(),
            "selected": {"formal": fb.name if fb else None, "equiv": eb.name if eb else None},
            "configured": {"formal": bool(sec), "equiv": bool(esec)}, "hints": hints}


def run_check(cfg: Config, backend: str | None = None, depth: int | None = None, only: list[str] | None = None, progress=None) -> dict:
    say = progress or (lambda *_: None)
    sec = formal_section(cfg)
    if backend:
        sec["backend"] = backend
    steps = set(only or ["prove", "cover", "mutate", "assumes"])
    files = _sources(cfg, sec)
    top = sec.get("top") or cfg.project.get("formal_top")
    depth = int(depth or sec.get("depth", 20))
    clock = sec.get("clock")
    reset = sec.get("reset")
    result: dict = {
        "version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "top": top, "files": files, "source_hash": source_hash(cfg, files), "depth": depth,
        "backend": None, "required": bool(sec.get("required", False)),
        "accept_bounded": bool(sec.get("accept_bounded", False)),
        "asserts": [], "covers": [], "assumes": [], "faults": [], "problems": [], "gaps": [], "runs": [],
    }
    if not sec:
        result.update({"verdict": "INCONCLUSIVE", "status": "not_configured",
                       "gaps": ["dv.toml has no [formal] section (top, sources, clock, reset, depth); formal was not run"]})
        return result
    if not top:
        result.update({"verdict": "INCONCLUSIVE", "status": "not_configured", "gaps": ["[formal].top is not set in dv.toml"]})
        return result

    props: list[sva.Prop] = []
    texts: dict[str, str] = {}
    for f in files:
        p = cfg.root / f
        if not p.exists():
            result["problems"].append(f"formal source missing: {f}")
            continue
        texts[f] = p.read_text(errors="replace")
        props += sva.extract(texts[f], f, default_clock=clock)
    need_native = any(p.unsupported for p in props)
    be, infos = bk.select("formal", sec, cfg.root, need_native=need_native)
    result["detected"] = [{k: i.get(k) for k in ("name", "family", "available", "missing", "install", "native_sva", "tested")} for i in infos]
    if be is None:
        wanted = sec.get("backend", "auto")
        why = (f"backend '{wanted}' was requested but is not available"
               + "".join(f"; missing {', '.join(i['missing'])} ({i.get('install')})" for i in infos if i["name"] == wanted)
               if wanted != "auto" else "no formal tool found on this machine")
        hint = bk.OSS_HINT
        result.update({"verdict": "INCONCLUSIVE", "status": "tool_missing", "install": hint})
        result["gaps"].append(f"formal not run: {why}. Install: {hint}")
        for p in props:
            if p.kind == "assert":
                result["asserts"].append({"name": p.label, "file": p.file, "line": p.line, "proof": "tool_missing", "witness": "not_run", "mutation": "not_run"})
        return result
    result["backend"] = be.name
    native = be.native_sva
    work = cfg.build_dir / "formal"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    # shim: rewrite for free Yosys if needed, add the reset assumption and antecedent covers
    shim_dir = work / "src"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_files = []
    for f, text in texts.items():
        fprops = [p for p in props if p.file == f]
        # commercial and custom flows set reset in their own script; the open-source ones need it as an assume
        out = sva.shim(text, fprops, translate=not native, top=top, reset=reset if be.family == "oss" else None)
        dst = shim_dir / f.replace("/", "__")
        dst.write_text(out)
        shim_files.append(dst)
    if not reset:
        result["gaps"].append("[formal].reset is not set: the solver may start in any state, so a cex may be unreachable from reset")

    checked = [p for p in props if native or not p.unsupported]
    asserts = [p for p in checked if p.kind == "assert"]
    explicit_covers = [p.label for p in checked if p.kind == "cover"]
    ante_covers = {p.label: f"{p.label}__antecedent" for p in asserts if p.antecedent}
    all_covers = explicit_covers + list(ante_covers.values())
    for p in props:
        if p.unsupported and not native:
            result["gaps"].append(f"{p.kind} {p.label} ({p.file}:{p.line}) not checked by {be.title}: {p.unsupported}. "
                                  "Needs a backend with native SVA (Yosys with Verific, JasperGold, VC Formal, Questa)")
        if not p.named_label:
            result["gaps"].append(f"{p.kind} at {p.file}:{p.line} has no label; label it so results can be tracked")

    def job(mode: str, defines: list[str] | None = None, sub: str = "") -> bk.Job:
        return bk.Job(mode=mode, top=top, workdir=work / (sub or mode), files=shim_files, depth=depth,
                      defines=list(sec.get("defines", [])) + list(defines or []), include_dirs=list(sec.get("include_dirs", [])),
                      clock=clock, reset=reset, asserts=[p.label for p in asserts], covers=all_covers,
                      timeout=int(sec.get("timeout", 900)), solver=sec.get("solver"))

    proof: dict[str, str] = {}
    if "prove" in steps and asserts:
        say(f"formal prove ({be.name}, depth {depth})")
        r = be.run(job("prove"))
        result["runs"].append(_brief(r, cfg))
        proof = r.get("asserts", {})
        if r["status"] in ("error", "unknown") and r.get("detail"):
            result["problems"].append(f"prove: {r['detail']}")
        if r.get("trace"):
            result["cex_trace"] = _rel(cfg, r["trace"])
    cover_state: dict[str, str] = {}
    if "cover" in steps and all_covers:
        say(f"formal cover ({len(all_covers)} witnesses)")
        r = be.run(job("cover"))
        result["runs"].append(_brief(r, cfg))
        cover_state = r.get("covers", {})
        if r["status"] in ("error", "unknown") and r.get("detail"):
            result["problems"].append(f"cover: {r['detail']}")
        for c in all_covers:
            result["covers"].append({"name": c, "state": cover_state.get(c, "unknown"), "step": (r.get("steps") or {}).get(c),
                                     "auto": c in ante_covers.values()})

    rows = _vplan_rows(cfg)
    row_of: dict[str, dict] = {a: row for row in rows for a in (row.get("assertions") or [])}
    mutation: dict[str, dict] = {}
    if "mutate" in steps:
        labels = {p.label for p in asserts}
        faults: dict[str, set[str]] = {}
        for row in rows:
            ra = [a for a in (row.get("assertions") or []) if a in labels]
            if not ra:
                continue
            for fault in row.get("faults") or []:
                faults.setdefault(fault, set()).update(ra)
        for fault, owners in sorted(faults.items()):
            say(f"formal mutate {fault}")
            r = be.run(job("bmc", [fault], sub=f"mutate_{fault}"))
            result["runs"].append(_brief(r, cfg))
            cex = sorted(a for a, s in (r.get("asserts") or {}).items() if s == "cex")
            caught_by_owner = sorted(set(cex) & owners)
            state = "caught" if caught_by_owner else ("caught_elsewhere" if cex else ("missed" if r["status"] == "pass" else "unknown"))
            entry = {"fault": fault, "state": state, "cex": cex, "owners": sorted(owners)}
            if state == "unknown" and r.get("detail"):
                entry["detail"] = r["detail"]
            result["faults"].append(entry)
            for a in owners:
                prev = mutation.get(a)
                if prev is None or prev["state"] != "caught":
                    mutation[a] = {"state": "caught" if a in cex else ("caught_by_row" if caught_by_owner else state), "fault": fault}

    if "assumes" in steps:
        known = _all_labels(cfg, sec)
        listed: dict[str, str] = {}
        for row in rows:
            for k, v in (row.get("assumes") or {}).items():
                listed[k] = v
        for p in props:
            if p.kind != "assume":
                continue
            up = listed.get(p.label)
            up_label = up.split(".")[-1].split(":")[-1] if up else None
            state = "matched" if up_label and up_label in known else ("upstream_missing" if up else "unlisted")
            result["assumes"].append({"name": p.label, "file": p.file, "line": p.line, "upstream": up, "state": state})

    # per-assert summary
    for p in asserts:
        row = row_of.get(p.label)
        twins = [c for c in (row.get("covers") or []) if c in explicit_covers] if row else []
        if p.label in ante_covers:
            twins.append(ante_covers[p.label])
        if "cover" not in steps:
            witness = "not_run"
        elif not twins:
            witness = "missing"
        elif any(cover_state.get(c) == "reached" for c in twins):
            witness = "reached"
        elif all(cover_state.get(c) == "unreachable" for c in twins):
            witness = "unreachable"
        else:
            witness = "unknown"
        m = mutation.get(p.label)
        result["asserts"].append({
            "name": p.label, "file": p.file, "line": p.line, "vplan": row.get("id") if row else None,
            "proof": proof.get(p.label, "not_run" if "prove" not in steps else "unknown"),
            "witness": witness, "witnesses": twins,
            "mutation": (m["state"] if m else ("not_run" if "mutate" not in steps else "no_fault")),
            "fault": m["fault"] if m else None,
        })
    for p in props:
        if p.kind == "assert" and p not in asserts:
            result["asserts"].append({"name": p.label, "file": p.file, "line": p.line, "vplan": (row_of.get(p.label) or {}).get("id"),
                                      "proof": "unsupported", "witness": "not_run", "mutation": "not_run", "fault": None, "why": p.unsupported})
    if not props:
        result["gaps"].append("no assert, assume or cover property found in the formal sources")
    result["verdict"] = verdict(result)
    result["status"] = "ran"
    (cfg.evidence_dir / "formal.json").write_text(json.dumps(result, indent=2))
    return result


def verdict(r: dict) -> str:
    if r.get("status") in ("tool_missing", "not_configured"):
        return "INCONCLUSIVE"
    a = r.get("asserts", [])
    if any(x["proof"] == "cex" for x in a) or any(x["witness"] == "unreachable" for x in a) \
            or any(f["state"] == "missed" for f in r.get("faults", [])):
        return "FAIL"
    if r.get("problems") or not a:
        return "INCONCLUSIVE"
    ok_proof = {"proven", "bounded"} if r.get("accept_bounded") else {"proven"}
    if any(x["proof"] not in ok_proof for x in a):
        return "INCONCLUSIVE"
    if any(x["witness"] != "reached" for x in a if x["proof"] != "not_run"):
        return "INCONCLUSIVE"
    if any(x["mutation"] not in ("caught", "caught_by_row") for x in a):
        return "INCONCLUSIVE"
    if any(s["state"] != "matched" for s in r.get("assumes", [])):
        return "INCONCLUSIVE"
    return "PASS"


def reasons(r: dict) -> list[str]:
    """Plain-language reasons behind a non-PASS verdict, for agents and humans."""
    if r.get("status") in ("tool_missing", "not_configured"):
        return list(r.get("gaps", []))
    out = []
    for x in r.get("asserts", []):
        if x["proof"] == "cex":
            out.append(f"{x['name']}: counterexample found (see {r.get('cex_trace') or 'the prove log'})")
        elif x["proof"] == "bounded" and not r.get("accept_bounded"):
            out.append(f"{x['name']}: no counterexample in {r.get('depth')} cycles but not proven; raise [formal].depth or add helper assertions")
        elif x["proof"] in ("unknown", "unsupported", "tool_missing"):
            out.append(f"{x['name']}: proof {x['proof']}" + (f" ({x.get('why')})" if x.get("why") else ""))
        if x["witness"] == "unreachable":
            out.append(f"{x['name']}: vacuous, none of its covers {x.get('witnesses')} can be reached")
        elif x["witness"] == "missing":
            out.append(f"{x['name']}: no cover witness; add a cover to its vplan row or write it as an implication")
        if x["mutation"] == "no_fault":
            out.append(f"{x['name']}: no fault define on its vplan row proves it can fail")
        elif x["mutation"] in ("missed", "unknown"):
            out.append(f"{x['name']}: fault {x.get('fault')} {x['mutation']}")
    for f in r.get("faults", []):
        if f["state"] == "missed":
            out.append(f"fault {f['fault']} produced no counterexample: the assertions {f['owners']} cannot see this bug")
        elif f["state"] == "caught_elsewhere":
            out.append(f"fault {f['fault']} was caught only by {f['cex']}, not by its row's assertions {f['owners']}")
    for s in r.get("assumes", []):
        if s["state"] == "unlisted":
            out.append(f"assume {s['name']} has no `assumes:` entry in vplan.yaml naming the upstream assert that guarantees it")
        elif s["state"] == "upstream_missing":
            out.append(f"assume {s['name']} names upstream assert {s['upstream']}, which was not found")
    return out + list(r.get("problems", [])) + list(r.get("gaps", []))


def _rel(cfg: Config, p: str) -> str:
    try:
        return str(Path(p).resolve().relative_to(cfg.root))
    except ValueError:
        return p


def _brief(r: dict, cfg: Config) -> dict:
    return {k: (_rel(cfg, v) if k in ("log", "trace") and isinstance(v, str) else v)
            for k, v in r.items() if k in ("backend", "mode", "status", "log", "trace", "seconds", "detail")}


def text_report(r: dict) -> str:
    lines = [f"FORMAL: {r['verdict']}   backend: {r.get('backend') or 'none'}   depth: {r.get('depth')}", ""]
    for a in r.get("asserts", []):
        lines.append(f"  assert {a['name']:24} proof={a['proof']:9} witness={a['witness']:11} mutation={a['mutation']}"
                     + (f" ({a['fault']})" if a.get("fault") else ""))
    for c in r.get("covers", []):
        lines.append(f"  cover  {c['name']:24} {c['state']}" + (f" at step {c['step']}" if c.get("step") is not None else "") + (" (auto)" if c.get("auto") else ""))
    for f in r.get("faults", []):
        lines.append(f"  fault  {f['fault']:24} {f['state']}" + (f" by {', '.join(f['cex'])}" if f["cex"] else ""))
    for s in r.get("assumes", []):
        lines.append(f"  assume {s['name']:24} {s['state']}" + (f" <- {s['upstream']}" if s.get("upstream") else ""))
    why = reasons(r) if r["verdict"] != "PASS" else list(r.get("gaps", []))
    if why:
        lines += ["", "why:" if r["verdict"] != "PASS" else "notes:"] + [f"  - {x}" for x in why]
    return "\n".join(lines)
