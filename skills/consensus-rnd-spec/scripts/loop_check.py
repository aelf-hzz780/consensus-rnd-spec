#!/usr/bin/env python3
"""Plan one consensus-rnd-spec loop turn without mutating the host repo."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path
from typing import Any

from backend_common import detect_backend, load_config, print_json


def count_inflight(repo: Path) -> int:
    result = subprocess.run(["ps", "axo", "command="], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return 0
    repo_resolved = repo.resolve()
    workers: set[tuple[str, str, str]] = set()
    for line in result.stdout.splitlines():
        worker = _spawn_codex_worker_key(line, repo_resolved)
        if worker is not None:
            workers.add(worker)
    return len(workers)


def _spawn_codex_worker_key(command: str, repo: Path) -> tuple[str, str, str] | None:
    try:
        args = shlex.split(command)
    except ValueError:
        args = command.split()
    if "spawn-codex" not in args:
        return None
    if not any(Path(arg).name == "consensus-rnd-cli" for arg in args):
        return None

    cd = _option_value(args, "--cd")
    if cd is None:
        return None
    try:
        if Path(cd).resolve() != repo:
            return None
    except OSError:
        if str(Path(cd)) != str(repo):
            return None

    prompt = _option_value(args, "--prompt") or ""
    log = _option_value(args, "--log") or ""
    return (str(Path(cd)), prompt, log or command)


def _option_value(args: list[str], name: str) -> str | None:
    prefix = f"{name}="
    for index, arg in enumerate(args):
        if arg.startswith(prefix):
            return arg[len(prefix) :]
        if arg == name and index + 1 < len(args):
            return args[index + 1]
    return None


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
