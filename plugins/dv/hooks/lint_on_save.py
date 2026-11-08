#!/usr/bin/env python3
"""PostToolUse: lint the HDL file that was just written and feed errors back.

Also flags testbench anti-patterns the lint tools miss: hierarchical references
outside bind files, casex, and # delays outside clock/reset generators.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import dvhook  # noqa: E402

HIER_REF_RE = re.compile(r"^(?!\s*//).*\b(?:top|tb\w*|dut|u_\w+)\.(?:\w+\.)+\w+\s*(?:<=|=|;|\))", re.M)
CASEX_RE = re.compile(r"\bcasex\b")
DELAY_RE = re.compile(r"^(?!\s*//).*#\s*\d+", re.M)
CLKGEN_RE = re.compile(r"(always\s*#\s*\d+\s*\w*clk|forever\s*#|\brst_n?\s*=\s*1'b[01];?\s*$)", re.M)


def tb_checks(path: str, text: str) -> list[str]:
    notes = []
    name = Path(path).name
    is_bind = "bind" in name or "sva" in name
    if dvhook.is_testbench(path) and not is_bind and HIER_REF_RE.search(text):
        notes.append("hierarchical reference outside a bind/SVA file (sequences and tests must not touch DUT internals)")
    if CASEX_RE.search(text):
        notes.append("`casex` is banned; use `case` with `unique` or `case ... inside`")
    if not dvhook.is_testbench(path):
        delays = [m.group(0).strip() for m in DELAY_RE.finditer(text)]
        if delays and not CLKGEN_RE.search(text):
            notes.append(f"`#` delay in RTL ({delays[0]}); delays belong only in clock/reset generators")
    return notes


def main() -> None:
    ev = dvhook.read_event()
    path = dvhook.tool_file(ev) or ""
    if not dvhook.is_hdl(path) or not Path(path).exists():
        dvhook.allow()
    root = dvhook.project_root(ev)
    text = Path(path).read_text(errors="replace")
    notes = tb_checks(path, text)

    lint_summary = ""
    if dvhook.have_control_eda():
        code, out, err = dvhook.run(["control-eda", "lint", "--files", path, "--json"], root)
        try:
            data = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            data = {}
        errs = data.get("errors", [])
        if errs:
            lines = "\n".join(f"  {e.get('file','?')}:{e.get('line','?')}: {e.get('message','')}" for e in errs[:10])
            lint_summary = f"control-eda lint: {len(errs)} error(s)\n{lines}"
        elif code not in (0,):
            lint_summary = f"control-eda lint did not run cleanly: {err.strip()[:200]}"
    else:
        lint_summary = "control-eda not installed; lint skipped (install: pip install -e tools/control-eda)"

    msgs = []
    if lint_summary:
        msgs.append(lint_summary)
    if notes:
        msgs.append("dv conventions in " + path + ":\n  - " + "\n  - ".join(notes))
    if msgs:
        dvhook.allow("[dv:lint-on-save]\n" + "\n".join(msgs))
    dvhook.allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        dvhook.warn_and_allow(f"lint_on_save: {e}")
