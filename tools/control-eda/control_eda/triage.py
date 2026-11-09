"""triage: group regression failures by error signature so people only see new kinds of failure.

    control-eda triage run --json                   # failures already in evidence/sim
    control-eda triage run --regress 20 --json      # run every test on fixed + 20 random seeds first
    control-eda triage run --logs 'nightly/*.log'   # logs from any simulator (VCS, Xcelium, Questa ...)
    control-eda triage record --sig 3f2a9c1e07 --status open --link <draft PR url>

For each signature bucket, `run`:
  1. normalizes the first error line (times, values, line numbers, paths) into a signature,
  2. looks it up in the project's signature store (triage/signatures.json): new, known or regressed,
  3. reproduces one failing seed here, then reruns it on `main` in a temporary git worktree
     to see whether main already fixed it,
  4. shrinks the repro by bisecting a length plusarg ([triage].shrink_plusarg, e.g. cycles)
     to the shortest run that still fails with the same signature,
  5. writes triage/latest.json and triage/report.md, with a draft-PR title and body for each
     bucket a human needs to see.

Every step that needs a tool degrades: no simulator means the reproduce, main and shrink
fields say `tool_missing` with an install hint; no git means the main check says
`not_available`. Signatures and the store need nothing but Python.
"""
from __future__ import annotations

import glob
import hashlib
import json
import random
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfgmod
from . import sim as simmod
from .config import Config
from .util import which  # honours CONTROL_EDA_HIDE_TOOLS

STORE_STATUSES = ("open", "fixed")

# Applied in order after any project [[triage.normalize]] rules. They remove what
# changes from seed to seed (times, data values, line numbers) and keep what names
# the failure (the check, the signal, the hierarchy).
NORMALIZE = [
    (r"^\s*\[\s*\d+(?:\.\d+)?\s*(?:fs|ps|ns|us|ms|s)?\s*\]\s*", ""),                 # Verilator "[75] "
    (r"@\s*\d+(?:\.\d+)?\s*(?:fs|ps|ns|us|ms|s)?\b", "@<t>"),                          # "@ 18450ns"
    (r"\b(?:at\s+)?time\s*[:=]?\s*\d+(?:\.\d+)?\s*(?:fs|ps|ns|us|ms|s)?\b", "time <t>"),
    (r"(?:[A-Za-z]:)?(?:[\w.\-~$]*/)+([\w.\-]+)", r"\1"),                               # paths -> basename
    (r"(\.\w+):\d+(?::\d+)?", r"\1:<l>"),                                               # fifo.sv:59 / :59:3
    (r"(\.\w+)\(\d+\)", r"\1(<l>)"),                                                    # UVM file.sv(123)
    (r"\bTOP\.", ""),                                                                   # Verilator hierarchy prefix
    (r"\b\d+'[sS]?[bodhBODH][0-9a-fA-FxXzZ_?]+", "<v>"),                               # 8'hfe
    (r"\b0x[0-9a-fA-F]+\b", "<v>"),
    (r"(=\s*)[0-9a-fA-FxXzZ_.\-]+\b", r"\1<v>"),                                        # key=value
    (r"\b\d+(?:\.\d+)?\b", "<n>"),
    (r"\s+", " "),
]


# ---------------------------------------------------------------- signatures

def tcfg(cfg: Config) -> dict:
    return cfg.raw.get("triage", {})


def normalize(line: str, project_rules: list[dict] | None = None) -> str:
    s = line.strip()
    for rule in project_rules or []:
        s = re.sub(rule["pattern"], rule.get("replace", ""), s)
    for pat, rep in NORMALIZE:
        s = re.sub(pat, rep, s)
    return s.strip()


def classify(line: str | None, exit_code: int | None = None, status: str | None = None) -> str:
    if status == "error":
        return "build"
    if not line:
        return "timeout" if exit_code == 124 else "no_pass_line"
    low = line.lower()
    if "assert" in low:
        return "assertion"
    if "uvm_fatal" in low or "fatal" in low:
        return "fatal"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    return "error"


