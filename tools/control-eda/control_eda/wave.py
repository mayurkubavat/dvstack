"""wave-query: a JSON cycle table around a time, from VCD (or FST via fst2vcd).

The agent asks "what did these signals do around 18450 ns" and gets rows it can
read, instead of a human opening a waveform viewer and describing it.
"""
from __future__ import annotations

import fnmatch
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .util import fmt_time, which


@dataclass
class Var:
    code: str
    name: str
    width: int
    path: str


@dataclass
class Vcd:
    timescale_ps: int = 1
    vars: dict[str, list[Var]] = field(default_factory=dict)   # code -> vars (aliases)
    changes: list[tuple[int, str, str]] = field(default_factory=list)  # (time_ps, code, value)
    end_time: int = 0


def _timescale_ps(spec: str) -> int:
    spec = spec.strip().replace(" ", "")
    units = {"fs": 0.001, "ps": 1, "ns": 1000, "us": 1_000_000, "ms": 1_000_000_000, "s": 1_000_000_000_000}
    for u in ("fs", "ps", "ns", "us", "ms", "s"):
        if spec.endswith(u):
            n = float(spec[: -len(u)] or "1")
            return max(1, int(n * units[u]))
    return 1


def to_vcd(path: Path) -> Path:
    if path.suffix.lower() == ".vcd":
        return path
    if path.suffix.lower() == ".fst":
        exe = which("fst2vcd")
        if not exe:
            raise RuntimeError("fst2vcd not installed (ships with gtkwave); dump VCD instead or install gtkwave")
        out = Path(tempfile.mkdtemp()) / (path.stem + ".vcd")
        subprocess.run([exe, str(path), "-o", str(out)], check=True, capture_output=True)
        return out
    if path.suffix.lower() == ".fsdb":
        raise RuntimeError("FSDB needs the Verdi adapter (fsdb2vcd); not available in the open-source build")
    raise RuntimeError(f"unknown wave format {path.suffix}")


def parse_vcd(path: Path) -> Vcd:
    v = Vcd()
    scope: list[str] = []
    in_header = True
    t = 0
    with path.open(errors="replace") as f:
        tokens_iter = iter(f.read().split("\n"))
        for raw in tokens_iter:
            line = raw.strip()
            if not line:
                continue
            if in_header:
                if line.startswith("$timescale"):
                    spec = line[len("$timescale"):].replace("$end", "").strip()
                    while not spec:
                        spec = next(tokens_iter).replace("$end", "").strip()
                    v.timescale_ps = _timescale_ps(spec)
                elif line.startswith("$scope"):
                    parts = line.split()
                    scope.append(parts[2] if len(parts) > 2 else "?")
                elif line.startswith("$upscope"):
                    if scope:
                        scope.pop()
                elif line.startswith("$var"):
                    parts = line.split()
                    # $var wire 8 & wdata [7:0] $end
                    width, code, name = int(parts[2]), parts[3], parts[4]
                    full = ".".join([*scope, name])
                    v.vars.setdefault(code, []).append(Var(code, name, width, full))
                elif line.startswith("$enddefinitions"):
                    in_header = False
                continue
            if line[0] == "#":
                t = int(line[1:]) * v.timescale_ps
                v.end_time = max(v.end_time, t)
            elif line[0] in "01xXzZ":
                v.changes.append((t, line[1:], line[0]))
            elif line[0] in "bBrR":
                val, code = line.split(None, 1)
                v.changes.append((t, code, val[1:]))
            elif line.startswith("$dumpvars") or line.startswith("$end"):
                continue
    return v


def _fmt(val: str, width: int) -> str:
    if val in ("x", "X", "z", "Z"):
        return val.lower()
    if any(c in "xXzZ" for c in val):
        return val
    if width == 1:
        return val
    try:
        n = int(val, 2)
    except ValueError:
        return val
    return f"0x{n:0{(width + 3) // 4}x}"


def query(dump: Path, at: int, window: int, patterns: list[str], clock: str | None = None) -> dict:
    vcd = parse_vcd(to_vcd(dump))
    # choose signals
    chosen: dict[str, Var] = {}
    for code, vs in vcd.vars.items():
        for var in vs:
            if any(fnmatch.fnmatch(var.path, p) or fnmatch.fnmatch(var.path, "*." + p) or fnmatch.fnmatch(var.name, p) for p in patterns):
                chosen[var.path] = var
    if not chosen:
        sample = sorted({var.path for vs in vcd.vars.values() for var in vs})[:40]
        return {"ok": False, "error": "no signals matched", "patterns": patterns, "available_sample": sample, "hint": "use a glob like 'tb_fifo.dut.*' (matched against full path or leaf name)"}
    # clock edges
    clk_var = None
    if clock:
        clk_var = next((v for v in chosen.values() if fnmatch.fnmatch(v.path, clock) or v.name == clock), None)
    if clk_var is None:
        clk_var = next((v for v in chosen.values() if v.name in ("clk", "clock", "clk_i")), None)
    if clk_var is None:
        for vs in vcd.vars.values():
            for var in vs:
                if var.name in ("clk", "clock", "clk_i"):
                    clk_var = var
                    break
            if clk_var:
                break
    edges: list[int] = []
    if clk_var:
        prev = None
        for t, code, val in vcd.changes:
            if code == clk_var.code:
                if prev == "0" and val == "1":
                    edges.append(t)
                prev = val
    if not edges:
        times = sorted({t for t, _, _ in vcd.changes})
        edges = times
    # pick window of edges around `at`
    idx = min(range(len(edges)), key=lambda i: abs(edges[i] - at)) if edges else 0
    lo, hi = max(0, idx - window), min(len(edges), idx + window + 1)
    sel = edges[lo:hi]
    # replay values
    codes = {v.code for v in chosen.values()}
    state: dict[str, str] = {}
    rows = []
    ci = 0
    changes = vcd.changes
    n = len(changes)
    prev_vals: dict[str, str] = {}
    for edge in sel:
        while ci < n and changes[ci][0] <= edge:
            t, code, val = changes[ci]
            if code in codes:
                state[code] = val
            ci += 1
        row = {"time": fmt_time(edge), "time_ps": edge, "signals": {}, "changed": []}
        for path, var in sorted(chosen.items()):
            val = _fmt(state.get(var.code, "x"), var.width)
            row["signals"][path] = val
            if prev_vals.get(path, object()) != val and path in prev_vals:
                row["changed"].append(path)
            prev_vals[path] = val
        rows.append(row)
    return {
        "ok": True,
        "dump": str(dump),
        "at": fmt_time(at),
        "clock": clk_var.path if clk_var else None,
        "edges": len(sel),
        "signals": sorted(chosen),
        "rows": rows,
    }


def table(res: dict) -> str:
    if not res.get("ok"):
        return f"error: {res.get('error')}\n{res.get('hint', '')}"
    sigs = res["signals"]
    short = [s.split(".")[-1] for s in sigs]
    w = [max(len(h), 6) for h in short]
    lines = [" | ".join(["time".ljust(10)] + [h.ljust(w[i]) for i, h in enumerate(short)])]
    lines.append("-" * len(lines[0]))
    for r in res["rows"]:
        cells = []
        for i, s in enumerate(sigs):
            v = r["signals"][s]
            mark = "*" if s in r["changed"] else " "
            cells.append((v + mark).ljust(w[i]))
        lines.append(" | ".join([r["time"].ljust(10)] + cells))
    lines.append("(* = changed since previous edge)")
    return "\n".join(lines)
