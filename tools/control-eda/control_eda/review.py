"""review: evidence for RTL review of CDC, reset and X-propagation, and a merge of reviewer findings.

`review scan` is a structural pass over the changed RTL that needs no EDA tool,
so every user gets it. It is a heuristic, not a CDC sign-off tool: it finds
clock domains from `always_ff` / `always @(posedge ...)` blocks, follows signals
assigned in one domain and read in another, and recognises the plain two-flop
synchronizer. When Verilator is on PATH its lint warnings that bear on these
three topics are added; a site CDC tool (SpyGlass, Questa CDC, Meridian, ...)
plugs in as a command under `[review.tools.<name>]` in dv.toml.

`review merge` takes the findings of two or three reviewers (usually the same
prompt on different models), clusters them by file, line and category, and
marks each cluster agreed, majority, single (one reviewer only) or contested
(one flagged it, another explicitly cleared it). Disagreements go to a human.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .formal.sva import blank_comments
from .util import run

SEVERITIES = ("info", "low", "medium", "high")
CATEGORIES = ("cdc", "reset", "xprop", "other")
RESET_NAME = re.compile(r"(^|_)(a?rst|reset|rstn|rst_n|resetn|reset_n|arst_n|nrst)(_|$)|^rst", re.I)
ALWAYS_RE = re.compile(r"\b(always_ff|always_latch|always_comb|always)\b\s*(@\s*(\*|\(\s*\*\s*\)|\(([^()]*)\)))?")
EDGE_RE = re.compile(r"\b(posedge|negedge)\s+([A-Za-z_][\w.]*)")
NB_ASSIGN_RE = re.compile(r"(?<![<>=!])\b([A-Za-z_]\w*)\s*(?:\[[^\];]*\]\s*)*<=")
BL_ASSIGN_RE = re.compile(r"(?<![<>=!])\b([A-Za-z_]\w*)\s*(?:\[[^\];]*\]\s*)*=(?!=)")
IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")
DECL_RE = re.compile(r"\b(?:input|output|inout)?\s*(?:wire|reg|logic|bit)\s*(?:signed\s*)?(\[[^\]]+\])?\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)")
PORT_DECL_RE = re.compile(r"\b(input|output|inout)\b\s*(?:wire|reg|logic|bit)?\s*(?:signed\s*)?(\[[^\]]+\])?\s*([A-Za-z_]\w*)")
MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)")
KEYWORDS = set("""always always_ff always_comb always_latch begin end if else case casez casex endcase default posedge negedge
or and not assign logic wire reg bit int integer unique priority inside for while repeat module endmodule input output inout
parameter localparam genvar generate endgenerate function endfunction task endtask return signed unsigned disable iff assert
property cover assume initial final typedef enum struct packed""".split())

VERILATOR_TOPICS = {
    "SYNCASYNCNET": ("reset", "medium"), "MULTIDRIVEN": ("cdc", "high"), "CASEINCOMPLETE": ("xprop", "medium"),
    "CASEX": ("xprop", "high"), "CASEOVERLAP": ("xprop", "low"), "LATCH": ("xprop", "medium"), "NOLATCH": ("xprop", "low"),
    "COMBDLY": ("xprop", "low"), "BLKSEQ": ("other", "medium"), "ALWCOMBORDER": ("xprop", "low"), "UNOPTFLAT": ("other", "low"),
    "INITIALDLY": ("other", "low"), "MULTITOP": ("other", "info"),
}


def _paren_end(text: str, i: int) -> int:
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def _match_block(text: str, start: int) -> int:
    """End offset of the procedural statement starting at `start`."""
    i = re.compile(r"\s*").match(text, start).end()
    if re.match(r"begin\b", text[i:]):
        depth = 0
        for t in re.finditer(r"\b(begin|end)\b", text[i:]):
            depth += 1 if t.group(1) == "begin" else -1
            if depth == 0:
                return i + t.end()
        return len(text)
    m = re.match(r"(?:unique0?\s+|priority\s+)?(if|case[zx]?|for|while|repeat|foreach)\b", text[i:])
    if m:
        kw = m.group(1)
        p = text.find("(", i + m.end())
        if p < 0:
            return len(text)
        after = _paren_end(text, p)
        if kw.startswith("case"):
            e = re.compile(r"\bendcase\b").search(text, after)
            return e.end() if e else len(text)
        j = _match_block(text, after)
        if kw == "if":
            nxt = re.compile(r"\s*else\b").match(text, j)
            if nxt:
                return _match_block(text, nxt.end())
        return j
    semi = text.find(";", i)
    return semi + 1 if semi >= 0 else len(text)


def _widths(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in DECL_RE.finditer(text):
        rng = m.group(1)
        w = _range_width(rng)
        for name in re.split(r"\s*,\s*", m.group(2)):
            out.setdefault(name.strip(), w)
    for _, rng, names in _port_groups(text):
        for n in names:
            out[n] = _range_width(rng)
    return out


def _range_width(rng: str | None) -> int:
    if not rng:
        return 1
    m = re.match(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", rng)
    if m:
        return abs(int(m.group(1)) - int(m.group(2))) + 1
    return 2  # a parameterised range: treat as multi-bit


PORT_GROUP_RE = re.compile(r"\b(input|output|inout)\b(.*?)(?=\b(?:input|output|inout)\b|\)\s*;|;)", re.S)


def _port_groups(text: str):
    for m in PORT_GROUP_RE.finditer(text):
        body = m.group(2)
        rng = re.search(r"\[[^\]]+\]", body)
        names = [n for n in IDENT_RE.findall(re.sub(r"\[[^\]]*\]", " ", body)) if n not in KEYWORDS]
        yield m.group(1), rng.group(0) if rng else None, names


def _ports(text: str) -> dict[str, str]:
    return {n: d for d, _, names in _port_groups(text) for n in names}


def scan_text(text: str, file: str, async_inputs: list[str] | None = None, reset_sync: list[str] | None = None,
              is_tb: bool = False) -> list[dict]:
    """Findings for one file. Pure Python; the same on every machine."""
    clean = blank_comments(text)
    findings: list[dict] = []
    lines = text.splitlines()

    def add(cat: str, sev: str, rule: str, pos: int, msg: str) -> None:
        ln = clean.count("\n", 0, pos) + 1
        findings.append({"category": cat, "severity": sev, "rule": rule, "file": file, "line": ln, "message": msg,
                         "evidence": lines[ln - 1].strip()[:160] if 0 < ln <= len(lines) else "", "source": "scan"})

    mods = list(MODULE_RE.finditer(clean))
    spans = []
    for i, m in enumerate(mods):
        end = clean.find("endmodule", m.end())
        spans.append((m.group(1), m.start(), end if end >= 0 else len(clean)))
    for mod, s, e in spans:
        body = clean[s:e]
        widths = _widths(body)
        ports = _ports(body)
        blocks = []
        for am in ALWAYS_RE.finditer(body):
            kind = am.group(1)
            sens = am.group(4) or ""
            stmt_start = am.end()
            stmt_end = _match_block(body, stmt_start)
            stmt = body[stmt_start:stmt_end]
            edges = EDGE_RE.findall(sens)
            seq = kind == "always_ff" or bool(edges)
            if not seq:
                blocks.append({"kind": "comb", "pos": s + am.start(), "spos": s + stmt_start, "stmt": stmt, "clock": None})
                continue
            resets = [sig for _, sig in edges if RESET_NAME.search(sig.split(".")[-1])]
            clocks = [sig for _, sig in edges if sig not in resets]
            if not clocks and resets:   # a lone edge named like a reset is still the clock
                clocks, resets = resets[:1], resets[1:]
            blocks.append({"kind": "seq", "pos": s + am.start(), "spos": s + stmt_start, "stmt": stmt, "clock": clocks[0] if clocks else None,
                           "async_resets": resets, "assigned": set(NB_ASSIGN_RE.findall(stmt)) | set(BL_ASSIGN_RE.findall(stmt)) - KEYWORDS})
        domain: dict[str, str] = {}
        for b in blocks:
            if b["kind"] == "seq" and b["clock"]:
                for sig in b["assigned"]:
                    domain.setdefault(sig, b["clock"])
        for sig in async_inputs or []:
            if ports.get(sig) == "input":
                domain.setdefault(sig, "<async>")
        # combinational signals inherit the domain of everything they read, if it is one domain
        comb_src: dict[str, set[str]] = {}
        for am in re.finditer(r"\bassign\s+([A-Za-z_]\w*)[^=]*=([^;]*);", body):
            comb_src.setdefault(am.group(1), set()).update(IDENT_RE.findall(am.group(2)))
        for b in blocks:
            if b["kind"] == "comb":
                for sig in set(BL_ASSIGN_RE.findall(b["stmt"])) - KEYWORDS:
                    comb_src.setdefault(sig, set()).update(IDENT_RE.findall(b["stmt"]))
        for _ in range(4):
            for sig, srcs in comb_src.items():
                doms = {domain[x] for x in srcs if x in domain and x != sig}
                if len(doms) == 1 and sig not in domain:
                    domain[sig] = doms.pop()
                elif len(doms) > 1 and sig not in domain:
                    domain[sig] = "<mixed:" + ",".join(sorted(doms)) + ">"

        seq_blocks = [b for b in blocks if b["kind"] == "seq" and b["clock"]]
        # ---- CDC
        for b in seq_blocks:
            clk = b["clock"]
            copies = dict(re.findall(r"\b([A-Za-z_]\w*)\s*<=\s*([A-Za-z_]\w*)\s*;", b["stmt"]))
            for ident in sorted(set(IDENT_RE.findall(b["stmt"])) - KEYWORDS - b["assigned"]):
                src = domain.get(ident)
                if not src or src == clk or ident in (b.get("async_resets") or []):
                    continue
                hit = re.search(rf"\b{re.escape(ident)}\b", b["stmt"])
                pos = b["spos"] + (hit.start() if hit else 0)
                pure = [d for d, s_ in copies.items() if s_ == ident]
                second = [d2 for d2, s2 in copies.items() for d in pure if s2 == d]
                where = "an asynchronous input" if src == "<async>" else (f"signals from several domains ({src[7:-1]})" if src.startswith("<mixed") else f"clock domain {src}")
                if src.startswith("<mixed"):
                    add("cdc", "high", "cdc-mixed-source", pos,
                        f"`{ident}` combines {where} and is sampled on {clk}: glitch-prone crossing; register it in its source domain first")
                elif pure and second:
                    if widths.get(ident, 1) > 1 and not re.search(r"gray|gry", ident, re.I):
                        add("cdc", "high", "cdc-multibit-2ff", pos,
                            f"`{ident}` ({widths.get(ident)} bits) from {where} goes through a two-flop synchronizer on {clk}; bits can arrive in different cycles. Use gray code, a handshake or an async FIFO")
                    # single bit through two flops: the accepted pattern, nothing to report
                elif pure:
                    add("cdc", "medium", "cdc-single-flop", pos,
                        f"`{ident}` from {where} is captured by one flop (`{pure[0]}`) on {clk}; one stage leaves metastability. Add a second stage")
                else:
                    add("cdc", "high", "cdc-unsynchronized", pos,
                        f"`{ident}` from {where} is used in logic on {clk} without a synchronizer")
        # ---- reset
        reset_domains: dict[str, set[str]] = {}
        for b in seq_blocks:
            for r in b.get("async_resets") or []:
                reset_domains.setdefault(r, set()).add(b["clock"])
                first = re.match(r"\s*(begin\s*)?(?:\w+\s*:\s*)?if\s*\(([^)]*)\)", b["stmt"])
                if not first or r not in first.group(2):
                    add("reset", "high", "reset-async-not-first", b["pos"],
                        f"async reset `{r}` is in the sensitivity list but the block does not test it first; synthesis and simulation will disagree")
        for b in seq_blocks:
            sync_rst = re.match(r"\s*(begin\s*)?if\s*\(\s*!?\s*([A-Za-z_]\w*)\s*\)", b["stmt"])
            if sync_rst and not b.get("async_resets") and RESET_NAME.search(sync_rst.group(2)) and sync_rst.group(2) in reset_domains:
                add("reset", "medium", "reset-mixed-sync-async", b["pos"],
                    f"`{sync_rst.group(2)}` is an async reset elsewhere in {mod} but a sync reset here; pick one style per reset")
        for r, clks in reset_domains.items():
            if len(clks) > 1 and not any(re.search(p, r) for p in (reset_sync or [r"_sync", r"sync_"])):
                pos = next(b["pos"] for b in seq_blocks if r in (b.get("async_resets") or []))
                add("reset", "medium", "reset-multi-domain", pos,
                    f"async reset `{r}` is used in clock domains {sorted(clks)}; its deassertion must be synchronized to each domain")
        if not is_tb:
            reset_regs = {sig for b in seq_blocks if b.get("async_resets") or re.match(r"\s*(begin\s*)?if\s*\(\s*!?\s*\w*(rst|reset)", b["stmt"], re.I) for sig in b["assigned"]}
            for b in seq_blocks:
                if not b.get("async_resets") and not re.match(r"\s*(begin\s*)?if\s*\(\s*!?\s*\w*(rst|reset)", b["stmt"], re.I) and reset_regs:
                    plain = sorted(x for x in b["assigned"] if x not in reset_regs and widths.get(x, 1) <= 64)
                    if plain:
                        add("reset", "low", "reset-missing", b["pos"],
                            f"{', '.join('`'+x+'`' for x in plain[:4])} have no reset while other registers in {mod} do; check they cannot drive control logic out of reset")
        # ---- X-propagation
        if not is_tb:
            for m in re.finditer(r"\bcasex\b", body):
                add("xprop", "high", "xprop-casex", s + m.start(), "`casex` treats X and Z in the selector as wildcards and hides X; use `case ... inside`")
            for m in re.finditer(r"(===|!==)", body):
                add("xprop", "medium", "xprop-case-equality", s + m.start(), f"`{m.group(1)}` in RTL compares X literally; synthesis treats it as `==`, so simulation and gates can differ")
            for m in re.finditer(r"=\s*(?:\d+)?'[bh]?[xX]\b|=\s*'x\b|=\s*\{[^}]*'[bh]?x", body):
                add("xprop", "low", "xprop-x-assign", s + m.start(), "explicit X assignment: simulation is X-optimistic here; make sure no control path reads it")
            for b in blocks:
                if b["kind"] != "comb":
                    continue
                for cm in re.finditer(r"\b(unique\s+|priority\s+)?case[z]?\b\s*\(", b["stmt"]):
                    end = re.compile(r"\bendcase\b").search(b["stmt"], cm.end())
                    seg = b["stmt"][cm.end(): end.start() if end else len(b["stmt"])]
                    if not re.search(r"\bdefault\b", seg) and not re.search(r"\binside\b", seg[:80]):
                        add("xprop", "medium", "xprop-case-no-default", b["spos"] + cm.start(),
                            "combinational case without `default`: an unlisted or X selector leaves the output unassigned (latch or X)")
    return findings


def _scope(cfg: Config, files: list[str] | None, base: str | None) -> list[str]:
    if files:
        return [f for f in files if (cfg.root / f).exists()]
    from . import evidence as evmod
    hdl = (".sv", ".v", ".svh", ".vh")
    if base:
        changed = [f for f in evmod.changed_files(cfg, base) if f.endswith(hdl) and (cfg.root / f).exists()]
        if changed:
            return changed
    out = []
    for g in cfg.project.get("rtl_globs", []):
        out += sorted(str(p.relative_to(cfg.root)) for p in cfg.root.glob(g))
    return out


def _verilator(cfg: Config, files: list[str]) -> tuple[list[dict], dict]:
    exe = shutil.which("verilator")
    if not exe:
        return [], {"name": "verilator", "available": False, "install": "apt install verilator / brew install verilator"}
    flags = [f for f in cfg.lint.get("flags", []) if not f.startswith("-Wno-")]
    code, out, _ = run([exe, "--lint-only", "--timing", "-Wall", "-Wno-fatal", *flags, *files], cfg.root, cfg.evidence_dir / "review-verilator.log")
    found = []
    for line in out.splitlines():
        m = re.match(r"^%(Warning|Error)-(\w+): ([^:\s]+):(\d+):(?:\d+:)? (.*)$", line)
        if m and m.group(2) in VERILATOR_TOPICS:
            cat, sev = VERILATOR_TOPICS[m.group(2)]
            found.append({"category": cat, "severity": sev, "rule": f"verilator-{m.group(2)}", "file": m.group(3), "line": int(m.group(4)),
                          "message": m.group(5).strip(), "evidence": "", "source": "verilator"})
    return found, {"name": "verilator", "available": True}


def _custom_tools(cfg: Config, files: list[str]) -> tuple[list[dict], list[dict]]:
    found, status = [], []
    for name, spec in (cfg.raw.get("review", {}).get("tools") or {}).items():
        exe = spec.get("exe")
        path = shutil.which(exe) if exe else None
        if not path and exe and (cfg.root / exe).exists():
            path = str(cfg.root / exe)
        if not path:
            status.append({"name": name, "available": False, "install": f"`{exe}` is not on PATH; set [review.tools.{name}].exe in dv.toml"})
            continue
        cmd = []
        for c in spec.get("cmd", ["{{exe}}", "{{files}}"]):
            if c == "{{files}}":
                cmd += files
            else:
                cmd.append(c.replace("{{exe}}", path).replace("{{root}}", str(cfg.root)))
        code, out, _ = run(cmd, cfg.root, cfg.evidence_dir / f"review-{name}.log", timeout=int(spec.get("timeout", 1800)))
        rx = re.compile(spec.get("pattern", r"(?P<file>[^:\s]+):(?P<line>\d+):\s*(?P<message>.*)"))
        n = 0
        for line in out.splitlines():
            m = rx.search(line)
            if not m:
                continue
            g = m.groupdict()
            found.append({"category": g.get("category") or spec.get("category", "cdc"), "severity": g.get("severity") or spec.get("severity", "medium"),
                          "rule": f"{name}-{g.get('rule') or 'finding'}", "file": g.get("file", ""), "line": int(g.get("line") or 0),
                          "message": g.get("message", line).strip(), "evidence": "", "source": name})
            n += 1
        status.append({"name": name, "available": True, "exit_code": code, "findings": n})
    return found, status


def scan(cfg: Config, files: list[str] | None = None, base: str | None = None) -> dict:
    sec = cfg.raw.get("review") or {}
    scope = _scope(cfg, files, base)
    tb_globs = cfg.project.get("tb_globs", [])
    tb_files = {str(p.relative_to(cfg.root)) for g in tb_globs for p in cfg.root.glob(g)}
    findings: list[dict] = []
    for f in scope:
        findings += scan_text((cfg.root / f).read_text(errors="replace"), f, sec.get("async_inputs"), sec.get("reset_synchronizers"), is_tb=f in tb_files)
    rtl_scope = [f for f in scope if f not in tb_files]
    tools = [{"name": "scan", "available": True, "note": "structural heuristic, not a CDC sign-off"}]
    if rtl_scope and sec.get("verilator", True):
        v, st = _verilator(cfg, rtl_scope)
        findings += v
        tools.append(st)
    c, st = _custom_tools(cfg, rtl_scope)
    findings += c
    tools += st
    for i, f in enumerate(findings):
        f["id"] = f"S{i + 1}"
    by = {c: sum(1 for f in findings if f["category"] == c) for c in CATEGORIES}
    r = {"ok": True, "version": 1, "files": scope, "base": base, "tools": tools, "findings": findings, "counts": by,
         "high": sum(1 for f in findings if f["severity"] == "high")}
    (cfg.evidence_dir / "review-scan.json").write_text(json.dumps(r, indent=2))
    return r


# ---------------------------------------------------------------- merge

def _load_review(p: Path) -> dict:
    d = json.loads(p.read_text())
    if "findings" not in d:
        raise ValueError(f"{p}: no `findings` list")
    d.setdefault("reviewer", p.stem)
    d.setdefault("cleared", [])
    return d


def merge(cfg: Config, paths: list[Path], window: int | None = None, include_scan: bool = True) -> dict:
    sec = cfg.raw.get("review") or {}
    window = int(window if window is not None else sec.get("line_window", 3))
    reviews, problems = [], []
    for p in paths:
        try:
            reviews.append(_load_review(p))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            problems.append(str(e))
    names = [r["reviewer"] for r in reviews]
    models = sorted({r.get("model") or "unknown" for r in reviews})
    scan_r = None
    sp = cfg.evidence_dir / "review-scan.json"
    if include_scan and sp.exists():
        scan_r = json.loads(sp.read_text())

    items = []
    for r in reviews:
        for f in r["findings"]:
            items.append({**f, "reviewer": r["reviewer"], "model": r.get("model")})
    clusters: list[dict] = []
    for it in sorted(items, key=lambda x: (x.get("file", ""), x.get("category", ""), int(x.get("line") or 0))):
        cat = it.get("category") if it.get("category") in CATEGORIES else "other"
        for c in clusters:
            if c["file"] == it.get("file") and c["category"] == cat and abs(c["line"] - int(it.get("line") or 0)) <= window:
                c["items"].append(it)
                break
        else:
            clusters.append({"file": it.get("file"), "line": int(it.get("line") or 0), "category": cat, "items": [it]})
    out = []
    n = len(set(names))
    for i, c in enumerate(clusters):
        who = sorted({x["reviewer"] for x in c["items"]})
        cleared_by = sorted({r["reviewer"] for r in reviews if r["reviewer"] not in who for cl in r["cleared"]
                             if cl.get("file") == c["file"] and (cl.get("category") in (None, c["category"]))
                             and abs(int(cl.get("line") or 0) - c["line"]) <= window})
        sev = max((x.get("severity", "medium") for x in c["items"]), key=lambda s: SEVERITIES.index(s) if s in SEVERITIES else 2)
        tool = [f for f in (scan_r or {}).get("findings", []) if f["file"] == c["file"] and f["category"] == c["category"] and abs(f["line"] - c["line"]) <= window]
        if n <= 1:
            status = "single_model"
        elif cleared_by:
            status = "contested"
        elif len(who) == n:
            status = "agreed"
        elif len(who) >= 2:
            status = "majority"
        else:
            status = "single"
        out.append({"id": f"F{i + 1}", "file": c["file"], "line": c["line"], "category": c["category"], "severity": sev, "status": status,
                    "reviewers": who, "cleared_by": cleared_by, "tool_backed": [t["id"] for t in tool],
                    "claims": [{"reviewer": x["reviewer"], "model": x.get("model"), "claim": x.get("claim") or x.get("message", ""),
                                "evidence": x.get("evidence", "")} for x in c["items"]],
                    "needs_human": status in ("single", "contested", "single_model") or (status == "majority" and sev in ("high", "medium"))})
    # tool findings no reviewer raised: a reviewer missed something a tool saw
    seen = {t for f in out for t in f["tool_backed"]}
    missed = [f for f in (scan_r or {}).get("findings", []) if f["id"] not in seen and f["severity"] in ("high", "medium")]
    verdict = _verdict(out, missed, n)
    res = {"ok": not problems, "version": 1, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "reviewers": names, "models": models, "single_model": len(models) <= 1 and n > 1, "findings": out,
           "tool_only": missed, "verdict": verdict, "problems": problems,
           "counts": {s: sum(1 for f in out if f["status"] == s) for s in ("agreed", "majority", "single", "contested", "single_model")}}
    if res["single_model"]:
        res["note"] = "every reviewer ran on the same model, so agreement says little; treat the merge as one review"
    (cfg.evidence_dir / "review.json").write_text(json.dumps(res, indent=2))
    (cfg.evidence_dir / "review.md").write_text(markdown(res))
    return res


def _verdict(out: list[dict], missed: list[dict], n: int) -> str:
    if not out and not missed:
        return "CLEAN"
    if any(f["status"] in ("agreed", "majority") and f["severity"] == "high" for f in out) or any(f["severity"] == "high" and f["tool_backed"] for f in out):
        return "CHANGES_REQUESTED"
    if any(f["needs_human"] for f in out) or missed:
        return "NEEDS_HUMAN"
    return "COMMENTS"


def markdown(r: dict) -> str:
    lines = [f"## RTL review: {r['verdict']}", "",
             f"Reviewers: {', '.join(r['reviewers']) or 'none'} (models: {', '.join(r['models'])}).", ""]
    if r.get("note"):
        lines += [f"> {r['note']}", ""]
    if r["findings"]:
        lines += ["| | Where | Topic | Severity | Status | Who | Claim |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for f in sorted(r["findings"], key=lambda x: (not x["needs_human"], -SEVERITIES.index(x["severity"]) if x["severity"] in SEVERITIES else 0)):
            claim = f["claims"][0]["claim"].replace("|", "\\|")[:140]
            mark = "**human**" if f["needs_human"] else ""
            who = ", ".join(f["reviewers"]) + (f"; cleared by {', '.join(f['cleared_by'])}" if f["cleared_by"] else "") + (" + tool" if f["tool_backed"] else "")
            lines.append(f"| {mark} | `{f['file']}:{f['line']}` | {f['category']} | {f['severity']} | {f['status']} | {who} | {claim} |")
        lines.append("")
    if r["tool_only"]:
        lines += ["Found by the scan but by no reviewer:", ""]
        lines += [f"- `{t['file']}:{t['line']}` {t['category']}/{t['severity']}: {t['message']}" for t in r["tool_only"]]
        lines.append("")
    return "\n".join(lines)
