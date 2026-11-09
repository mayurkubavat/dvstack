"""control-eda command line. Every subcommand takes --json."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import cov as covmod
from . import doctor as doctormod
from . import door as doormod
from . import evidence as evmod
from . import install as installmod
from . import probe as probemod
from . import lint as lintmod
from . import sim as simmod
from . import verify as verifymod
from . import vplan as vplanmod
from . import wave as wavemod
from . import rules as rulesmod
from . import review as reviewmod
from . import triage as triagemod
from .formal import check as formalmod
from .formal import equiv as equivmod
from .util import emit, fail, parse_time

EPILOG = """
examples:
  control-eda doctor --json
  control-eda lint --json
  control-eda sim --test tb_fifo --seed 42 --cov --trace --json
  control-eda sim --test tb_fifo --seed 42 --define FIFO_BUG_DOUBLE_POP --expect-fail --json
  control-eda cov report --json
  control-eda wave-query evidence/sim/tb_fifo_42.vcd --at 18450ns --window 8 --signals 'tb_fifo.dut.*'
  control-eda vplan lookup --query "full flag" --json
  control-eda verify --claim "[measured] fix closes the hang" --json
  control-eda evidence bundle --claim "[measured] 6/6 seeds pass" --json > evidence/worktree.json
  control-eda evidence check evidence/worktree.json
  control-eda formal detect --json
  control-eda formal check --json
  control-eda equiv --base main --json
  control-eda rules test --json
  control-eda review scan --base main --json
  control-eda review merge reviews/*.json --json
  control-eda triage run --regress 20 --json
  control-eda triage run --logs 'nightly/*.log' --main origin/main --json
  control-eda triage record --sig 3f2a9c1e07 --status open --link https://github.com/org/repo/pull/12
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="control-eda", description="Agent-friendly door to EDA tools. JSON on every command.", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"control-eda {__version__}")
    p.add_argument("-C", dest="chdir", help="run as if started in this directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    common(sub.add_parser("doctor", help="tool versions, licenses, dv.toml sanity"))

    sp = common(sub.add_parser("lint", help="lint RTL and TB (verilator --lint-only or verible)"))
    sp.add_argument("--files", nargs="*", help="lint only these files")

    sp = common(sub.add_parser("sim", help="compile and run one test with one seed"))
    sp.add_argument("--test", required=True)
    sp.add_argument("--seed", default="1", help="integer or 'random'")
    sp.add_argument("--define", action="append", default=[], help="+define (repeatable), e.g. a fault-injection define")
    sp.add_argument("--cov", action="store_true", help="collect coverage")
    sp.add_argument("--trace", action="store_true", help="dump a VCD")
    sp.add_argument("--expect-fail", action="store_true", help="fault injection: the run must FAIL to be ok")
    sp.add_argument("--compile-only", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--plusarg", action="append", default=[], help="override or add a plusarg, e.g. +cycles=40 (repeatable)")

    sp = common(sub.add_parser("cov", help="coverage: report | diff | clean"))
    sp.add_argument("action", choices=["report", "merge", "diff", "clean"])
    sp.add_argument("--base", help="base cov report for diff (default evidence/cov.base.json)")
    sp.add_argument("--uncovered", action="store_true", help="report: list uncovered points")

    sp = common(sub.add_parser("wave-query", help="JSON cycle table of signals around a time, from VCD/FST"))
    sp.add_argument("dump")
    sp.add_argument("--at", required=True, help="time, e.g. 18450ns")
    sp.add_argument("--window", type=int, default=10, help="clock edges each side")
    sp.add_argument("--signals", required=True, help="comma-separated globs, matched on full path or leaf name")
    sp.add_argument("--clock", help="clock signal path (auto-detects clk/clock)")

    sp = common(sub.add_parser("vplan", help="check | lookup the verification plan"))
    sp.add_argument("action", choices=["check", "lookup"])
    sp.add_argument("--files", nargs="*")
    sp.add_argument("--query")
    sp.add_argument("--block")

    sp = common(sub.add_parser("scope", help="changed RTL/TB files vs a base"))
    sp.add_argument("--base")

    sp = common(sub.add_parser("door", help="classify the change: one-way or two-way"))
    sp.add_argument("action", choices=["classify"])
    sp.add_argument("--base")

    sp = common(sub.add_parser("verify", help="the whole evidence gate: lint, sims, faults, coverage, bundle"))
    sp.add_argument("--base", help="base ref for scope and door classification")
    sp.add_argument("--files", nargs="*", help="override the changed-file scope")
    sp.add_argument("--claim", action="append", default=[], help='"[measured|inferred|guessed] text" (repeatable)')
    sp.add_argument("--gap", action="append", default=[])
    sp.add_argument("--door", choices=["two-way", "one-way"])
    sp.add_argument("--keep", action="store_true", help="keep previous sim results instead of starting clean")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--equiv", metavar="BASE", help="also prove equivalence against BASE (for refactors)")
    sp.add_argument("--no-formal", action="store_true", help="skip [formal] even if configured (recorded as a gap)")

    sp = common(sub.add_parser("formal", help="formal: detect tools | check (prove, cover witnesses, fault counterexamples, assume audit)"))
    sp.add_argument("action", choices=["detect", "check"])
    sp.add_argument("--backend", help="force a backend (default: [formal].backend or auto)")
    sp.add_argument("--depth", type=int, help="override [formal].depth for an exploratory run (the gate uses dv.toml)")
    sp.add_argument("--only", help="comma list of prove,cover,mutate,assumes")

    sp = common(sub.add_parser("equiv", help="prove the working tree is equivalent to a base commit (behavior-preserving refactor)"))
    sp.add_argument("--base", required=True, help="git ref for the golden RTL, e.g. HEAD or main")
    sp.add_argument("--files", nargs="*", help="override [equiv].sources")
    sp.add_argument("--top")
    sp.add_argument("--backend")

    sp = common(sub.add_parser("rules", help="project rules from review comments (rules.yaml): list | check | test"))
    sp.add_argument("action", choices=["list", "check", "test"])
    sp.add_argument("--files", nargs="*")

    sp = common(sub.add_parser("review", help="RTL review evidence: scan (CDC, reset, X-prop; no EDA tool needed) | merge reviewer findings"))
    sp.add_argument("action", choices=["scan", "merge"])
    sp.add_argument("reviews", nargs="*", help="merge: reviewer JSON files (default reviews/*.json)")
    sp.add_argument("--base", help="scan: only files changed since this ref")
    sp.add_argument("--files", nargs="*")
    sp.add_argument("--window", type=int, help="merge: lines apart that still count as the same finding (default [review].line_window or 3)")
    sp.add_argument("--no-scan", action="store_true", help="merge: ignore evidence/review-scan.json")

    sp = common(sub.add_parser("triage", help="regression triage: run (bucket failures by signature, check main, shrink) | record | list"))
    sp.add_argument("action", choices=["run", "record", "list"])
    sp.add_argument("--logs", nargs="*", help="run: triage these simulator logs (globs) instead of evidence/sim")
    sp.add_argument("--sim-dir", help="run: read failing runs from this directory of control-eda sim JSON (default evidence/sim)")
    sp.add_argument("--regress", type=int, metavar="N", help="run: first run every test on the fixed seeds plus N random seeds")
    sp.add_argument("--tests", nargs="*", help="run --regress: only these tests")
    sp.add_argument("--define", action="append", default=[], help="run --regress: +define for every run")
    sp.add_argument("--main", help="run: ref to recheck failures on (default [triage].main or main)")
    sp.add_argument("--no-main", action="store_true", help="run: skip the rerun on main")
    sp.add_argument("--no-shrink", action="store_true", help="run: skip shrinking")
    sp.add_argument("--no-repro", action="store_true", help="run: skip reproducing here")
    sp.add_argument("--sig", help="record: signature id")
    sp.add_argument("--status", choices=list(triagemod.STORE_STATUSES), help="record: open (owned by a PR or issue) | fixed (merged)")
    sp.add_argument("--link", help="record: PR or issue URL")
    sp.add_argument("--note")
    sp.add_argument("--text", help="record: normalized error line, for a signature not in triage/latest.json")

    sp = common(sub.add_parser("install", help="install the dv plugin into a Cursor or Antigravity project folder"))
    sp.add_argument("--env", choices=["cursor", "antigravity"], required=True)
    sp.add_argument("--plugins", default="all", help="comma list of plugins to install (default: all, e.g. dv,formal)")
    sp.add_argument("--project", default=".", help="project root (default: current directory)")
    sp.add_argument("--global", dest="global_scope", action="store_true", help="user-level install instead of the project")
    sp.add_argument("--probe", action="store_true", help="also record raw hook payloads to evidence/hook-probe.jsonl")
    sp.add_argument("--dry-run", action="store_true")

    sp = common(sub.add_parser("probe", help="capture raw hook payloads from a Cursor/Antigravity session: on | off | report"))
    sp.add_argument("action", choices=["on", "off", "report"])
    sp.add_argument("--project", default=".")

    sp = common(sub.add_parser("evidence", help="bundle | check the evidence file"))
    sp.add_argument("action", choices=["bundle", "check"])
    sp.add_argument("path", nargs="?", help="for check: the bundle to validate")
    sp.add_argument("--base")
    sp.add_argument("--block")
    sp.add_argument("--door", choices=["two-way", "one-way"], help="override the classifier")
    sp.add_argument("--claim", action="append", default=[], help='"[measured|inferred|guessed] text" (repeatable)')
    sp.add_argument("--gap", action="append", default=[])
    sp.add_argument("--out", help="also write the bundle here")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    start = Path(args.chdir) if args.chdir else None
    try:
        if args.cmd == "doctor":
            d = doctormod.run_doctor(start)
            emit(d, as_json, doctormod.text_report(d))
            sys.exit(0 if d["ok"] else 1)
        if args.cmd == "probe":
            root = Path(args.project).resolve()
            r = {"on": probemod.enable, "off": probemod.disable, "report": probemod.report}[args.action](root)
            txt = json.dumps(r, indent=2) if args.action == "report" else ("probe on: payloads go to evidence/hook-probe.jsonl" if r.get("enabled") else "probe off")
            emit(r, as_json, txt)
            sys.exit(0 if r.get("ok") else 1)
        if args.cmd == "install":
            plugins = None if args.plugins == "all" else [x.strip() for x in args.plugins.split(",") if x.strip()]
            r = installmod.install(args.env, Path(args.project).resolve(), args.global_scope, args.dry_run, probe=args.probe, plugins=plugins)
            emit(r, as_json, f"{'would install' if args.dry_run else 'installed'} {', '.join(r['plugins'])} for {args.env} under {r['root']}\n" + "\n".join("  " + a for a in r["actions"]) + "\n" + "\n".join("note: " + n for n in r["notes"]))
            return
        if args.cmd == "wave-query":
            r = wavemod.query(Path(args.dump), parse_time(args.at), args.window, [s.strip() for s in args.signals.split(",")], args.clock)
            emit(r, as_json, wavemod.table(r))
            sys.exit(0 if r.get("ok") else 1)
        if args.cmd == "evidence" and args.action == "check":
            if not args.path:
                fail("evidence check needs a path", "control-eda evidence check evidence/<sha>.json", as_json)
            r = evmod.check(Path(args.path))
            emit(r, as_json, f"{r.get('verdict')} ({'valid' if r['ok'] else 'INVALID'})" + "".join(f"\n  - {x}" for x in r.get("problems", [])))
            sys.exit(0 if r["ok"] else 1)
        if args.cmd == "formal" and args.action == "detect":
            try:
                c = cfgmod.load(start)
            except cfgmod.ConfigError:
                c = None
            r = formalmod.detect(c)
            emit(r, as_json, _detect_text(r))
            return
        cfg = cfgmod.load(start)
        if args.cmd == "formal":
            prog = None if as_json else (lambda m: print(f"  ... {m}", file=sys.stderr))
            only = [x.strip() for x in args.only.split(",")] if args.only else None
            r = formalmod.run_check(cfg, args.backend, args.depth, only, prog)
            r["reasons"] = formalmod.reasons(r) if r["verdict"] != "PASS" else []
            emit(r, as_json, formalmod.text_report(r))
            sys.exit(0 if r["verdict"] == "PASS" else 1)
        if args.cmd == "equiv":
            r = equivmod.run_equiv(cfg, args.base, args.files, args.backend, args.top)
            emit(r, as_json, equivmod.text_report(r))
            sys.exit(0 if r["verdict"] == "PASS" else 1)
        if args.cmd == "review":
            if args.action == "scan":
                r = reviewmod.scan(cfg, args.files, args.base)
                txt = f"review scan: {len(r['findings'])} findings ({r['high']} high) in {len(r['files'])} files" + "".join(
                    f"\n  {f['file']}:{f['line']} {f['category']}/{f['severity']} [{f['rule']}] {f['message']}" for f in r["findings"]) + "".join(
                    f"\n  tool {t['name']}: {'ok' if t['available'] else 'missing (' + t.get('install', '') + ')'}" for t in r["tools"])
                emit(r, as_json, txt)
                return
            paths = [Path(p) for p in args.reviews] or sorted((cfg.root / "reviews").glob("*.json"))
            if not paths:
                fail("no reviewer files", "write reviews/<reviewer>.json (schemas/review.schema.json) or pass paths", as_json)
            r = reviewmod.merge(cfg, paths, args.window, not args.no_scan)
            emit(r, as_json, reviewmod.markdown(r))
            sys.exit(0 if r["ok"] else 1)
        if args.cmd == "triage":
            if args.action == "run":
                prog = None if as_json else (lambda m: print(f"  ... {m}", file=sys.stderr))
                r = triagemod.run(cfg, logs=args.logs, sim_dir=Path(args.sim_dir) if args.sim_dir else None, regress_n=args.regress, tests=args.tests,
                                  defines=args.define, main_ref=args.main, do_main=not args.no_main, do_shrink=not args.no_shrink,
                                  do_repro=not args.no_repro, progress=prog)
                emit(r, as_json, triagemod.text_report(r))
                sys.exit(0 if r["ok"] else 1)
            if args.action == "record":
                if not (args.sig and args.status):
                    fail("triage record needs --sig and --status", "control-eda triage record --sig <id> --status open --link <url>", as_json)
                try:
                    r = triagemod.record(cfg, args.sig, args.status, args.link, args.note, args.text)
                except ValueError as e:
                    fail(str(e), as_json=as_json)
                emit(r, as_json, f"{r['signature']}: {r['entry']['status']}" + (f" ({r['entry'].get('link')})" if r["entry"].get("link") else "") + f" -> {r['store']}")
                return
            st = triagemod.load_store(cfg)
            emit({"ok": True, **st}, as_json, "\n".join(f"{k} {v.get('status'):6} {v.get('text')}" + (f"  {v['link']}" if v.get("link") else "") for k, v in st["signatures"].items()) or "no known signatures")
            return
        if args.cmd == "rules":
            if args.action == "list":
                r = rulesmod.list_rules(cfg)
                txt = "\n".join(f"{x['id']:24} {x['kind']:5} {x.get('message') or x.get('assertion') or ''}" for x in r.get("rules", [])) or "no rules.yaml"
            elif args.action == "check":
                r = rulesmod.check(cfg, args.files)
                txt = f"{r['violation_count']} violations" + "".join(f"\n  {v['file']}:{v['line']}: [{v['rule']}] {v['message']}" for v in r["violations"])
            else:
                r = rulesmod.test(cfg)
                txt = "\n".join(f"{x['id']:24} {x['state']}" + "".join(f"\n    - {p}" for p in x.get("problems", [])) for x in r.get("rules", [])) or "no rules.yaml"
            emit(r, as_json, txt)
            sys.exit(0 if r.get("ok") else 1)
        if args.cmd == "lint":
            r = lintmod.lint(cfg, args.files)
            (cfg.evidence_dir / "lint.json").write_text(json.dumps(r, indent=2))
            txt = f"{r.get('tool')}: {r.get('error_count', '?')} errors, {r.get('warning_count', '?')} warnings" + "".join(f"\n  {e['file']}:{e['line']}: {e['message']}" for e in r.get("errors", []))
            emit(r, as_json, txt if r.get("ok") is not None else json.dumps(r))
            sys.exit(0 if r.get("ok") else 1)
        if args.cmd == "sim":
            r = simmod.run_test(cfg, args.test, args.seed, args.define, args.cov, args.trace, args.expect_fail, args.dry_run, args.compile_only, args.plusarg)
            if not args.dry_run and not args.compile_only and r.get("status") in ("pass", "fail"):
                Path(cfg.root / r["log"]).with_suffix(".json").write_text(json.dumps(r, indent=2))
            txt = f"{r['test']} seed={r['seed']} {r.get('status','?').upper()}" + (" (expected fail: ok)" if args.expect_fail and r.get("ok") else "") + (f"\n  {r['first_failure']}" if r.get("first_failure") else "") + (f"\n  wave: {r['wave']}" if r.get("wave") else "")
            emit(r, as_json, txt)
            sys.exit(0 if r.get("ok") else 1)
        if args.cmd == "cov":
            if args.action in ("report", "merge"):
                r = covmod.merge_project(cfg)
                if not args.uncovered:
                    r.pop("uncovered", None)
                txt = f"coverage {r.get('percent')}% ({r.get('covered')}/{r.get('total')} points)" + "".join(f"\n  {p['name']}: {p['count']} hits" for p in r.get("user_points", []))
                emit(r, as_json, txt if r.get("ok") else json.dumps(r))
                sys.exit(0 if r.get("ok") else 1)
            if args.action == "diff":
                base = Path(args.base) if args.base else cfg.evidence_dir / "cov.base.json"
                r = covmod.diff(cfg, base)
                emit(r, as_json, f"coverage {r.get('before')} -> {r.get('after')} (delta {r.get('delta')})")
                sys.exit(0 if r.get("ok") and not r.get("dropped") else 1)
            if args.action == "clean":
                for f in covmod.cov_files(cfg):
                    f.unlink()
                emit({"ok": True}, as_json, "coverage data removed")
                return
        if args.cmd == "vplan":
            r = vplanmod.check(cfg) if args.action == "check" else vplanmod.lookup(cfg, args.files, args.query, args.block)
            if args.action == "check":
                txt = f"vplan: {r.get('features')} features, {len(r.get('problems', []))} problems, {len(r.get('orphan_rtl', []))} orphan RTL" + "".join(f"\n  - {x}" for x in r.get("problems", [])) + "".join(f"\n  orphan: {x}" for x in r.get("orphan_rtl", []))
            else:
                txt = "\n".join(f"{f['id']}: {f['summary']}  tests={f.get('tests')} faults={f.get('faults')}" for f in r.get("matches", [])) or "no matches"
            emit(r, as_json, txt)
            sys.exit(0 if r.get("ok") else 1)
        if args.cmd == "scope":
            files = evmod.changed_files(cfg, args.base)
            emit({"ok": True, "base": args.base, "files": files}, as_json, "\n".join(files) or "(no HDL changes)")
            return
        if args.cmd == "door":
            r = doormod.classify(cfg, args.base)
            emit(r, as_json, r["door"] + "".join(f"\n  {x}" for x in r["reasons"]))
            return
        if args.cmd == "verify":
            prog = None if (as_json or args.quiet) else (lambda m: print(f"  ... {m}", file=sys.stderr))
            r = verifymod.run_verify(cfg, args.base, args.files, args.claim, args.gap, args.door, args.keep, prog, equiv_base=args.equiv, formal=not args.no_formal)
            emit(r, as_json, verifymod.text_report(r))
            sys.exit(0 if r["ok"] else 1)
        if args.cmd == "evidence":
            door = args.door or doormod.classify(cfg, args.base)["door"]
            b = evmod.bundle(cfg, args.base, args.block, door, args.claim, args.gap)
            if args.out:
                Path(args.out).write_text(json.dumps(b, indent=2))
            print(json.dumps(b, indent=2))
            return
    except cfgmod.ConfigError as e:
        fail(str(e), as_json=as_json)
    except FileNotFoundError as e:
        fail(str(e), as_json=as_json)
    except RuntimeError as e:
        fail(str(e), as_json=as_json)


def _detect_text(r: dict) -> str:
    lines = ["formal backends:"]
    for b in r["formal"]:
        lines.append(f"  {'ok     ' if b['available'] else 'missing'}  {b['name']:10} {b['title']}" + ("" if b["available"] else f"  (needs {', '.join(b['missing'])})"))
    lines.append("equivalence backends:")
    for b in r["equiv"]:
        lines.append(f"  {'ok     ' if b['available'] else 'missing'}  {b['name']:10} {b['title']}" + ("" if b["available"] else f"  (needs {', '.join(b['missing'])})"))
    lines.append(f"solvers: {', '.join(r['solvers']) or 'none'}")
    lines.append(f"selected: formal={r['selected']['formal'] or 'NONE'} equiv={r['selected']['equiv'] or 'NONE'}")
    lines += [f"hint: {h}" for h in r["hints"]]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
