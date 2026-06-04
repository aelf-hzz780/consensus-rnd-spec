#!/usr/bin/env python3
"""Shared helpers for consensus-rnd-spec scripts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on"}
VALID_BACKENDS = {"auto", "spec-kitty", "native"}
VALID_KITTY_FLOW_ENFORCEMENT = {"strict", "off"}


@dataclass(frozen=True)
class HostConfig:
    repo_root: Path
    backend_mode: str
    code_floor: int
    loop_interval_seconds: int
    spec_kitty_agent: str
    spec_kitty_mission_type: str
    spec_kitty_full_loop_enable: bool
    spec_kitty_mission: str
    spec_kitty_scan_limit: int
    kitty_flow_enforcement: str
    native_full_loop_enable: bool
    native_consensus_skill_root: str
    synthetic_human_intake_enable: bool
    codex_model: str
    codex_extra_args: str


def parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def parse_host_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    export_re = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = export_re.match(line)
        if not match:
            continue
        key, raw_value = match.groups()
        value = raw_value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


def merged_env(repo: Path) -> dict[str, str]:
    values = dict(os.environ)
    values.update(parse_host_env(repo / ".consensus-rnd-spec" / "host.env"))
    return values


def load_config(repo: Path) -> HostConfig:
    env = merged_env(repo)
    backend_mode = env.get("BACKEND_MODE", "auto").strip()
    if backend_mode not in VALID_BACKENDS:
        backend_mode = "auto"
    try:
        code_floor = max(2, int(env.get("CODEX_FLOOR", "5")))
    except ValueError:
        code_floor = 5
    try:
        loop_interval_seconds = max(10, int(env.get("LOOP_INTERVAL_SECONDS", "600")))
    except ValueError:
        loop_interval_seconds = 600
    try:
        spec_kitty_scan_limit = max(1, int(env.get("SPEC_KITTY_SCAN_LIMIT", "30")))
    except ValueError:
        spec_kitty_scan_limit = 30
    kitty_flow_enforcement = env.get("KITTY_FLOW_ENFORCEMENT", "strict").strip().lower()
    if kitty_flow_enforcement not in VALID_KITTY_FLOW_ENFORCEMENT:
        kitty_flow_enforcement = "strict"
    repo_root = Path(env.get("REPO_ROOT") or str(repo)).expanduser().resolve()
    return HostConfig(
        repo_root=repo_root,
        backend_mode=backend_mode,
        code_floor=code_floor,
        loop_interval_seconds=loop_interval_seconds,
        spec_kitty_agent=env.get("SPEC_KITTY_AGENT", "codex"),
        spec_kitty_mission_type=env.get("SPEC_KITTY_MISSION_TYPE", "software-dev"),
        spec_kitty_full_loop_enable=parse_bool(env.get("SPEC_KITTY_FULL_LOOP_ENABLE"), default=True),
        spec_kitty_mission=env.get("SPEC_KITTY_MISSION", ""),
        spec_kitty_scan_limit=spec_kitty_scan_limit,
        kitty_flow_enforcement=kitty_flow_enforcement,
        native_full_loop_enable=parse_bool(env.get("NATIVE_FULL_LOOP_ENABLE"), default=False),
        native_consensus_skill_root=env.get("NATIVE_CONSENSUS_SKILL_ROOT", ""),
        synthetic_human_intake_enable=parse_bool(env.get("SYNTHETIC_HUMAN_INTAKE_ENABLE"), default=True),
        codex_model=env.get("CODEX_MODEL", ""),
        codex_extra_args=env.get("CODEX_EXTRA_ARGS", ""),
    )


def has_spec_kitty_files(repo: Path) -> bool:
    pyproject = repo / "pyproject.toml"
    pyproject_mentions = False
    if pyproject.exists():
        pyproject_mentions = "spec-kitty" in pyproject.read_text(encoding="utf-8", errors="ignore")
    return (repo / "kitty-specs").is_dir() or (repo / ".agents").is_dir() or pyproject_mentions


def spec_kitty_callable() -> bool:
    exe = shutil.which("spec-kitty")
    if not exe:
        return False
    result = subprocess.run([exe, "--help"], capture_output=True, text=True, check=False)
    return result.returncode == 0


def detect_backend(repo: Path, *, mode: str = "auto") -> dict[str, Any]:
    repo = repo.resolve()
    spec_files = has_spec_kitty_files(repo)
    spec_cli = spec_kitty_callable()
    spec_available = spec_files and spec_cli

    if mode == "spec-kitty":
        backend = "spec-kitty" if spec_available else "blocked"
        reason = "forced spec-kitty backend" if spec_available else "forced spec-kitty backend but Spec Kitty is unavailable"
    elif mode == "native":
        backend = "native"
        reason = "forced native backend"
    elif spec_available:
        backend = "spec-kitty"
        reason = "Spec Kitty project signals and CLI detected"
    else:
        backend = "native"
        reason = "Spec Kitty unavailable; falling back to native backend"

    return {
        "backend": backend,
        "reason": reason,
        "repo_root": str(repo),
        "signals": {
            "kitty_specs_dir": (repo / "kitty-specs").is_dir(),
            "agents_dir": (repo / ".agents").is_dir(),
            "pyproject_mentions_spec_kitty": "spec-kitty"
            in ((repo / "pyproject.toml").read_text(encoding="utf-8", errors="ignore") if (repo / "pyproject.toml").exists() else ""),
            "spec_kitty_cli": spec_cli,
        },
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def state_dir(repo: Path) -> Path:
    return repo / ".consensus-rnd-spec" / "state"


def append_event(repo: Path, event: dict[str, Any]) -> Path:
    out_dir = state_dir(repo)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "loop-events.jsonl"
    payload = dict(event)
    payload.setdefault("timestamp", utc_now())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_skill_contract(skill_root: Path) -> dict[str, Any]:
    skill_path = skill_root / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    return {
        "path": str(skill_path),
        "bytes": len(text.encode("utf-8")),
        "lines": len(text.splitlines()),
        "has_synthetic_human_guard": "synthetic_human_intake" in text and "maintainer approval" in text,
    }


def parse_duration_seconds(value: str | None, default_seconds: int) -> int:
    if not value:
        return default_seconds
    raw = value.strip().lower()
    match = re.fullmatch(r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?", raw)
    if not match:
        raise ValueError(f"invalid duration: {value}")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    if unit.startswith("h"):
        return amount * 3600
    if unit.startswith("m"):
        return amount * 60
    return amount
