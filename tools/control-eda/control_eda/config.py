"""Find the project root and load dv.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


def find_root(start: Path | None = None) -> Path:
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / "dv.toml").exists():
            return d
    raise ConfigError(
        "no dv.toml found here or above. Create one (see examples/fifo_block/dv.toml) "
        "or run from inside a dv-enabled project."
    )


@dataclass
class Config:
    root: Path
    raw: dict[str, Any]

    @property
    def project(self) -> dict[str, Any]:
        return self.raw.get("project", {})

    @property
    def sim(self) -> dict[str, Any]:
        return self.raw.get("sim", {})

    @property
    def lint(self) -> dict[str, Any]:
        return self.raw.get("lint", {})

    @property
    def cov(self) -> dict[str, Any]:
        return self.raw.get("cov", {})

    def test(self, name: str) -> dict[str, Any]:
        tests = self.raw.get("tests", {})
        if name not in tests:
            known = ", ".join(sorted(tests)) or "(none)"
            raise ConfigError(f"test '{name}' is not in dv.toml [tests]. Known tests: {known}")
        return tests[name]

    @property
    def tests(self) -> list[str]:
        return sorted(self.raw.get("tests", {}))

    @property
    def sources(self) -> list[str]:
        return list(self.sim.get("sources", []))

    @property
    def evidence_dir(self) -> Path:
        d = self.root / "evidence"
        d.mkdir(exist_ok=True)
        return d

    @property
    def build_dir(self) -> Path:
        d = self.root / self.sim.get("build_dir", "build")
        d.mkdir(exist_ok=True)
        return d


def load(start: Path | None = None) -> Config:
    root = find_root(start)
    with open(root / "dv.toml", "rb") as f:
        raw = tomllib.load(f)
    return Config(root=root, raw=raw)
