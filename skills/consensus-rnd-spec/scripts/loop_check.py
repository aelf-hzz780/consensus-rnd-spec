#!/usr/bin/env python3
"""Plan one consensus-rnd-spec loop turn without mutating the host repo."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

from backend_common import detect_backend, load_config, print_json


def count_inflight(repo: Path) -> int:
    result = subprocess.run(["ps", "axo", "command="], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return 0
    repo_str = str(repo)
    return sum(1 for line in result.stdout.splitlines() if "consensus-rnd-spec" in line and repo_str in line)


def build_plan(repo: Path) -> dict[str, Any]:
    config = load_config(repo)
    backend = detect_backend(config.repo_root, mode=config.backend_mode)
    inflight = count_inflight(config.repo_root)
    missing = max(0, config.code_floor - inflight)
    actions: list[dict[str, Any]] = [
        {"action": "reload_skill_contract", "status": "required"},
        {"action": "backend_detected", "backend": backend["backend"], "reason": backend["reason"]},
        {"action": "check_concurrency_floor", "actual": inflight, "floor": config.code_floor, "missing": missing},
    ]

    if backend["backend"] == "spec-kitty":
        if config.spec_kitty_full_loop_enable:
            actions.append({"action": "dispatch_spec_kitty_loop", "agent": config.spec_kitty_agent})
        else:
            actions.append({"action": "blocked", "reason": "SPEC_KITTY_FULL_LOOP_ENABLE is false"})
    elif backend["backend"] == "native":
        if config.native_full_loop_enable:
            actions.append({"action": "dispatch_native_consensus_loop", "skill_root": config.native_consensus_skill_root})
        else:
            actions.append({"action": "blocked", "reason": "NATIVE_FULL_LOOP_ENABLE is false"})
    else:
        actions.append({"action": "blocked", "reason": backend["reason"]})

    return {
        "repo_root": str(config.repo_root),
        "backend": backend,
        "concurrency": {"actual": inflight, "floor": config.code_floor, "missing": missing},
        "actions": actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Host repository root")
    args = parser.parse_args()
    print_json(build_plan(Path(args.repo).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
