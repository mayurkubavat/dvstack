"""probe: turn hook-payload capture on or off, and summarise what a session sent."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def enable(root: Path) -> dict:
    (root / "evidence").mkdir(exist_ok=True)
    (root / "evidence" / ".hook-probe").write_text("hooks/adapt.py appends raw payloads to hook-probe.jsonl while this file exists\n")
    return {"ok": True, "enabled": True, "log": "evidence/hook-probe.jsonl"}


def disable(root: Path) -> dict:
    m = root / "evidence" / ".hook-probe"
    if m.exists():
        m.unlink()
    return {"ok": True, "enabled": False}


def report(root: Path) -> dict:
    log = root / "evidence" / "hook-probe.jsonl"
    if not log.exists():
        return {"ok": False, "error": "no evidence/hook-probe.jsonl", "hint": "control-eda probe on, then run one agent session that edits an RTL file"}
    recs = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    tools = Counter()
    unmapped = []
    blocked = []
    for r in recs:
        raw = r["raw"]
        name = raw.get("tool_name") or (raw.get("toolCall") or {}).get("name")
        if name:
            tools[name] += 1
            if r["kind"] == "tool" and not (r["translated"].get("tool_input") or {}).get("file_path"):
                keys = sorted((raw.get("tool_input") or (raw.get("toolCall") or {}).get("args") or {}).keys())
                unmapped.append({"tool": name, "arg_keys": keys})
        rep = r.get("reply") or {}
        if rep.get("permission") == "deny" or rep.get("decision") in ("deny", "continue") or rep.get("followup_message"):
            blocked.append({"hook": r["hook"], "tool": name})
    envs = sorted({r["env"] for r in recs})
    events = Counter(r["kind"] for r in recs)
    return {
        "ok": True,
        "env": envs,
        "records": len(recs),
        "events": dict(events),
        "tools_seen": dict(tools),
        "edit_tools_not_mapped": [dict(t) for t in {json.dumps(u, sort_keys=True): u for u in unmapped}.values()],
        "blocks": blocked,
        "verdict": "hooks saw every edit tool" if not unmapped else "some edit tools were not recognised; send this report and hooks/adapt.py will be taught their names",
    }
