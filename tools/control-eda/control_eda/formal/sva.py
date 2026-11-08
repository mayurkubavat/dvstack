"""Find concurrent SVA in RTL and, when the backend needs it, rewrite it.

Free Yosys (no Verific) does not parse `assert property (@(posedge clk) ...)`.
It does parse clocked immediate assertions inside `always` blocks. This module
rewrites the single-cycle subset into that form so a user with only the open
source tools can still prove, cover and mutate:

    a: assert property (@(posedge clk) disable iff (!rst_n) req |-> gnt);
 -> always @(posedge clk) if (!(!rst_n)) a: assert(!(req) || (gnt));
    + a__antecedent: cover(req)          (anti-vacuity: the trigger must happen)

What it cannot rewrite (sequences, ##, repetition, named properties, until,
eventually) it leaves out and reports as `unsupported`, so the gate says
INCONCLUSIVE for that property instead of pretending it was proven. Backends
with native SVA (Verific, JasperGold, VC Formal, Questa) skip the rewrite.

Replacements keep the line count, so tool messages point at the real line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PROP_START = re.compile(r"(?:\b(?P<label>[A-Za-z_]\w*)\s*:\s*)?\b(?P<kind>assert|assume|cover)\s+property\s*\(")
CLOCK_RE = re.compile(r"^\s*@\s*\(\s*(?P<edge>posedge|negedge)\s+(?P<clk>[\w.]+)\s*\)\s*")
DISABLE_RE = re.compile(r"^\s*disable\s+iff\s*\(")
UNSUPPORTED_RE = re.compile(
    r"##|\[\s*\*|\[\s*=|\[\s*->|\b(throughout|within|intersect|s_eventually|eventually|until|s_until|until_with|"
    r"s_until_with|nexttime|s_nexttime|always|s_always|first_match|strong|weak|accept_on|reject_on|sync_accept_on|"
    r"sync_reject_on|not|or|and|implies|iff|if|case)\b|#-#|#=#"
)
PAST_RE = re.compile(r"\$(past|rose|fell|stable|changed)\b")
MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)")
ENDMODULE_RE = re.compile(r"\bendmodule\b")


@dataclass
class Prop:
    label: str
    kind: str                 # assert | assume | cover
    file: str
    line: int
    text: str                 # the original statement
    module: str | None
    clock: str | None = None
    edge: str = "posedge"
    disable: str | None = None
    body: str = ""
    antecedent: str | None = None
    implication: str | None = None   # "|->" | "|=>" | None
    named_label: bool = True
    unsupported: str | None = None   # reason the free-Yosys rewrite can't express it
    start: int = 0
    end: int = 0

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("label", "kind", "file", "line", "module", "clock", "disable", "antecedent", "implication", "named_label", "unsupported")}
        return d


def blank_comments(text: str) -> str:
    """Replace comments and string bodies with spaces, keeping offsets and newlines."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"' and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _match_paren(s: str, open_idx: int) -> int:
    """Index of the ')' that closes s[open_idx] == '('; -1 if unbalanced."""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top(expr: str, op: str) -> list[str]:
    parts, depth, last, i = [], 0, 0, 0
    while i < len(expr):
        c = expr[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[last:i])
            last = i + len(op)
            i += len(op)
            continue
        i += 1
    parts.append(expr[last:])
    return parts


def _module_at(clean: str, pos: int) -> str | None:
    name = None
    for m in MODULE_RE.finditer(clean, 0, pos):
        name = m.group(1)
    return name


def extract(text: str, file: str = "", default_clock: str | None = None) -> list[Prop]:
    clean = blank_comments(text)
    props: list[Prop] = []
    named_props = set(re.findall(r"\bproperty\s+([A-Za-z_]\w*)", clean))
    for m in PROP_START.finditer(clean):
        open_idx = m.end() - 1
        close = _match_paren(clean, open_idx)
        if close < 0:
            continue
        # statement ends at the first ';' after the closing paren (skips an else action block)
        semi = clean.find(";", close)
        end = semi + 1 if semi >= 0 else close + 1
        tail = clean[close + 1:end - 1].strip()
        label = m.group("label")
        line = text.count("\n", 0, m.start()) + 1
        kind = m.group("kind")
        named = bool(label) and label not in ("assert", "assume", "cover")
        p = Prop(label=label if named else f"__cea_{kind}_L{line}", kind=kind, file=file, line=line,
                 text=text[m.start():end], module=_module_at(clean, m.start()), named_label=named,
                 start=m.start(), end=end)
        inner = clean[open_idx + 1:close]
        cm = CLOCK_RE.match(inner)
        if cm:
            p.clock, p.edge = cm.group("clk"), cm.group("edge")
            inner = inner[cm.end():]
        else:
            p.clock = default_clock
        dm = DISABLE_RE.match(inner)
        if dm:
            d_open = dm.end() - 1
            d_close = _match_paren(inner, d_open)
            if d_close < 0:
                p.unsupported = "unbalanced disable iff"
            else:
                p.disable = inner[d_open + 1:d_close].strip()
                inner = inner[d_close + 1:]
        body = " ".join(inner.split())
        p.body = body
        if tail and not tail.startswith("else"):
            p.unsupported = p.unsupported or f"unexpected text after the property: {tail[:40]}"
        if p.clock is None:
            p.unsupported = p.unsupported or "no clocking event and no [formal].clock"
        if UNSUPPORTED_RE.search(body):
            p.unsupported = p.unsupported or "uses sequence or temporal operators the free Yosys parser cannot read"
        nm = re.match(r"^([A-Za-z_]\w*)\s*(\(|$)", body.strip())
        if nm and nm.group(1) in named_props:
            p.unsupported = p.unsupported or "refers to a named property or sequence"
        for op in ("|->", "|=>"):
            parts = _split_top(body, op)
            if len(parts) == 2:
                p.implication = op
                p.antecedent = parts[0].strip()
                break
            if len(parts) > 2:
                p.unsupported = p.unsupported or "chained implications"
        if p.implication and kind == "cover":
            p.unsupported = p.unsupported or "cover of an implication"
        props.append(p)
    return props