def signature(fail: dict, project_rules: list[dict] | None = None) -> dict:
    """{id, text, kind} for one failing run. The test name is not part of it: one bug seen by
    three tests is one bucket."""
    line = fail.get("first_failure")
    kind = classify(line, fail.get("exit_code"), fail.get("status"))
    if kind == "build":
        text = "build failed: " + normalize((fail.get("build") or {}).get("error") or "unknown", project_rules)
    elif not line:
        text = "timeout" if kind == "timeout" else f"no pass line, exit code {fail.get('exit_code')}"
    else:
        text = normalize(line, project_rules)
    return {"id": hashlib.sha1(text.encode()).hexdigest()[:10], "text": text, "kind": kind}


# ---------------------------------------------------------------- collecting failures

def from_evidence(cfg: Config, sim_dir: Path | None = None) -> list[dict]:
    """Failing runs recorded by `control-eda sim` / `verify`. Expected failures (fault injection) are not failures."""
    d = sim_dir or cfg.evidence_dir / "sim"
    out = []
    for meta in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            r = json.loads(meta.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict) or "test" not in r or r.get("expect_fail"):
            continue
        if r.get("status") in ("fail", "error"):
            out.append({k: r.get(k) for k in ("test", "seed", "defines", "plusargs", "status", "exit_code", "first_failure", "fail_time", "log", "build")}
                       | {"source": "evidence"})
    return out


def from_logs(cfg: Config, patterns: list[str]) -> list[dict]:
    """Logs from any simulator. test and seed come from the file name ([triage].log_name, a regex with
    named groups test and seed) or, failing that, from `seed=N` in the log; the pass/fail decision uses
    dv.toml [sim].pass_pattern / fail_pattern, the same as `control-eda sim`."""
    t = tcfg(cfg)
    name_re = re.compile(t.get("log_name", r"(?P<test>.+?)[_.-](?:seed[_=-]?)?(?P<seed>\d+)$"))
    seed_re = re.compile(t.get("log_seed", r"\bseed\s*[=:]\s*(\d+)"), re.I)
    pass_re = re.compile(cfg.sim.get("pass_pattern", "TEST PASSED"))
    fail_re = re.compile(cfg.sim.get("fail_pattern", "TEST FAILED|ERROR|UVM_FATAL|%Error|Assertion failed"))
    out = []
    paths: list[Path] = []
    for pat in patterns:
        full = pat if Path(pat).is_absolute() else str(cfg.root / pat)
        paths += [Path(x) for x in sorted(glob.glob(full, recursive=True))]
    for log in paths:
        if not log.is_file():
            continue
        text = log.read_text(errors="replace")
        first = next((l.strip() for l in text.splitlines() if fail_re.search(l)), None)
        if pass_re.search(text) and not first:
            continue
        m = name_re.search(log.stem)
        test = m.group("test") if m else log.stem
        seed = int(m.group("seed")) if m else None
        if seed is None:
            sm = seed_re.search(text)
            seed = int(sm.group(1)) if sm else None
        tm = simmod.FAIL_TIME_RE.search(first or "")
        out.append({"test": test, "seed": seed, "defines": [], "plusargs": [], "status": "fail", "exit_code": None, "first_failure": first,
                    "fail_time": ((tm.group(1) or tm.group(3)) + (tm.group(2) or tm.group(4) or "").strip()) if tm else None,
                    "log": str(log.relative_to(cfg.root)) if cfg.root in log.parents else str(log), "source": "log"})
    return out


def regress(cfg: Config, tests: list[str], n_random: int, defines: list[str], progress=None) -> tuple[list[dict], dict]:
    """Run each test on the fixed seeds plus n_random fresh ones. Returns (failures, summary)."""
    say = progress or (lambda *_: None)
    seeds = list(cfg.sim.get("seeds", [1]))
    rng = random.Random()
    seeds += [rng.randint(1, 2**31 - 1) for _ in range(n_random)]
    fails, runs, missing = [], 0, None
    miss = _sim_missing(cfg)
    if miss:
        return [], {"tests": tests, "seeds": seeds, "runs": 0, "failed": 0, "tool_missing": {"error": miss["detail"], "hint": miss.get("install")}}
    for t in tests:
        for s in seeds:
            say(f"regress {t} seed {s}")
            r = simmod.run_test(cfg, t, s, defines=defines)
            runs += 1
            if r.get("status") == "error" and "not installed" in str((r.get("build") or {}).get("error")):
                missing = r["build"]
                break
            (cfg.root / r["log"]).with_suffix(".json").write_text(json.dumps(r, indent=2))
            if r.get("status") in ("fail", "error"):
                fails.append(r | {"source": "regress"})
        if missing:
            break
    return fails, {"tests": tests, "seeds": seeds, "runs": runs, "failed": len(fails), "tool_missing": missing}


