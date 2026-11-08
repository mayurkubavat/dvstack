"""equiv: prove a refactor is behavior-preserving against a base commit.

Gold is the RTL at `--base` (read with `git show`), gate is the working tree.
Properties are stripped first so only the design logic is compared. Results:

  equivalent   every compared point proven equal           -> PASS
  not_proven   some points not proven (lists them)         -> FAIL for a refactor
  tool_missing no equivalence backend on this machine      -> INCONCLUSIVE
  error        the tool ran but its result was unreadable  -> INCONCLUSIVE
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config
from . import backends as bk
from . import sva
from .check import source_hash


def _git(cfg: Config, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cfg.root, capture_output=True, text=True)
    return p.returncode, p.stdout


def run_equiv(cfg: Config, base: str, files: list[str] | None = None, backend: str | None = None, top: str | None = None) -> dict:
    sec = dict(cfg.raw.get("equiv") or {})
    fsec = dict(cfg.raw.get("formal") or {})
    if backend:
        sec["backend"] = backend
    srcs = files or sec.get("sources") or fsec.get("sources") or []
    if not srcs:
        for g in cfg.project.get("rtl_globs", []):
            srcs += sorted(str(p.relative_to(cfg.root)) for p in cfg.root.glob(g))
    top = top or sec.get("top") or fsec.get("top")
    code, sha = _git(cfg, "rev-parse", "--short", "HEAD")
    res: dict = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "base": base, "sha": sha.strip() or "worktree", "top": top, "files": srcs,
                 "source_hash": source_hash(cfg, srcs), "backend": None, "unproven": [], "gaps": []}
    if not top:
        res.update({"status": "error", "verdict": "INCONCLUSIVE", "detail": "set [equiv].top (or [formal].top) in dv.toml or pass --top"})
        return res
    be, infos = bk.select("equiv", sec, cfg.root)
    res["detected"] = [{k: i.get(k) for k in ("name", "family", "available", "missing", "install", "tested")} for i in infos]
    if be is None:
        res.update({"status": "tool_missing", "verdict": "INCONCLUSIVE",
                    "detail": "no equivalence checker found. Install yosys (" + bk.OSS_HINT + "), or put Conformal (lec) or Formality (fm_shell) on PATH"})
        _write(cfg, res)
        return res
    res["backend"] = be.name
    code, top_dir = _git(cfg, "rev-parse", "--show-toplevel")
    if code != 0:
        res.update({"status": "error", "verdict": "INCONCLUSIVE", "detail": "not a git repository; equivalence needs a base commit"})
        return res
    prefix = cfg.root.resolve().relative_to(Path(top_dir.strip()).resolve())
    work = cfg.build_dir / "equiv"
    shutil.rmtree(work, ignore_errors=True)
    (work / "gold").mkdir(parents=True)
    (work / "gate").mkdir(parents=True)
    gold, gate = [], []
    for f in srcs:
        rel = (prefix / f).as_posix() if str(prefix) != "." else f
        p = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=cfg.root, capture_output=True, text=True)
        name = f.replace("/", "__")
        if p.returncode != 0:
            res["gaps"].append(f"{f} does not exist at {base}; it is compared as new logic only if another file instantiates it")
        else:
            g = work / "gold" / name
            g.write_text(sva.shim(p.stdout, sva.extract(p.stdout, f), translate=False, strip=True))
            gold.append(g)
        cur = cfg.root / f
        if cur.exists():
            t = cur.read_text(errors="replace")
            d = work / "gate" / name
            d.write_text(sva.shim(t, sva.extract(t, f), translate=False, strip=True))
            gate.append(d)
    if not gold or not gate:
        res.update({"status": "error", "verdict": "INCONCLUSIVE", "detail": "nothing to compare (no source at base or in the working tree)"})
        _write(cfg, res)
        return res
    job = bk.Job(mode="equiv", top=top, workdir=work, gold=gold, gate=gate, depth=int(sec.get("depth", 10)),
                 defines=list(sec.get("defines", [])), include_dirs=list(sec.get("include_dirs", [])), timeout=int(sec.get("timeout", 900)))
    r = be.run(job)
    res.update({"status": r["status"], "unproven": r.get("unproven", []), "cells": r.get("cells"), "log": _rel(cfg, r.get("log")),
                "seconds": r.get("seconds")})
    if r.get("detail"):
        res["detail"] = r["detail"]
    res["verdict"] = {"equivalent": "PASS", "not_proven": "FAIL"}.get(r["status"], "INCONCLUSIVE")
    _write(cfg, res)
    return res


def _rel(cfg: Config, p: str | None) -> str | None:
    if not p:
        return p
    try:
        return str(Path(p).resolve().relative_to(cfg.root))
    except ValueError:
        return p


def _write(cfg: Config, res: dict) -> None:
    (cfg.evidence_dir / "equiv.json").write_text(json.dumps(res, indent=2))


def text_report(r: dict) -> str:
    lines = [f"EQUIV: {r['verdict']}   {r.get('status')}   backend: {r.get('backend') or 'none'}   base: {r.get('base')}"]
    if r.get("cells"):
        lines.append(f"  {r['cells']['proven']} points proven, {r['cells']['unproven']} unproven")
    for u in r.get("unproven", []):
        lines.append(f"  unproven: {u}")
    if r.get("detail"):
        lines.append(f"  {r['detail']}")
    for g in r.get("gaps", []):
        lines.append(f"  gap: {g}")
    return "\n".join(lines)
