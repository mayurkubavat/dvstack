#!/usr/bin/env python3
"""Run a dv hook under another agent environment.

The hook scripts speak Claude Code's protocol: a JSON event on stdin with
`tool_name`, `tool_input`, `cwd`, `prompt`, `stop_hook_active`; exit 0 allows,
exit 2 blocks with the reason on stderr; stdout is context for the agent.

Cursor and Google Antigravity use different payloads and reply with JSON on
stdout. This adapter translates both ways so the same hook logic runs in all
three environments:

    python3 adapt.py --env cursor no_weaken        # Cursor preToolUse
    python3 adapt.py --env antigravity evidence_gate  # Antigravity Stop

The environment's hooks file names the hook event; this script only needs the
hook name. Anything it cannot map falls through as "allow" (fail open).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Antigravity (Windsurf-lineage) tool names and their argument keys.
AG_WRITE_TOOLS = {"write_to_file": ("TargetFile", None, "CodeContent"),
                  "replace_file_content": ("TargetFile", "TargetContent", "ReplacementContent"),
                  "multi_replace_file_content": ("TargetFile", None, None),
                  "edit_file": ("TargetFile", None, "CodeContent")}


def read_stdin() -> dict:
    raw = sys.stdin.read()
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


# ---------- Cursor -> Claude-shaped event ----------

def from_cursor(p: dict) -> tuple[dict, str]:
    ev_name = p.get("hook_event_name", "")
    cwd = p.get("cwd") or (p.get("workspace_roots") or [None])[0] or ""
    ev: dict = {"cwd": cwd}
    if ev_name == "preToolUse" or ev_name == "postToolUse":
        ti = dict(p.get("tool_input") or {})
        name = p.get("tool_name") or ""
        # Cursor's write tool may call the body `contents`; the hooks read `content`.
        if "contents" in ti and "content" not in ti:
            ti["content"] = ti["contents"]
        if "path" in ti and "file_path" not in ti:
            ti["file_path"] = ti["path"]
        ev.update({"tool_name": "Write" if name.lower() in ("write", "write_file", "edit", "edit_file", "strreplace", "str_replace") and "old_string" not in ti else ("Edit" if "old_string" in ti else name), "tool_input": ti})
        return ev, "tool"
    if ev_name == "afterFileEdit":
        ev.update({"tool_name": "MultiEdit", "tool_input": {"file_path": p.get("file_path"), "edits": p.get("edits") or []}})
        return ev, "post"
    if ev_name == "beforeSubmitPrompt":
        ev.update({"prompt": p.get("prompt", "")})
        return ev, "prompt"
    if ev_name == "stop":
        ev.update({"stop_hook_active": int(p.get("loop_count") or 0) > 0})
        return ev, "stop"
    if ev_name == "sessionStart":
        return ev, "session"
    return ev, "unknown"


def to_cursor(kind: str, code: int, out: str, err: str) -> dict:
    blocked = code == 2
    if kind == "tool":
        return {"permission": "deny", "user_message": err.strip(), "agent_message": err.strip()} if blocked else {"permission": "allow"}
    if kind == "post":
        return {"additional_context": out.strip()} if out.strip() else {}
    if kind == "prompt":
        return {"continue": True}
    if kind == "stop":
        return {"followup_message": err.strip()} if blocked else {}
    if kind == "session":
        return {"additional_context": out.strip()} if out.strip() else {}
    return {}


# ---------- Antigravity -> Claude-shaped event ----------

def from_antigravity(p: dict) -> tuple[dict, str]:
    cwd = (p.get("workspacePaths") or [None])[0] or ""
    ev: dict = {"cwd": cwd}
    call = p.get("toolCall")
    if call is not None:
        name = call.get("name", "")
        args = call.get("args") or {}
        spec = AG_WRITE_TOOLS.get(name)
        ti: dict = {}
        if spec:
            fkey, okey, nkey = spec
            ti["file_path"] = args.get(fkey)
            if name == "multi_replace_file_content":
                ti["edits"] = [{"old_string": c.get("TargetContent", ""), "new_string": c.get("ReplacementContent", "")} for c in args.get("ReplacementChunks") or []]
                tool = "MultiEdit"
            elif okey:
                ti["old_string"], ti["new_string"] = args.get(okey, ""), args.get(nkey, "")
                tool = "Edit"
            else:
                ti["content"] = args.get(nkey, "")
                tool = "Write"
        else:
            # generic fallback: common key names
            ti["file_path"] = args.get("file_path") or args.get("path") or args.get("TargetFile")
            if "old_string" in args:
                ti.update({"old_string": args["old_string"], "new_string": args.get("new_string", "")})
                tool = "Edit"
            else:
                ti["content"] = args.get("content") or args.get("contents") or ""
                tool = name
        ev.update({"tool_name": tool, "tool_input": ti})
        return ev, "post" if p.get("error") is not None or "error" in p else "tool"
    if "fullyIdle" in p or "terminationReason" in p:
        ev.update({"stop_hook_active": int(p.get("executionNum") or 0) > 1})
        return ev, "stop"
    if "lastUserInput" in p and p.get("stepIdx") is None and "invocationNum" in p:
        ev.update({"prompt": p.get("lastUserInput", "")})
        return ev, "prompt"
    return ev, "session"


def to_antigravity(kind: str, code: int, out: str, err: str) -> dict:
    blocked = code == 2
    if kind == "tool":
        return {"decision": "deny", "reason": err.strip()} if blocked else {"decision": "allow"}
    if kind == "stop":
        return {"decision": "continue", "reason": err.strip()} if blocked else {}
    if kind in ("session", "prompt"):
        msg = out.strip() or err.strip()
        return {"injectSteps": [{"systemMessage": msg}]} if msg else {}
    if kind == "post":
        if out.strip():
            sys.stderr.write(out)
        return {}
    return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", choices=["cursor", "antigravity"], required=True)
    ap.add_argument("hook", help="hook script name without .py, e.g. no_weaken")
    a = ap.parse_args()
    payload = read_stdin()
    try:
        ev, kind = from_cursor(payload) if a.env == "cursor" else from_antigravity(payload)
        script = HERE / f"{a.hook}.py"
        p = subprocess.run([sys.executable, str(script)], input=json.dumps(ev), capture_output=True, text=True, timeout=60, cwd=ev.get("cwd") or None)
        reply = to_cursor(kind, p.returncode, p.stdout, p.stderr) if a.env == "cursor" else to_antigravity(kind, p.returncode, p.stdout, p.stderr)
    except Exception as e:  # fail open, loudly
        sys.stderr.write(f"[dv adapt] {e}\n")
        reply = {"permission": "allow"} if a.env == "cursor" else {"decision": "allow"}
    print(json.dumps(reply))
    sys.exit(0)


if __name__ == "__main__":
    main()