# ---------------------------------------------------------------- the signature store

def store_path(cfg: Config) -> Path:
    return cfg.root / tcfg(cfg).get("store", "triage/signatures.json")


def load_store(cfg: Config) -> dict:
    p = store_path(cfg)
    if not p.exists():
        return {"version": 1, "signatures": {}}
    data = json.loads(p.read_text())
    data.setdefault("signatures", {})
    return data


def record(cfg: Config, sig: str, status: str, link: str | None = None, note: str | None = None, text: str | None = None) -> dict:
    """Add or update one signature. `open`: someone owns it (a draft PR or an issue); `fixed`: a fix merged,
    so seeing it again is a regression. The store only decides what people are shown; it never changes
    the gate, which still fails on every failing seed."""
    if status not in STORE_STATUSES:
        raise ValueError(f"status must be one of {STORE_STATUSES}")
    store = load_store(cfg)
    entry = store["signatures"].get(sig)
    if entry is None:
        latest = cfg.root / "triage" / "latest.json"
        known = {}
        if latest.exists():
            known = {b["signature"]: b for b in json.loads(latest.read_text()).get("buckets", [])}
        if sig not in known and not text:
            raise ValueError(f"signature {sig} is not in triage/latest.json; pass --text with its normalized error line")
        b = known.get(sig, {})
        entry = {"text": text or b.get("text"), "kind": b.get("kind"), "first_seen": _now()}
    entry.update({"status": status, "updated": _now()})
    if link:
        entry["link"] = link
    if note:
        entry["note"] = note
    store["signatures"][sig] = entry
    p = store_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, indent=2) + "\n")
    return {"ok": True, "signature": sig, "entry": entry, "store": str(p.relative_to(cfg.root))}


# ---------------------------------------------------------------- git helpers

def _git(cwd: Path, *args: str) -> tuple[int, str]:
    if not which("git"):
        return 127, "git not installed"
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr).strip()


def default_main(cfg: Config) -> str:
    """origin/main when the remote ref exists (a nightly compares against what is upstream), else main."""
    code, _ = _git(cfg.root, "rev-parse", "--verify", "--quiet", "origin/main^{commit}")
    return "origin/main" if code == 0 else "main"


