#!/usr/bin/env python3
"""Spec Kitty backend adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from backend_common import load_config, print_json


def evidence_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def plan_handoff(repo: Path, title: str, source_issue: str | None, audit_artifact: str | None) -> dict[str, Any]:
    config = load_config(repo)
    seed = {
        "title": title,
        "source": "consensus-rnd-spec",
        "source_issue": source_issue,
        "audit_artifact": audit_artifact,
        "evidence_hash": evidence_hash("|".join([title, source_issue or "", audit_artifact or ""])),
        "mission_type": config.spec_kitty_mission_type,
        "synthetic_human_intake": config.synthetic_human_intake_enable,
    }
    return {
        "backend": "spec-kitty",
        "repo_root": str(config.repo_root),
        "seed": seed,
        "next_commands": [
            [
                "spec-kitty",
                "specify",
                title,
                "--mission-type",
                config.spec_kitty_mission_type,
                "--json",
            ],
            [
                "spec-kitty",
                "next",
                "--agent",
                config.spec_kitty_agent,
                "--mission",
                "<mission-slug>",
                "--json",
            ],
        ],
    }


def run_specify(repo: Path, title: str) -> dict[str, Any]:
    config = load_config(repo)
    command = ["spec-kitty", "specify", title, "--mission-type", config.spec_kitty_mission_type, "--json"]
    result = subprocess.run(command, cwd=config.repo_root, capture_output=True, text=True, check=False)
    payload: dict[str, Any]
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout}
    return {"command": command, "returncode": result.returncode, "result": payload, "stderr": result.stderr}


def run_json(command: list[str], repo: Path) -> dict[str, Any]:
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout}
    return {"command": command, "returncode": result.returncode, "payload": payload, "stderr": result.stderr}


def list_mission_slugs(repo: Path, *, limit: int | None = None) -> list[str]:
    specs = repo / "kitty-specs"
    if not specs.is_dir():
        return []
    slugs: list[str] = []
    meta_paths = sorted(specs.glob("*/meta.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for meta_path in meta_paths[:limit]:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        slug = data.get("slug") or data.get("mission_slug") or meta_path.parent.name
        if isinstance(slug, str) and slug:
            slugs.append(slug)
    return slugs


def local_lane_summary(mission_dir: Path) -> dict[str, int]:
    events_path = mission_dir / "status.events.jsonl"
    lanes: dict[str, str] = {}
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            wp_id = event.get("wp_id")
            to_lane = event.get("to_lane")
            if isinstance(wp_id, str) and isinstance(to_lane, str):
                lanes[wp_id] = to_lane
    if not lanes:
        task_dir = mission_dir / "tasks"
        for path in task_dir.glob("WP*.md"):
            wp_id = path.name.split("-", 1)[0]
            lanes[wp_id] = "planned"
    summary = {lane: 0 for lane in ("planned", "claimed", "in_progress", "for_review", "in_review", "approved", "done", "blocked", "canceled")}
    for lane in lanes.values():
        summary[lane] = summary.get(lane, 0) + 1
    return summary


def local_actionable_score(summary: dict[str, int]) -> int:
    return (
        int(summary.get("for_review") or 0) * 100
        + int(summary.get("planned") or 0) * 80
        + int(summary.get("in_progress") or 0) * 40
        + int(summary.get("claimed") or 0) * 20
    )


def local_actionable_missions(repo: Path, *, limit: int) -> list[dict[str, Any]]:
    specs = repo / "kitty-specs"
    if not specs.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    meta_paths = sorted(specs.glob("*/meta.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for meta_path in meta_paths[:limit]:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        slug = data.get("slug") or data.get("mission_slug") or meta_path.parent.name
        if not isinstance(slug, str) or not slug:
            continue
        summary = local_lane_summary(meta_path.parent)
        score = local_actionable_score(summary)
        if score > 0:
            candidates.append({"mission": slug, "summary": summary, "score": score, "source": "local-scan"})
    return sorted(candidates, key=lambda item: int(item["score"]), reverse=True)


def mission_state(repo: Path, mission: str) -> dict[str, Any]:
    command = ["spec-kitty", "orchestrator-api", "mission-state", "--mission", mission]
    return run_json(command, repo)


def score_mission_state(state: dict[str, Any]) -> int:
    if not state.get("success"):
        return -1
    summary = state.get("data", {}).get("summary", {})
    if not isinstance(summary, dict):
        return -1
    score = 0
    score += int(summary.get("for_review") or 0) * 100
    score += int(summary.get("planned") or 0) * 80
    score += int(summary.get("in_progress") or 0) * 40
    score += int(summary.get("claimed") or 0) * 20
    return score


def mission_has_actionable_work(state: dict[str, Any]) -> bool:
    return score_mission_state(state) > 0


def choose_mission(repo: Path, preferred: str = "", *, scan_limit: int = 30) -> dict[str, Any]:
    if preferred:
        state = mission_state(repo, preferred)
        return {"mission": preferred, "state": state, "source": "configured"}
    best: dict[str, Any] | None = None
    for local in local_actionable_missions(repo, limit=scan_limit):
        slug = str(local["mission"])
        state = mission_state(repo, slug)
        payload = state.get("payload", {})
        score = score_mission_state(payload)
        if not mission_has_actionable_work(payload):
            continue
        candidate = {"mission": slug, "state": state, "source": "scan", "score": score, "local": local}
        if best is None or score > int(best.get("score", -1)):
            best = candidate
    if best is None:
        return {"mission": None, "state": None, "source": "none", "score": -1}
    return best


def next_decision(repo: Path, mission: str, agent: str, *, result: str | None = None) -> dict[str, Any]:
    command = ["spec-kitty", "next", "--agent", agent, "--mission", mission, "--json"]
    if result:
        command.extend(["--result", result])
    return run_json(command, repo)


def action_command(decision: dict[str, Any], agent: str) -> list[str] | None:
    payload = decision.get("payload", {})
    action = payload.get("action")
    mission = payload.get("mission_slug")
    wp_id = payload.get("wp_id")
    if not action or not mission:
        return None
    if action == "implement":
        command = ["spec-kitty", "agent", "action", "implement"]
    elif action == "review":
        command = ["spec-kitty", "agent", "action", "review"]
    else:
        return None
    if wp_id:
        command.append(str(wp_id))
    command.extend(["--mission", str(mission), "--agent", agent])
    return command


def plan_next(repo: Path) -> dict[str, Any]:
    config = load_config(repo)
    chosen = choose_mission(config.repo_root, config.spec_kitty_mission, scan_limit=config.spec_kitty_scan_limit)
    mission = chosen.get("mission")
    if not mission:
        return {
            "backend": "spec-kitty",
            "status": "discovery_needed",
            "reason": "no Spec Kitty mission has actionable WP work",
            "chosen": chosen,
            "discovery_command": ["python3", "<skill-root>/scripts/discovery.py", "--repo", str(config.repo_root)],
        }
    decision = next_decision(config.repo_root, str(mission), config.spec_kitty_agent)
    command = action_command(decision, config.spec_kitty_agent)
    return {
        "backend": "spec-kitty",
        "status": "ready" if command else "waiting",
        "chosen": chosen,
        "decision": decision,
        "action_command": command,
    }


def write_discovery_seed(repo: Path, *, title: str, body: str, source: str = "synthetic_human_intake") -> dict[str, Any]:
    config = load_config(repo)
    runs = config.repo_root / ".consensus-rnd-spec" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe = "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-")[:60] or "discovery"
    path = runs / f"discovery-{stamp}-{safe}.json"
    payload = {
        "title": title,
        "body": body,
        "source": source,
        "producer": "consensus-rnd-spec",
        "synthetic_human_intake": source == "synthetic_human_intake",
        "mission_type": config.spec_kitty_mission_type,
        "evidence_hash": evidence_hash(title + "\n" + body),
        "created_at": stamp,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"artifact": str(path), "payload": payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--repo", default=".")
    plan.add_argument("--title", required=True)
    plan.add_argument("--source-issue")
    plan.add_argument("--audit-artifact")

    specify = sub.add_parser("specify")
    specify.add_argument("--repo", default=".")
    specify.add_argument("--title", required=True)

    next_plan = sub.add_parser("next-plan")
    next_plan.add_argument("--repo", default=".")

    seed = sub.add_parser("write-discovery-seed")
    seed.add_argument("--repo", default=".")
    seed.add_argument("--title", required=True)
    seed.add_argument("--body", required=True)
    seed.add_argument("--source", default="synthetic_human_intake")

    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    if args.command == "plan":
        print_json(plan_handoff(repo, args.title, args.source_issue, args.audit_artifact))
        return 0
    if args.command == "next-plan":
        print_json(plan_next(repo))
        return 0
    if args.command == "write-discovery-seed":
        print_json(write_discovery_seed(repo, title=args.title, body=args.body, source=args.source))
        return 0
    print_json(run_specify(repo, args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
