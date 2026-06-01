#!/usr/bin/env python3
"""Detect supported native consensus backend entrypoints."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "native-capabilities.v1"


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
    payload["entrypoints"] = {
        "legacy_cli": legacy_cli,
        "spawn_wrapper": spawn_wrapper,
    }

    if legacy_cli["usable"]:
        payload.update(
            {
                "status": "ready",
                "entrypoint": "legacy-cli",
                "next": "delegate to codex-refactor-loop via consensus-rnd-cli spawn-codex",
            }
        )
        return payload

    if spawn_wrapper["usable"]:
        payload.update(
            {
                "status": "ready",
                "entrypoint": "spawn-wrapper",
                "next": "delegate to codex-refactor-loop via spawn-codex.sh",
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
