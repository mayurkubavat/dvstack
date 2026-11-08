"""install: put the dv plugin where Cursor or Antigravity read project config.

Claude Code installs from the marketplace (`/plugin install dv@dvstack`). Cursor
and Antigravity have no local-marketplace path for a private repo, so this copies
the plugin's skills, agents, commands, rules and hooks into the project's own
agent folder and writes the environment's hooks file.

    control-eda install --env cursor        # -> .cursor/
    control-eda install --env antigravity   # -> .agents/
    control-eda install --env antigravity --global   # -> ~/.gemini/config/
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3] / "plugins" / "dv"


def _copy_tree(src: Path, dst: Path, dry: bool, log: list[str]) -> None:
    if not src.exists():
        return
    for p in sorted(src.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        target = dst / p.relative_to(src)
        log.append(f"copy {p.relative_to(PLUGIN)} -> {target}")
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def _merge_json(path: Path, new: dict, dry: bool, log: list[str], under: str | None = None) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            log.append(f"warning: {path} is not valid JSON; leaving it alone and writing {path.name}.dv instead")
            path = path.with_suffix(path.suffix + ".dv")
            existing = {}
    if under:
        existing.setdefault(under, {})
        existing[under].update(new)
    else:
        merged = dict(existing)
        for k, v in new.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        existing = merged
    log.append(f"write {path}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing, indent=2) + "\n")


def install(env: str, project: Path, global_scope: bool = False, dry_run: bool = False, plugin: Path = PLUGIN) -> dict:
    log: list[str] = []
    if env == "cursor":
        root = (Path.home() / ".cursor") if global_scope else (project / ".cursor")
        for sub in ("skills", "agents", "commands", "rules"):
            _copy_tree(plugin / sub, root / sub, dry_run, log)
        _copy_tree(plugin / "hooks", root / "hooks" / "dv", dry_run, log)
        hooks = json.loads((plugin / "hooks" / "cursor-hooks.json").read_text())
        prefix = ".cursor/hooks/dv/adapt.py" if not global_scope else str(root / "hooks" / "dv" / "adapt.py")
        for entries in hooks["hooks"].values():
            for e in entries:
                e["command"] = e["command"].replace("./hooks/adapt.py", prefix)
        _merge_json(root / "hooks.json", {"version": 1, "hooks": hooks["hooks"]}, dry_run, log)
        # Cursor rules want .mdc with alwaysApply; the same file works as .md too
        if not dry_run and (root / "rules" / "dv-gate.md").exists():
            (root / "rules" / "dv-gate.md").rename(root / "rules" / "dv-gate.mdc")
        log.append("rename rules/dv-gate.md -> rules/dv-gate.mdc")
        notes = ["Cursor reads .cursor/skills, agents, commands, rules and hooks.json from the project.",
                 "Hook tool names in Cursor are not fully documented; the matcher covers Write/Edit variants. Check Cursor Settings > Hooks shows 6 dv hooks."]
    elif env == "antigravity":
        root = (Path.home() / ".gemini" / "config") if global_scope else (project / ".agents")
        _copy_tree(plugin / "skills", root / "skills", dry_run, log)
        # Antigravity has no agents/ or commands/ folders: agents become skills, commands become workflows
        for agent in sorted((plugin / "agents").glob("*.md")):
            target = root / "skills" / agent.stem / "SKILL.md"
            log.append(f"copy agents/{agent.name} -> {target} (as a skill)")
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(agent, target)
        for cmd in sorted((plugin / "commands").glob("*.md")):
            target = root / "workflows" / cmd.name
            log.append(f"copy commands/{cmd.name} -> {target} (as a workflow)")
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cmd, target)
        _copy_tree(plugin / "rules", root / "rules", dry_run, log)
        _copy_tree(plugin / "hooks", root / "hooks" / "dv", dry_run, log)
        hooks = json.loads((plugin / "hooks" / "antigravity-hooks.json").read_text())
        if global_scope:
            text = json.dumps(hooks).replace(".agents/hooks/dv/adapt.py", str(root / "hooks" / "dv" / "adapt.py"))
            hooks = json.loads(text)
        _merge_json(root / "hooks.json", hooks, dry_run, log)
        notes = ["Antigravity reads .agents/skills, .agents/rules/*.md (frontmatter trigger: always_on) and .agents/hooks.json.",
                 "Antigravity tool names (write_to_file, replace_file_content, multi_replace_file_content) are mapped in hooks/adapt.py; the matcher is '.*' so unknown names still reach the hook and fall through as allow."]
    else:
        raise ValueError("env must be cursor or antigravity (Claude Code installs from the marketplace)")
    return {"ok": True, "env": env, "root": str(root), "dry_run": dry_run, "actions": log, "notes": notes}
