#!/usr/bin/env python3
"""Run the consensus-rnd-spec controller loop."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from backend_common import (
    append_event,
    detect_backend,
    load_config,
    parse_duration_seconds,
    print_json,
    read_skill_contract,
    utc_now,
)
from loop_check import count_inflight
from spec_backend import plan_next
from discovery import produce as produce_discovery
from promote_discovery import promote as promote_discovery


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def codex_command(repo: Path, prompt_file: str, config) -> list[str]:
    command = ["codex", "exec", "--cd", str(repo), "--ask-for-approval", "never"]
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    if config.codex_extra_args:
        command.extend(shlex.split(config.codex_extra_args))
    command.append(Path(prompt_file).read_text(encoding="utf-8"))
    return command


def run_command(command: list[str], repo: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def execute_spec_kitty_action(repo: Path, plan: dict[str, Any], config) -> dict[str, Any]:
    if plan.get("status") == "discovery_needed":
        promotion = promote_discovery(repo, execute=False)
        if promotion.get("status") == "planned":
            executed = promote_discovery(repo, execute=True)
            return {"status": "discovery-promoted", "promotion": executed}
        return {"status": "discovery-produced", "discovery": produce_discovery(repo)}
    action_command = plan.get("action_command")
    if not action_command:
        return {"status": "noop", "reason": "no actionable Spec Kitty command"}
    action_result = run_command(action_command, repo)
    decision_payload = plan.get("decision", {}).get("payload", {})
    prompt_file = decision_payload.get("prompt_file")
    if action_result["returncode"] != 0 or not prompt_file:
        return {"status": "action-command-only", "action_result": action_result}
    worker_command = codex_command(repo, str(prompt_file), config)
    worker_result = run_command(worker_command, repo)
    return {"status": "worker-finished", "action_result": action_result, "worker_result": worker_result}


def native_plan(config) -> dict[str, Any]:
    if not config.native_full_loop_enable:
        return {"backend": "native", "status": "blocked", "reason": "NATIVE_FULL_LOOP_ENABLE is false"}
    skill_root_value = config.native_consensus_skill_root
    if not skill_root_value or not (Path(skill_root_value) / "SKILL.md").exists():
        return {"backend": "native", "status": "blocked", "reason": "NATIVE_CONSENSUS_SKILL_ROOT is invalid"}
    return {"backend": "native", "status": "ready", "skill_root": skill_root_value}


def run_native(config) -> dict[str, Any]:
    plan = native_plan(config)
    if plan.get("status") != "ready":
        return plan
    script = skill_root() / "scripts" / "native_backend.sh"
    if not script.exists():
        return {"backend": "native", "status": "blocked", "reason": "native_backend.sh not found"}
    return run_command(["bash", str(script), "run"], config.repo_root)


def loop_turn(repo: Path, *, execute: bool) -> dict[str, Any]:
    config = load_config(repo)
    contract = read_skill_contract(skill_root())
    backend = detect_backend(config.repo_root, mode=config.backend_mode)
    inflight = count_inflight(config.repo_root)
    missing = max(0, config.code_floor - inflight)
    turn: dict[str, Any] = {
        "timestamp": utc_now(),
        "repo_root": str(config.repo_root),
        "execute": execute,
        "skill_contract": contract,
        "backend": backend,
        "concurrency": {"actual": inflight, "floor": config.code_floor, "missing": missing},
        "dispatches": [],
    }

    if backend["backend"] == "spec-kitty":
        if not config.spec_kitty_full_loop_enable:
            turn["dispatches"].append({"status": "blocked", "reason": "SPEC_KITTY_FULL_LOOP_ENABLE is false"})
        else:
            for _ in range(max(1, missing)):
                plan = plan_next(config.repo_root)
                if plan.get("status") == "discovery_needed":
                    promotion = promote_discovery(config.repo_root, execute=False)
                    if promotion.get("status") == "planned":
                        plan = dict(plan)
                        plan["promotion"] = promotion
                item: dict[str, Any] = {"plan": plan}
                if execute and plan.get("status") in {"ready", "discovery_needed"}:
                    item["execution"] = execute_spec_kitty_action(config.repo_root, plan, config)
                turn["dispatches"].append(item)
                if not execute:
                    break
    elif backend["backend"] == "native":
        for _ in range(max(1, missing)):
            plan = native_plan(config)
            item = {"plan": plan}
            if execute and plan.get("status") == "ready":
                item["execution"] = run_native(config)
            turn["dispatches"].append(item)
            if not execute:
                break
    else:
        turn["dispatches"].append({"status": "blocked", "reason": backend["reason"]})

    append_event(config.repo_root, {"type": "loop_turn", "payload": turn})
    return turn


def run_loop(repo: Path, duration_seconds: int, *, execute: bool, once: bool) -> dict[str, Any]:
    config = load_config(repo)
    started = time.time()
    turns: list[dict[str, Any]] = []
    while True:
        turns.append(loop_turn(config.repo_root, execute=execute))
        if once or time.time() - started >= duration_seconds:
            break
        sleep_for = min(config.loop_interval_seconds, max(1, duration_seconds - int(time.time() - started)))
        time.sleep(sleep_for)
    return {"turns": turns, "turn_count": len(turns), "execute": execute}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--duration", default=None, help="Examples: 10min, 600s, 1h")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Perform backend actions. Default is dry-run planning.")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    config = load_config(repo)
    duration = parse_duration_seconds(args.duration, config.loop_interval_seconds)
    print_json(run_loop(repo, duration, execute=args.execute, once=args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