class MainTree:
    """`ref` checked out in a temporary worktree, created on first use and removed by close()."""

    def __init__(self, cfg: Config, ref: str):
        self.cfg, self.ref = cfg, ref
        self.path: Path | None = None
        self.cfg_main: Config | None = None
        self.error: str | None = None
        self.sha: str | None = None
        self.same = False
        code, top = _git(cfg.root, "rev-parse", "--show-toplevel")
        if code != 0:
            self.error = "not a git repository" if code != 127 else "git not installed"
            return
        self.top = Path(top)
        code, sha = _git(cfg.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if code != 0 or not sha:
            self.error = f"ref '{ref}' not found (try: git fetch origin {ref})"
            return
        self.sha = sha
        _, head = _git(cfg.root, "rev-parse", "HEAD")
        rel = cfg.root.resolve().relative_to(self.top.resolve())
        srcs = [str(rel / s) for s in cfg.sources] or [str(rel)]
        _, dirty = _git(self.top, "status", "--porcelain", "--", *srcs)
        self.same = sha == head and not dirty
        self.rel = rel

    def open(self) -> Config | None:
        if self.cfg_main or self.error:
            return self.cfg_main
        self._tmp = Path(tempfile.mkdtemp(prefix="dv-triage-"))
        self.path = self._tmp / "main"
        code, out = _git(self.top, "worktree", "add", "--detach", str(self.path), self.sha)
        if code != 0:
            self.error = f"git worktree add failed: {out[-300:]}"
            return None
        try:
            self.cfg_main = cfgmod.load(self.path / self.rel)
        except cfgmod.ConfigError as e:
            self.error = f"{self.ref} has no usable dv.toml: {e}"
        return self.cfg_main

    def commits_since_head(self) -> list[str]:
        if self.error or not self.sha:
            return []
        code, out = _git(self.top, "log", "--oneline", "-n", "10", f"HEAD..{self.sha}", "--", *([str(self.rel / s) for s in self.cfg.sources] or [str(self.rel)]))
        return out.splitlines() if code == 0 and out else []

    def close(self) -> None:
        if self.path is not None:
            _git(self.top, "worktree", "remove", "--force", str(self.path))
            _git(self.top, "worktree", "prune")
            shutil.rmtree(self._tmp, ignore_errors=True)
            self.path = None


# ---------------------------------------------------------------- per-bucket checks

def _sim_missing(cfg: Config) -> dict | None:
    tool = cfg.sim.get("tool", "verilator")
    exe = {"verilator": "verilator", "icarus": "iverilog"}.get(tool)
    if exe is None:
        return {"status": "unsupported", "detail": f"[sim].tool '{tool}' has no control-eda adapter; reruns need verilator or icarus"}
    if not which(exe):
        return {"status": "tool_missing", "detail": f"{exe} not installed", "install": f"apt-get install {exe} (or brew install {exe})"}
    return None


def _missing(miss: dict) -> dict:
    return {"state": miss["status"], **{k: v for k, v in miss.items() if k != "status"}}


def _rerun(cfg: Config, rep: dict, rules, plusargs: list[str] | None = None) -> dict:
    try:
        r = simmod.run_test(cfg, rep["test"], rep["seed"], defines=rep.get("defines") or [], plusargs=[*(rep.get("plusargs") or []), *(plusargs or [])])
    except cfgmod.ConfigError as e:
        return {"status": "error", "detail": str(e)}
    sig = signature(r, rules) if r.get("status") in ("fail", "error") else None
    return {"status": r.get("status"), "signature": sig["id"] if sig else None, "text": sig["text"] if sig else None,
            "fail_time": r.get("fail_time"), "log": r.get("log")}


def reproduce(cfg: Config, rep: dict, sig: str, rules) -> dict:
    if rep.get("seed") is None:
        return {"state": "not_available", "detail": "no seed known for this failure (set [triage].log_name or print seed=N in the log)"}
    miss = _sim_missing(cfg)
    if miss:
        return _missing(miss)
    r = _rerun(cfg, rep, rules)
    if r["status"] == "error" and r.get("detail"):
        return {"state": "not_available", "detail": r["detail"]}
    if r["signature"] == sig:
        return {"state": "reproduced", "fail_time": r["fail_time"], "log": r["log"]}
    if r["status"] == "pass":
        return {"state": "not_reproduced", "detail": "passes when rerun here: flaky, environment-dependent, or fixed in the working tree", "log": r["log"]}
    return {"state": "different_signature", "signature": r["signature"], "text": r["text"], "log": r["log"]}


def check_main(tree: MainTree, rep: dict, sig: str, rules) -> dict:
    out: dict = {"ref": tree.ref, "sha": tree.sha[:12] if tree.sha else None}
    if tree.error:
        return out | {"state": "not_available", "detail": tree.error}
    if tree.same:
        return out | {"state": "same_commit", "detail": f"the failing tree is {tree.ref}; nothing newer to compare"}
    if rep.get("seed") is None:
        return out | {"state": "not_available", "detail": "no seed known for this failure"}
    miss = _sim_missing(tree.cfg)
    if miss:
        return out | _missing(miss)
    mcfg = tree.open()
    if mcfg is None:
        return out | {"state": "not_available", "detail": tree.error}
    r = _rerun(mcfg, rep, rules)
    out["commits"] = tree.commits_since_head()
    if r["status"] == "error" and r.get("detail"):
        return out | {"state": "not_available", "detail": r["detail"]}
    if r["status"] == "pass":
        return out | {"state": "fixed"}
    if r["signature"] == sig:
        return out | {"state": "still_failing"}
    return out | {"state": "changed", "signature": r["signature"], "text": r["text"]}


def shrink(cfg: Config, rep: dict, sig: str, rules) -> dict:
    """Bisect [triage].shrink_plusarg down to the shortest run that fails with the same signature.
    With the same seed, a shorter run replays a prefix of the same stimulus, so this is a binary search."""
    t = tcfg(cfg)
    name = t.get("shrink_plusarg")
    if not name:
        return {"state": "not_configured", "detail": "set [triage].shrink_plusarg in dv.toml (the plusarg that sets test length, e.g. cycles)"}
    miss = _sim_missing(cfg)
    if miss:
        return _missing(miss)
    over = {simmod._plusarg_name(a): a for a in rep.get("plusargs") or []}
    raw = over[name].split("=", 1)[1] if name in over else simmod.plusarg_value(cfg, rep["test"], name)
    if raw is None or not str(raw).isdigit():
        return {"state": "not_available", "detail": f"test {rep['test']} sets no integer +{name}=N in dv.toml"}
    hi, lo = int(raw), int(t.get("shrink_min", 1))
    budget = int(t.get("shrink_max_runs", 16))
    runs, last_time = 0, None
    base = dict(rep, plusargs=[a for a in rep.get("plusargs") or [] if simmod._plusarg_name(a) != name])
    while lo < hi and runs < budget:
        mid = (lo + hi) // 2
        r = _rerun(cfg, base, rules, [f"+{name}={mid}"])
        runs += 1
        if r["signature"] == sig:
            hi, last_time = mid, r["fail_time"]
        else:
            lo = mid + 1
    cmd = f"control-eda sim --test {rep['test']} --seed {rep['seed']}" + "".join(f" --define {d}" for d in rep.get("defines") or []) + \
        "".join(f" --plusarg {a}" for a in base["plusargs"]) + f" --plusarg +{name}={hi} --trace"
    return {"state": "shrunk" if hi < int(raw) else "minimal", "plusarg": name, "from": int(raw), "to": hi, "runs": runs,
            "exhausted_budget": lo < hi, "fail_time": last_time, "command": cmd}


# ---------------------------------------------------------------- the run

def run(cfg: Config, logs: list[str] | None = None, sim_dir: Path | None = None, regress_n: int | None = None, tests: list[str] | None = None,
        defines: list[str] | None = None, main_ref: str | None = None, do_main: bool = True, do_shrink: bool = True, do_repro: bool = True,
        progress=None) -> dict:
    say = progress or (lambda *_: None)
    t = tcfg(cfg)
    rules = t.get("normalize", [])
    main_ref = main_ref or t.get("main") or default_main(cfg)
    regress_summary = None
    if regress_n is not None:
        fails, regress_summary = regress(cfg, tests or cfg.tests, regress_n, defines or [], say)
    elif logs:
        fails = from_logs(cfg, logs)
    else:
        fails = from_evidence(cfg, sim_dir)

    groups: dict[str, dict] = {}
    for f in fails:
        s = signature(f, rules)
        g = groups.setdefault(s["id"], {"signature": s["id"], "text": s["text"], "kind": s["kind"], "failures": []})
        g["failures"].append(f)

    store = load_store(cfg)["signatures"]
    tree = MainTree(cfg, main_ref) if do_main else None
    buckets = []
    try:
        for sid, g in groups.items():
            fl = g["failures"]
            # representative: the seeded failure that failed earliest, so the repro is short to begin with
            rep = min(fl, key=lambda f: (f.get("seed") is None, _ps(f.get("fail_time")), f.get("seed") or 0))
            known = store.get(sid)
            status = "new" if known is None else ("regressed" if known.get("status") == "fixed" else "known")
            b = {"signature": sid, "text": g["text"], "kind": g["kind"], "status": status, "count": len(fl),
                 "tests": sorted({f["test"] for f in fl}), "seeds": sorted({f["seed"] for f in fl if f.get("seed") is not None}),
                 "first_failure": rep.get("first_failure"),
                 "representative": {k: rep.get(k) for k in ("test", "seed", "defines", "plusargs", "fail_time", "log")},
                 "known": known}
            if status == "known":
                b.update({"reproduce": {"state": "skipped"}, "main": {"state": "skipped"}, "shrink": {"state": "skipped"}})
            else:
                say(f"{sid}: reproduce {rep['test']} seed {rep.get('seed')}")
                b["reproduce"] = reproduce(cfg, rep, sid, rules) if do_repro else {"state": "skipped"}
                if tree is not None:
                    say(f"{sid}: rerun on {main_ref}")
                    b["main"] = check_main(tree, rep, sid, rules)
                else:
                    b["main"] = {"state": "skipped"}
                if do_shrink and b["main"]["state"] != "fixed" and b["reproduce"]["state"] in ("reproduced", "skipped", "tool_missing", "unsupported"):
                    say(f"{sid}: shrink")
                    b["shrink"] = shrink(cfg, rep, sid, rules)
                else:
                    b["shrink"] = {"state": "skipped"}
            b["action"] = _action(b)
            if b["action"] == "draft_pr":
                b["pr"] = pr_draft(cfg, b)
            buckets.append(b)
    finally:
        if tree is not None:
            tree.close()

    order = {"regressed": 0, "new": 1, "known": 2}
    buckets.sort(key=lambda b: (order[b["status"]], b["main"].get("state") == "fixed", -b["count"]))
    needs_human = [b["signature"] for b in buckets if b["action"] in ("draft_pr", "human")]
    inconclusive = []
    if (regress_summary or {}).get("tool_missing"):
        tm = regress_summary["tool_missing"]
        inconclusive.append(f"regression not run: {tm.get('error')} ({tm.get('hint')})")
    verdict = "NEEDS_HUMAN" if needs_human else ("INCONCLUSIVE" if inconclusive else "CLEAN")
    report = {
        "version": 1, "ok": verdict == "CLEAN", "verdict": verdict, "inconclusive": inconclusive, "generated": _now(), "project": cfg.project.get("name"),
        "commit": _git(cfg.root, "rev-parse", "--short=12", "HEAD")[1] or None,
        "source": "regress" if regress_n is not None else ("logs" if logs else "evidence"),
        "regress": regress_summary, "main_ref": main_ref if do_main else None,
        "failures": len(fails), "signatures": len(buckets),
        "new": sum(b["status"] == "new" for b in buckets), "regressed": sum(b["status"] == "regressed" for b in buckets),
        "known": sum(b["status"] == "known" for b in buckets), "fixed_on_main": sum(b["main"].get("state") == "fixed" for b in buckets),
        "needs_human": needs_human, "buckets": buckets, "store": str(store_path(cfg).relative_to(cfg.root)),
    }
    out = cfg.root / "triage"
    out.mkdir(exist_ok=True)
    (out / "latest.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "report.md").write_text(markdown(report))
    report["report"] = "triage/report.md"
    return report


def _action(b: dict) -> str:
    if b["status"] == "known":
        return "none"
    if b["main"].get("state") == "fixed":
        return "none"           # main already has the fix; nothing to open
    if b["reproduce"].get("state") in ("not_reproduced", "different_signature"):
        return "human"          # flaky or unstable: a person decides, no PR from a repro that does not repro
    if b["status"] == "regressed":
        return "human"          # a fixed bug is back: reopen its owner's PR or issue
    return "draft_pr"


def pr_draft(cfg: Config, b: dict) -> dict:
    rep = b["representative"]
    sh = b.get("shrink") or {}
    cmd = sh.get("command") or (f"control-eda sim --test {rep['test']} --seed {rep['seed']}" + "".join(f" --define {d}" for d in rep.get("defines") or []) + " --trace")
    short = b["text"] if len(b["text"]) <= 60 else b["text"][:57] + "..."
    title = f"triage: {short} ({b['count']} failing run{'s' if b['count'] != 1 else ''})"
    m = b["main"]
    main_line = {"still_failing": f"still fails on `{m.get('ref')}` ({m.get('sha')})", "changed": f"fails differently on `{m.get('ref')}`: `{m.get('text')}`",
                 "same_commit": f"the failing commit is `{m.get('ref')}`"}.get(m.get("state"), f"not checked ({m.get('state')}: {m.get('detail', '')})")
    lines = [
        f"Signature `{b['signature']}` ({b['kind']}), first seen in this regression.", "",
        "```", b["text"], "```", "",
        "| | |", "| --- | --- |",
        f"| Failing runs | {b['count']} ({', '.join(b['tests'])}; seeds {', '.join(map(str, b['seeds'][:12]))}{' ...' if len(b['seeds']) > 12 else ''}) |",
        f"| First error | `{b['first_failure']}` |",
        f"| Reproduced here | {b['reproduce'].get('state')} |",
        f"| On main | {main_line} |",
    ]
    if sh.get("state") in ("shrunk", "minimal"):
        lines.append(f"| Shrunk | +{sh['plusarg']} {sh['from']} -> {sh['to']} in {sh['runs']} runs |")
    else:
        lines.append(f"| Shrunk | {sh.get('state')}{': ' + sh['detail'] if sh.get('detail') else ''} |")
    lines += ["", "Repro:", "", "```bash", cmd, "```", "",
              "Root cause: _filled in by dv:why-fail, with measured / inferred / guessed labels._", "",
              f"Generated by `control-eda triage run`. Record it with `control-eda triage record --sig {b['signature']} --status open --link <this PR>`."]
    return {"branch": f"{tcfg(cfg).get('branch_prefix', 'triage/')}{b['signature']}", "title": title, "body": "\n".join(lines), "repro": cmd}


def markdown(r: dict) -> str:
    lines = [f"# Regression triage: {r.get('project')} @ {r.get('commit')}", "",
             f"{r['failures']} failing runs in {r['signatures']} signatures: {r['new']} new, {r['regressed']} regressed, {r['known']} known, "
             f"{r['fixed_on_main']} already fixed on {r.get('main_ref') or 'main (not checked)'}. Verdict: **{r['verdict']}**.", ""]
    lines += [f"- {x}" for x in r.get("inconclusive", [])] + ([""] if r.get("inconclusive") else [])
    if not r["buckets"]:
        lines.append("No failures. Nothing to triage.")
        return "\n".join(lines) + "\n"
    lines += ["| Status | Signature | Error | Runs | On main | Shrink | Action |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for b in r["buckets"]:
        sh = b["shrink"]
        shrink_txt = f"{sh['from']} -> {sh['to']}" if sh.get("state") in ("shrunk", "minimal") else sh.get("state")
        link = (b.get("known") or {}).get("link")
        act = b["action"] + (f" ({link})" if link else "")
        lines.append(f"| {b['status']} | `{b['signature']}` | `{b['text'][:80]}` | {b['count']} | {b['main'].get('state')} | {shrink_txt} | {act} |")
    for b in r["buckets"]:
        if b.get("pr"):
            lines += ["", f"## {b['pr']['title']}", "", f"Branch `{b['pr']['branch']}`", "", b["pr"]["body"]]
    return "\n".join(lines) + "\n"


def text_report(r: dict) -> str:
    lines = [f"TRIAGE: {r['verdict']}  {r['failures']} failures, {r['signatures']} signatures "
             f"({r['new']} new, {r['regressed']} regressed, {r['known']} known, {r['fixed_on_main']} fixed on {r.get('main_ref') or '-'})"]
    lines += [f"  {x}" for x in r.get("inconclusive", [])]
    for b in r["buckets"]:
        sh = b["shrink"]
        lines.append(f"  {b['status']:9} {b['signature']}  x{b['count']:<3} main={b['main'].get('state'):13} "
                     f"shrink={(str(sh.get('from')) + '->' + str(sh.get('to'))) if sh.get('state') in ('shrunk', 'minimal') else sh.get('state')}  "
                     f"action={b['action']}\n            {b['text'][:100]}")
    lines.append(f"report: {r.get('report')}")
    return "\n".join(lines)


def _ps(t: str | None) -> float:
    if not t:
        return float("inf")
    m = re.match(r"(\d+(?:\.\d+)?)\s*(fs|ps|ns|us|ms|s)?", str(t))
    if not m:
        return float("inf")
    mult = {"fs": 1e-3, "ps": 1, "ns": 1e3, "us": 1e6, "ms": 1e9, "s": 1e12}.get(m.group(2) or "ps", 1)
    return float(m.group(1)) * mult


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
