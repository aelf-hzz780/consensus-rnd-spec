#!/usr/bin/env python3
"""Detect supported native consensus backend entrypoints."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "native-capabilities.v2"
LATEST_CONTROLLER_COMMANDS = (
    "spawn-codex",
    "peek",
    "wakeup-plan",
    "wakeup-runner",
    "check-degradation",
    "daemon-status",
    "pr-checks",
    "release-gate",
)


def _entrypoint(path: Path, *, executable_required: bool) -> dict[str, Any]:
    exists = path.is_file()
    executable = os.access(path, os.X_OK) if exists else False
    usable = exists and (executable or not executable_required)
    return {
        "path": str(path),
        "exists": exists,
        "executable": executable,
        "usable": usable,
    }


def _cli_commands(cli_path: Path) -> dict[str, Any]:
    if not cli_path.is_file():
        return {"available": False, "commands": [], "supports_latest_controller": False, "missing_latest_controller": list(LATEST_CONTROLLER_COMMANDS)}
    text = cli_path.read_text(encoding="utf-8", errors="ignore")
    commands = sorted(set(re.findall(r'"([a-z][a-z0-9-]+)"\s*:\s*CommandSpec', text)))
    missing = [command for command in LATEST_CONTROLLER_COMMANDS if command not in commands]
    return {
        "available": bool(commands),
        "commands": commands,
        "supports_latest_controller": not missing,
        "missing_latest_controller": missing,
    }


def _script_contract(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"available": False}
    text = path.read_text(encoding="utf-8", errors="ignore")
    return {
        "available": True,
        "has_unattended_bypass": "--dangerously-bypass-approvals-and-sandbox" in text,
        "has_file_prompt_contract": "--prompt" in text and "SPAWN: prompt=" in text,
        "has_stall_supervision": "STALL_KILL_AFTER" in text or "--stall" in text,
    }


def detect_native_capabilities(skill_root: str | Path) -> dict[str, Any]:
    root = Path(skill_root).expanduser().resolve()
    payload: dict[str, Any] = {
        "backend": "native",
        "contract": CONTRACT_VERSION,
        "skill_root": str(root),
        "status": "blocked",
        "entrypoints": {},
    }

    if not (root / "SKILL.md").is_file():
        payload["reason"] = "NATIVE_CONSENSUS_SKILL_ROOT is invalid"
        return payload

    scripts = root / "scripts"
    legacy_cli = _entrypoint(scripts / "consensus-rnd-cli", executable_required=True)
    spawn_wrapper = _entrypoint(scripts / "spawn-codex.sh", executable_required=False)
    python_cli = root / "scripts" / "codex_refactor_loop" / "cli.py"
    wakeup_runner = root / "scripts" / "codex_refactor_loop" / "wakeup_runner.py"
    payload["entrypoints"] = {
        "legacy_cli": legacy_cli,
        "spawn_wrapper": spawn_wrapper,
    }
    payload["controller_surface"] = {
        "cli": _cli_commands(python_cli),
        "spawn_wrapper_contract": _script_contract(scripts / "spawn-codex.sh"),
        "wakeup_runner": {
            "path": str(wakeup_runner),
            "exists": wakeup_runner.is_file(),
        },
    }

    if legacy_cli["usable"]:
        payload.update(
            {
                "status": "ready",
                "entrypoint": "legacy-cli",
                "next": "delegate to codex-refactor-loop via consensus-rnd-cli spawn-codex",
                "supports_latest_controller": payload["controller_surface"]["cli"]["supports_latest_controller"],
            }
        )
        return payload

    if spawn_wrapper["usable"]:
        payload.update(
            {
                "status": "ready",
                "entrypoint": "spawn-wrapper",
                "next": "delegate to codex-refactor-loop via spawn-codex.sh",
                "supports_latest_controller": False,
            }
        )
        return payload

    payload["reason"] = "native backend entrypoint not found"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", required=True)
    args = parser.parse_args(argv)

    payload = detect_native_capabilities(args.skill_root)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
