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

    sp = common(sub.add_parser("install", help="install the dv plugin into a Cursor or Antigravity project folder"))
    sp.add_argument("--env", choices=["cursor", "antigravity"], required=True)
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
            r = installmod.install(args.env, Path(args.project).resolve(), args.global_scope, args.dry_run, probe=args.probe)
            emit(r, as_json, f"{'would install' if args.dry_run else 'installed'} dv for {args.env} under {r['root']}\n" + "\n".join("  " + a for a in r["actions"]) + "\n" + "\n".join("note: " + n for n in r["notes"]))
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
        cfg = cfgmod.load(start)
        if args.cmd == "lint":
            r = lintmod.lint(cfg, args.files)
            (cfg.evidence_dir / "lint.json").write_text(json.dumps(r, indent=2))
            txt = f"{r.get('tool')}: {r.get('error_count', '?')} errors, {r.get('warning_count', '?')} warnings" + "".join(f"\n  {e['file']}:{e['line']}: {e['message']}" for e in r.get("errors", []))
            emit(r, as_json, txt if r.get("ok") is not None else json.dumps(r))
            sys.exit(0 if r.get("ok") else 1)
        if args.cmd == "sim":
            r = simmod.run_test(cfg, args.test, args.seed, args.define, args.cov, args.trace, args.expect_fail, args.dry_run, args.compile_only)
            if not args.dry_run and not args.compile_only and r.get("status") in ("pass", "fail"):
                tag = f"{r['test']}_{r['seed']}" + ("_" + "_".join(r["defines"]) if r["defines"] else "")
                (cfg.evidence_dir / "sim" / f"{tag}.json").write_text(json.dumps(r, indent=2))
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
            r = verifymod.run_verify(cfg, args.base, args.files, args.claim, args.gap, args.door, args.keep, prog)
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


if __name__ == "__main__":
    main()