def _rewrite(p: Prop, pv_name: str | None) -> str:
    guard = []
    if p.disable:
        guard.append(f"!({p.disable})")
    body = p.body
    if p.implication == "|->":
        a, b = _split_top(body, "|->")
        expr = f"!({a.strip()}) || ({b.strip()})"
    elif p.implication == "|=>":
        a, b = _split_top(body, "|=>")
        expr = f"!$past({a.strip()}) || ({b.strip()})"
        if p.disable:
            guard.append(f"$past(!({p.disable}))")
    else:
        expr = body
    if pv_name:
        guard.insert(0, pv_name)
    ev = f"@({p.edge} {p.clock})"
    g = f"if ({' && '.join(guard)}) " if guard else ""
    stmt = f"always {ev} {g}{p.label}: {p.kind}({expr});"
    if p.kind == "assert" and p.antecedent:
        ant_guard = [x for x in guard if not x.startswith("$past(")]  # the antecedent is sampled now
        ag = f"if ({' && '.join(ant_guard)}) " if ant_guard else ""
        stmt += f" always {ev} {ag}{p.label}__antecedent: cover({p.antecedent});"
    return stmt


def needs_past(p: Prop) -> bool:
    return p.implication == "|=>" or bool(PAST_RE.search(p.body)) or bool(p.disable and PAST_RE.search(p.disable))


def shim(text: str, props: list[Prop], translate: bool, top: str | None = None, reset: str | None = None,
         strip: bool = False) -> str:
    """Return the file text ready for a formal or equivalence tool.

    translate: rewrite concurrent properties for free Yosys (unsupported ones are dropped).
    strip:     remove every property (for equivalence checking).
    reset:     expression true during reset; assumed in the first cycle of `top`.
    """
    out = text
    # edit back to front so offsets stay valid
    pv_declared: set[tuple[str | None, str]] = set()
    first_in_module: dict[tuple[str | None, str], Prop] = {}
    for p in props:
        if translate and not p.unsupported and needs_past(p):
            first_in_module.setdefault((p.module, p.clock or ""), p)
    for p in sorted(props, key=lambda x: x.start, reverse=True):
        pad = "\n" * p.text.count("\n")
        if strip:
            out = out[:p.start] + pad + out[p.end:]
            continue
        if not translate:
            if p.kind == "assert" and p.antecedent:
                dis = f" disable iff ({p.disable})" if p.disable else ""
                cov = f" {p.label}__antecedent: cover property (@({p.edge} {p.clock}){dis} {p.antecedent});"
                out = out[:p.end] + cov + out[p.end:]
            continue
        if p.unsupported:
            repl = f"/* control-eda: {p.label} not checked by free Yosys ({p.unsupported}) */"
        else:
            key = (p.module, p.clock or "")
            pv = f"__cea_pv_{(p.clock or 'clk').replace('.', '_')}" if needs_past(p) else None
            repl = _rewrite(p, pv)
            if pv and first_in_module.get(key) is p and key not in pv_declared:
                pv_declared.add(key)
                repl = f"reg {pv} = 1'b0; always @({p.edge} {p.clock}) {pv} <= 1'b1; " + repl
        out = out[:p.start] + repl + pad + out[p.end:]
    if reset and top:
        clean = blank_comments(out)
        m = re.search(rf"\bmodule\s+{re.escape(top)}\b", clean)
        if m:
            e = ENDMODULE_RE.search(clean, m.end())
            if e:
                out = out[:e.start()] + f"initial assume ({reset}); " + out[e.start():]
    return out


def labels(props: list[Prop], kind: str) -> list[str]:
    return [p.label for p in props if p.kind == kind]
