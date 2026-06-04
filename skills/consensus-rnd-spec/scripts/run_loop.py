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
from spec_backend import clear_pending_result, plan_next, write_pending_result
from discovery import produce as produce_discovery
from promote_discovery import promote as promote_discovery
from native_capabilities import detect_native_capabilities
from github_sync import (
    PHASE_IMPLEMENTING,
    PHASE_REVIEWING,
    ensure_child_issues,
    open_or_update_mission_pr,
    sync_wp_status,
)


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def codex_base_command(repo: Path) -> list[str]:
    return ["codex", "exec", "--cd", str(repo), "--dangerously-bypass-approvals-and-sandbox"]


def codex_command(repo: Path, prompt_file: str, config) -> list[str]:
    command = codex_base_command(repo)
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    if config.codex_extra_args:
        command.extend(shlex.split(config.codex_extra_args))
    command.append(Path(prompt_file).read_text(encoding="utf-8"))
    return command


def codex_prompt_command(repo: Path, prompt_text: str, config) -> list[str]:
    command = codex_base_command(repo)
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    if config.codex_extra_args:
        command.extend(shlex.split(config.codex_extra_args))
    command.append(prompt_text)
    return command


def run_command(command: list[str], repo: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def worker_result_value(result: dict[str, Any]) -> str:
    if result.get("returncode") == 0:
        return "success"
    return "failed"


def record_worker_pending(repo: Path, plan: dict[str, Any], worker_result: dict[str, Any], *, action: str | None = None) -> dict[str, Any]:
    decision_payload = plan.get("decision", {}).get("payload", {})
    mission = (
        decision_payload.get("mission_slug")
        or plan.get("chosen", {}).get("mission")
        or plan.get("pending_result", {}).get("mission_slug")
    )
    if not mission:
        return {"status": "not-recorded", "reason": "missing mission_slug"}
    payload = {
        "mission_slug": str(mission),
        "result": worker_result_value(worker_result),
        "completed_action": action or plan.get("action"),
        "execution_kind": plan.get("execution_kind"),
        "worker_returncode": worker_result.get("returncode"),
    }
    path = write_pending_result(repo, payload)
    return {"status": "recorded", "path": str(path), "payload": payload}


def plan_mission(plan: dict[str, Any]) -> str:
    decision_payload = plan.get("decision", {}).get("payload", {})
    return str(
        decision_payload.get("mission_slug")
        or plan.get("chosen", {}).get("mission")
        or plan.get("pending_result", {}).get("mission_slug")
        or ""
    )


def wp_phase_for_action(action: str | None) -> str:
    if action == "review":
        return PHASE_REVIEWING
    return PHASE_IMPLEMENTING


def execute_spec_kitty_action(repo: Path, plan: dict[str, Any], config) -> dict[str, Any]:
    if plan.get("status") == "discovery_needed":
        promotion = promote_discovery(repo, execute=False)
        if promotion.get("status") == "planned":
            executed = promote_discovery(repo, execute=True)
            return {"status": "discovery-promoted", "promotion": executed}
        return {"status": "discovery-produced", "discovery": produce_discovery(repo)}
    execution_kind = plan.get("execution_kind")
    if execution_kind == "kitty-next-step":
        advance_command = plan.get("advance_command")
        if not advance_command:
            return {"status": "noop", "reason": "no Spec Kitty next command"}
        advance_result = run_command(advance_command, repo)
        if advance_result["returncode"] == 0:
            clear_pending_result(repo)
        try:
            decision_payload = json.loads(advance_result["stdout_tail"]) if advance_result["stdout_tail"].strip() else {}
        except json.JSONDecodeError:
            decision_payload = {}
        prompt_file = decision_payload.get("prompt_file") if isinstance(decision_payload, dict) else None
        if advance_result["returncode"] != 0 or not prompt_file:
            return {"status": "kitty-next-only", "advance_result": advance_result, "decision": decision_payload}
        worker_command = codex_command(repo, str(prompt_file), config)
        worker_result = run_command(worker_command, repo)
        pending_plan = {**plan, "decision": {"payload": decision_payload}}
        pending = record_worker_pending(repo, pending_plan, worker_result, action=decision_payload.get("action"))
        return {
            "status": "worker-finished",
            "execution_kind": execution_kind,
            "advance_result": advance_result,
            "decision": decision_payload,
            "worker_result": worker_result,
            "pending_result": pending,
        }

    if execution_kind == "kitty-prompt-file":
        prompt_file = plan.get("prompt_file")
        if not prompt_file:
            return {"status": "noop", "reason": "no Spec Kitty prompt_file"}
        worker_command = codex_command(repo, str(prompt_file), config)
        worker_result = run_command(worker_command, repo)
        pending = record_worker_pending(repo, plan, worker_result)
        return {"status": "worker-finished", "execution_kind": execution_kind, "worker_result": worker_result, "pending_result": pending}

    action_command = plan.get("action_command")
    if not action_command:
        return {"status": "noop", "reason": "no actionable Spec Kitty command"}
    action_result = run_command(action_command, repo)
    if execution_kind == "kitty-agent-action":
        mission = plan_mission(plan)
        wp_id = str(plan.get("wp_id") or plan.get("decision", {}).get("payload", {}).get("wp_id") or "")
        action = str(plan.get("action") or plan.get("decision", {}).get("payload", {}).get("action") or "")
        github_before = None
        github_after = None
        github_pr = None
        if action_result["returncode"] != 0:
            return {
                "status": "action-command-only",
                "execution_kind": execution_kind,
                "action_result": action_result,
                "github_before": None,
            }
        prompt_text = action_result.get("stdout_tail") or ""
        if not prompt_text.strip():
            return {
                "status": "action-command-only",
                "execution_kind": execution_kind,
                "action_result": action_result,
                "github_before": None,
            }
        if mission and wp_id:
            github_before = sync_wp_status(
                repo,
                mission,
                wp_id,
                wp_phase_for_action(action),
                detail=f"Spec Kitty {action or 'agent'} action dispatched.",
                execute=True,
            )
            if github_before.get("status") not in {"synced", "disabled"}:
                return {
                    "status": "blocked",
                    "reason": "GitHub status sync failed before worker dispatch",
                    "execution_kind": execution_kind,
                    "action_result": action_result,
                    "github_before": github_before,
                }
        worker_command = codex_prompt_command(repo, prompt_text, config)
        worker_result = run_command(worker_command, repo)
        pending = record_worker_pending(repo, plan, worker_result)
        if mission and wp_id:
            github_after = sync_wp_status(
                repo,
                mission,
                wp_id,
                PHASE_REVIEWING if action == "implement" and worker_result.get("returncode") == 0 else wp_phase_for_action(action),
                detail=f"Spec Kitty {action or 'agent'} worker completed with returncode {worker_result.get('returncode')}.",
                execute=True,
            )
            github_pr = open_or_update_mission_pr(repo, mission, execute=True)
        return {
            "status": "worker-finished",
            "execution_kind": execution_kind,
            "action_result": action_result,
            "worker_result": worker_result,
            "pending_result": pending,
            "github_before": github_before,
            "github_after": github_after,
            "github_pr": github_pr,
        }
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
    if not skill_root_value:
        return {"backend": "native", "status": "blocked", "reason": "NATIVE_CONSENSUS_SKILL_ROOT is invalid"}
    return detect_native_capabilities(skill_root_value)


def native_companion_plan(config, backend: dict[str, Any]) -> dict[str, Any] | None:
    if backend.get("backend") != "spec-kitty" or not config.native_full_loop_enable:
        return None
    plan = native_plan(config)
    companion: dict[str, Any] = {
        "backend": "native",
        "role": "companion",
        "kitty_flow_enforcement": config.kitty_flow_enforcement,
        "status": "blocked",
        "reason": "Spec Kitty owns mission, WP, implementation, review, merge, and acceptance for this repository",
        "native_plan": plan,
    }
    if plan.get("status") != "ready":
        companion["reason"] = plan.get("reason", companion["reason"])
        return companion
    if config.kitty_flow_enforcement == "strict":
        companion["allowed_actions"] = ["capability-detection", "read-only-status", "intake-artifact-production"]
        companion["forbidden_actions"] = ["native-implementation", "native-review", "native-merge", "native-release"]
        return companion
    companion["status"] = "ready"
    companion["reason"] = "KITTY_FLOW_ENFORCEMENT=off permits native lifecycle delegation"
    return companion


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
        companion = native_companion_plan(config, backend)
        if companion is not None:
            turn["native_companion"] = companion
        if not config.spec_kitty_full_loop_enable:
            turn["dispatches"].append({"status": "blocked", "reason": "SPEC_KITTY_FULL_LOOP_ENABLE is false"})
        else:
            for _ in range(max(1, missing)):
                plan = plan_next(config.repo_root)
                mission = plan.get("chosen", {}).get("mission")
                if isinstance(mission, str) and mission:
                    plan = dict(plan)
                    plan["github_children"] = ensure_child_issues(config.repo_root, mission, execute=False)
                if plan.get("status") == "discovery_needed":
                    promotion = promote_discovery(config.repo_root, execute=False)
                    if promotion.get("status") == "planned":
                        plan = dict(plan)
                        plan["promotion"] = promotion
                item: dict[str, Any] = {"plan": plan}
                if execute and plan.get("status") in {"ready", "discovery_needed"}:
                    item["execution"] = execute_spec_kitty_action(config.repo_root, plan, config)
                turn["dispatches"].append(item)
                if not execute or plan.get("execution_kind") in {"kitty-next-step", "kitty-prompt-file", "kitty-agent-action"}:
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
