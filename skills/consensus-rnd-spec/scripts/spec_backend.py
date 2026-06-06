#!/usr/bin/env python3
"""Spec Kitty backend adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from backend_common import load_config, print_json
from github_sync import ensure_child_issues

PENDING_RESULT_FILE = "spec-kitty-pending-result.json"
KITTY_NEXT_ACTIONS = {"research", "specify", "plan", "tasks", "tasks-outline", "tasks-packages", "analyze"}
KITTY_AGENT_ACTIONS = {"implement", "review"}
REVIEW_COMPLETE_LANES = {"approved", "done"}
PLANNING_LANE_ID = "lane-planning"
COCKPIT_RUNTIME_CORS_REQUIRED_FILES = (
    "apps/cockpit-api/src/runtime-config.ts",
    "apps/cockpit-api/tests/cockpit-runtime-entry.test.ts",
)


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
    lanes = local_lane_items(mission_dir)
    summary = {lane: 0 for lane in ("planned", "claimed", "in_progress", "for_review", "in_review", "approved", "done", "blocked", "canceled")}
    for lane in lanes.values():
        summary[lane] = summary.get(lane, 0) + 1
    return summary


def local_lane_items(mission_dir: Path) -> dict[str, str]:
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
    return lanes


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
        items = local_lane_items(meta_path.parent)
        summary = lane_summary(items)
        score = local_actionable_score(summary)
        if score > 0:
            candidates.append({"mission": slug, "summary": summary, "items": items, "score": score, "source": "local-scan"})
    return sorted(candidates, key=lambda item: int(item["score"]), reverse=True)


def lane_summary(items: dict[str, str]) -> dict[str, int]:
    summary = {lane: 0 for lane in ("planned", "claimed", "in_progress", "for_review", "in_review", "approved", "done", "blocked", "canceled")}
    for lane in items.values():
        summary[lane] = summary.get(lane, 0) + 1
    return summary


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


def mission_pre_wp_candidate(state: dict[str, Any]) -> bool:
    payload = state.get("payload", state)
    if not payload.get("success"):
        return False
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return False
    packages = data.get("work_packages")
    summary = data.get("summary")
    if isinstance(packages, list) and packages:
        return False
    if isinstance(summary, dict):
        return all(int(summary.get(lane) or 0) == 0 for lane in summary)
    return True


def state_work_packages(state: dict[str, Any]) -> list[dict[str, Any]]:
    payload = state.get("payload", {})
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return []
    packages = data.get("work_packages")
    if not isinstance(packages, list):
        return []
    items: list[dict[str, Any]] = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        wp_id = package.get("wp_id") or package.get("id") or package.get("work_package_id")
        lane = package.get("lane") or package.get("status") or package.get("state")
        if isinstance(wp_id, str) and wp_id and isinstance(lane, str) and lane:
            dependencies = package.get("dependencies")
            if not isinstance(dependencies, list):
                dependencies = []
            items.append(
                {
                    "wp_id": wp_id,
                    "lane": lane,
                    "dependencies": [str(dep) for dep in dependencies if isinstance(dep, str) and dep],
                }
            )
    return items


def state_lane_items(state: dict[str, Any]) -> dict[str, str]:
    items: dict[str, str] = {}
    for package in state_work_packages(state):
        items[str(package["wp_id"])] = str(package["lane"])
    return items


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
        for slug in list_mission_slugs(repo, limit=scan_limit):
            state = mission_state(repo, slug)
            payload = state.get("payload", {})
            if mission_pre_wp_candidate(payload):
                return {"mission": slug, "state": state, "source": "scan-pre-wp", "score": 10}
        latest = list_mission_slugs(repo, limit=1)
        if latest:
            slug = latest[0]
            return {"mission": slug, "state": mission_state(repo, slug), "source": "scan-latest", "score": 0}
        return {"mission": None, "state": None, "source": "none", "score": -1}
    return best


def next_command(mission: str, agent: str, *, result: str | None = None) -> list[str]:
    command = ["spec-kitty", "next", "--agent", agent, "--mission", mission, "--json"]
    if result:
        command.extend(["--result", result])
    return command


def next_decision(repo: Path, mission: str, agent: str, *, result: str | None = None) -> dict[str, Any]:
    return run_json(next_command(mission, agent, result=result), repo)


def kitty_agent_action_command(action: str, mission: str, wp_id: str | None, agent: str) -> list[str] | None:
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


def action_command(decision: dict[str, Any], agent: str) -> list[str] | None:
    payload = decision.get("payload", {})
    action = payload.get("action")
    mission = payload.get("mission_slug")
    wp_id = payload.get("wp_id")
    if not action or not mission:
        return None
    return kitty_agent_action_command(str(action), str(mission), str(wp_id) if wp_id else None, agent)


def wp_prompt_path(repo: Path, mission: str, wp_id: str) -> Path | None:
    tasks = mission_dir(repo, mission) / "tasks"
    if not tasks.is_dir():
        return None
    candidates = sorted(tasks.glob(f"{wp_id}-*.md"))
    return candidates[0] if candidates else None


def frontmatter_block(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    if end == -1:
        return ""
    return text[4:end]


def frontmatter_list(block: str, key: str) -> list[str]:
    lines = block.splitlines()
    values: list[str] = []
    collecting = False
    prefix = f"{key}:"
    for line in lines:
        if line.startswith(prefix):
            collecting = True
            remainder = line[len(prefix) :].strip()
            if remainder.startswith("[") and remainder.endswith("]"):
                return [item.strip().strip("\"'") for item in remainder[1:-1].split(",") if item.strip()]
            continue
        if not collecting:
            continue
        if line and not line.startswith((" ", "-")):
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            values.append(stripped[2:].strip().strip("\"'"))
    return [value for value in values if value]


def wp_owned_files(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return frontmatter_list(frontmatter_block(text), "owned_files")


def path_is_owned(owned: list[str], required: str) -> bool:
    for item in owned:
        if item == required:
            return True
        if item.endswith("/**") and required.startswith(item[:-3].rstrip("/") + "/"):
            return True
    return False


def audit_wp_owned_files(repo: Path, mission: str, wp_id: str) -> dict[str, Any]:
    path = wp_prompt_path(repo, mission, wp_id)
    if path is None:
        return {"status": "ok", "checks": []}
    text = path.read_text(encoding="utf-8", errors="ignore")
    normalized = text.lower()
    owned = wp_owned_files(path)
    checks: list[dict[str, Any]] = []

    mentions_cockpit_cors_config = (
        "production" in normalized
        and "cors" in normalized
        and (
            "config validation" in normalized
            or "runtime config" in normalized
            or "cockpit_cors" in normalized
            or "production restricted mode" in normalized
        )
    )
    if mentions_cockpit_cors_config:
        missing = [required for required in COCKPIT_RUNTIME_CORS_REQUIRED_FILES if not path_is_owned(owned, required)]
        if missing:
            checks.append(
                {
                    "status": "blocked",
                    "rule": "cockpit-runtime-cors-config-ownership",
                    "reason": "WP prompt requires production CORS config/runtime validation but owned_files omit runtime config files",
                    "wp_path": str(path),
                    "missing_owned_files": missing,
                    "suggestion": "Update the Spec Kitty WP ownership artifacts before dispatching implementation, or split the config-validation work into a WP that owns these files.",
                }
            )

    blocking = [check for check in checks if check.get("status") == "blocked"]
    if blocking:
        return {"status": "blocked", "reason": "WP owned_files preflight failed", "mission": mission, "wp_id": wp_id, "checks": checks}
    return {"status": "ok", "mission": mission, "wp_id": wp_id, "checks": checks}


def with_wp_preflight(repo: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("execution_kind") != "kitty-agent-action" or plan.get("action") != "implement":
        return plan
    chosen = plan.get("chosen")
    mission = str(plan.get("decision", {}).get("payload", {}).get("mission_slug") or plan.get("mission") or "")
    if not mission and isinstance(chosen, dict):
        mission = str(chosen.get("mission") or "")
    wp_id = str(plan.get("wp_id") or "")
    if not mission or not wp_id:
        return plan
    audit = audit_wp_owned_files(repo, mission, wp_id)
    if audit.get("status") == "blocked":
        blocked = dict(plan)
        blocked["status"] = "blocked"
        blocked["reason"] = audit.get("reason")
        blocked["action_command"] = None
        blocked["preflight"] = audit
        return blocked
    plan["preflight"] = audit
    return plan


def pending_result_path(repo: Path) -> Path:
    return repo / ".consensus-rnd-spec" / "state" / PENDING_RESULT_FILE


def load_pending_result(repo: Path) -> dict[str, Any] | None:
    path = pending_result_path(repo)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("result") not in {"success", "failed", "blocked"}:
        return None
    if not isinstance(payload.get("mission_slug"), str) or not payload.get("mission_slug"):
        return None
    return payload


def write_pending_result(repo: Path, payload: dict[str, Any]) -> Path:
    path = pending_result_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def clear_pending_result(repo: Path) -> None:
    pending_result_path(repo).unlink(missing_ok=True)


def dependencies_satisfied(package: dict[str, Any], lanes: dict[str, str]) -> bool:
    for dependency in package.get("dependencies", []):
        if lanes.get(str(dependency)) != "done":
            return False
    return True


def first_actionable_wp(packages: list[dict[str, Any]], lanes: dict[str, str]) -> tuple[str, str] | None:
    for package in sorted(packages, key=lambda item: str(item["wp_id"])):
        if package["lane"] == "for_review":
            return "review", str(package["wp_id"])
    for package in sorted(packages, key=lambda item: str(item["wp_id"])):
        if package["lane"] == "in_progress":
            return "implement", str(package["wp_id"])
    for package in sorted(packages, key=lambda item: str(item["wp_id"])):
        if package["lane"] == "planned" and dependencies_satisfied(package, lanes):
            return "implement", str(package["wp_id"])
    return None


def wp_action_from_chosen(chosen: dict[str, Any], mission: str, agent: str) -> dict[str, Any] | None:
    state = chosen.get("state") if isinstance(chosen.get("state"), dict) else {}
    packages = state_work_packages(state)
    if packages:
        lanes = {str(package["wp_id"]): str(package["lane"]) for package in packages}
    else:
        lanes = {}
        local = chosen.get("local")
        if isinstance(local, dict):
            local_items = local.get("items")
            if isinstance(local_items, dict):
                lanes = {str(key): str(value) for key, value in local_items.items()}
                packages = [{"wp_id": wp_id, "lane": lane, "dependencies": []} for wp_id, lane in lanes.items()]
    target = first_actionable_wp(packages, lanes)
    if target is None:
        return None
    action, wp_id = target
    command = kitty_agent_action_command(action, mission, wp_id, agent)
    if command is None:
        return None
    return {
        "backend": "spec-kitty",
        "status": "ready",
        "execution_kind": "kitty-agent-action",
        "chosen": chosen,
        "action": action,
        "wp_id": wp_id,
        "action_command": command,
    }


def next_step_plan(chosen: dict[str, Any], decision: dict[str, Any], agent: str) -> dict[str, Any] | None:
    payload = decision.get("payload", {})
    if not isinstance(payload, dict):
        return None
    mission = payload.get("mission_slug") or chosen.get("mission")
    if not isinstance(mission, str) or not mission:
        return None
    action = payload.get("action")
    prompt_file = payload.get("prompt_file")
    if isinstance(action, str) and action in KITTY_AGENT_ACTIONS:
        command = action_command(decision, agent)
        if command is not None:
            return {
                "backend": "spec-kitty",
                "status": "ready",
                "execution_kind": "kitty-agent-action",
                "chosen": chosen,
                "decision": decision,
                "action": action,
                "wp_id": payload.get("wp_id"),
                "action_command": command,
            }
    if isinstance(action, str) and action in KITTY_NEXT_ACTIONS and isinstance(prompt_file, str) and prompt_file:
        return {
            "backend": "spec-kitty",
            "status": "ready",
            "execution_kind": "kitty-prompt-file",
            "chosen": chosen,
            "decision": decision,
            "action": action,
            "prompt_file": prompt_file,
        }
    preview = payload.get("preview_step")
    if isinstance(preview, str) and preview:
        return {
            "backend": "spec-kitty",
            "status": "ready",
            "execution_kind": "kitty-next-step",
            "chosen": chosen,
            "decision": decision,
            "action": preview,
            "advance_command": next_command(mission, agent, result="success"),
            "advance_result": "success",
            "reason": "start_preview_step",
        }
    return None


def pending_step_plan(repo: Path, pending: dict[str, Any], agent: str) -> dict[str, Any]:
    mission = str(pending["mission_slug"])
    result = str(pending["result"])
    return {
        "backend": "spec-kitty",
        "status": "ready",
        "execution_kind": "kitty-next-step",
        "chosen": {"mission": mission, "source": "pending-result"},
        "pending_result": pending,
        "action": pending.get("completed_action"),
        "advance_command": next_command(mission, agent, result=result),
        "advance_result": result,
        "pending_result_path": str(pending_result_path(repo)),
        "reason": "advance_after_worker_result",
    }


def plan_next(repo: Path) -> dict[str, Any]:
    config = load_config(repo)
    pending = load_pending_result(config.repo_root)
    if pending is not None and (not config.spec_kitty_mission or pending["mission_slug"] == config.spec_kitty_mission):
        return pending_step_plan(config.repo_root, pending, config.spec_kitty_agent)
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
    step_plan = next_step_plan(chosen, decision, config.spec_kitty_agent)
    if step_plan is not None:
        return with_wp_preflight(config.repo_root, step_plan)
    wp_plan = wp_action_from_chosen(chosen, str(mission), config.spec_kitty_agent)
    if wp_plan is not None:
        wp_plan["decision"] = decision
        wp_plan["github_sync"] = ensure_child_issues(config.repo_root, str(mission), execute=False)
        return with_wp_preflight(config.repo_root, wp_plan)
    return {
        "backend": "spec-kitty",
        "status": "waiting",
        "reason": decision.get("payload", {}).get("reason") or "no Spec Kitty step is currently eligible",
        "chosen": chosen,
        "decision": decision,
        "action_command": None,
    }


def mission_dir(repo: Path, mission: str) -> Path:
    return repo / "kitty-specs" / mission


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return payload


def resolve_target_branch(mission_path: Path, lanes_payload: dict[str, Any], override: str) -> str:
    if override:
        return override
    target = lanes_payload.get("target_branch")
    if isinstance(target, str) and target:
        return target
    meta_path = mission_path / "meta.json"
    if meta_path.exists():
        meta = read_json_file(meta_path)
        meta_target = meta.get("target_branch")
        if isinstance(meta_target, str) and meta_target:
            return meta_target
    return "main"


def spec_kitty_lane_branch(mission: str, lane_id: str, target_branch: str) -> str | None:
    ensure_spec_kitty_import_path()
    try:
        from specify_cli.lanes.branch_naming import lane_branch_name
    except Exception:
        return None
    return lane_branch_name(mission, lane_id, planning_base_branch=target_branch)


def spec_kitty_python_path() -> Path | None:
    executable = shutil.which("spec-kitty")
    if not executable:
        return None
    try:
        first_line = Path(executable).read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None
    return Path(first_line[2:].strip().split(" ", 1)[0])


def ensure_spec_kitty_import_path() -> bool:
    try:
        import specify_cli  # noqa: F401

        return True
    except Exception:
        sys.modules.pop("specify_cli", None)
        pass

    python_path = spec_kitty_python_path()
    if python_path is None:
        return False
    root = python_path.parent.parent
    candidates = sorted((root / "lib").glob("python*/site-packages"))
    for site_packages in candidates:
        if not site_packages.is_dir():
            continue
        sys.path.insert(0, str(site_packages))
        try:
            import specify_cli  # noqa: F401

            return True
        except Exception:
            sys.modules.pop("specify_cli", None)
            try:
                sys.path.remove(str(site_packages))
            except ValueError:
                pass
    return False


def merge_lanes_to_mission(
    repo: Path,
    mission: str,
    target: str,
    lane_ids: list[str],
) -> dict[str, Any]:
    if ensure_spec_kitty_import_path():
        try:
            from specify_cli.lanes.merge import merge_lane_to_mission
            from specify_cli.lanes.persistence import require_lanes_json
        except Exception as exc:
            return {"status": "blocked", "reason": f"Spec Kitty lane merge API unavailable: {exc}"}

        path = mission_dir(repo, mission)
        try:
            lanes_manifest = require_lanes_json(path)
        except Exception as exc:
            return {"status": "blocked", "reason": f"could not load Spec Kitty lanes manifest: {exc}"}
        lanes_manifest.target_branch = target

        merged_lanes: list[dict[str, Any]] = []
        for current_lane_id in lane_ids:
            lane_result = merge_lane_to_mission(repo, mission, current_lane_id, lanes_manifest)
            if not lane_result.success:
                return {
                    "status": "failed",
                    "reason": "lane-to-mission merge failed",
                    "lane_id": current_lane_id,
                    "errors": lane_result.errors,
                }
            merged_lanes.append({"lane_id": current_lane_id, "merged_into": lane_result.merged_into})
        return {"status": "merged", "merged_lanes": merged_lanes}

    python_path = spec_kitty_python_path()
    if python_path is None:
        return {"status": "blocked", "reason": "Spec Kitty Python interpreter could not be resolved from the spec-kitty CLI"}

    helper = r"""
import json
import sys
from pathlib import Path

from specify_cli.lanes.merge import merge_lane_to_mission
from specify_cli.lanes.persistence import require_lanes_json

payload = json.loads(sys.stdin.read())
repo = Path(payload["repo"])
mission = payload["mission"]
target = payload["target"]
lane_ids = payload["lane_ids"]
mission_dir = repo / "kitty-specs" / mission
lanes_manifest = require_lanes_json(mission_dir)
lanes_manifest.target_branch = target
merged_lanes = []
for lane_id in lane_ids:
    lane_result = merge_lane_to_mission(repo, mission, lane_id, lanes_manifest)
    if not lane_result.success:
        print(json.dumps({"status": "failed", "reason": "lane-to-mission merge failed", "lane_id": lane_id, "errors": lane_result.errors}))
        sys.exit(1)
    merged_lanes.append({"lane_id": lane_id, "merged_into": lane_result.merged_into})
print(json.dumps({"status": "merged", "merged_lanes": merged_lanes}))
"""
    result = subprocess.run(
        [str(python_path), "-c", helper],
        input=json.dumps({"repo": str(repo), "mission": mission, "target": target, "lane_ids": lane_ids}),
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout}
    if result.returncode != 0:
        return {
            "status": "failed",
            "reason": payload.get("reason") or "Spec Kitty helper lane merge failed",
            "helper": str(python_path),
            "payload": payload,
            "stderr": result.stderr,
        }
    if not isinstance(payload, dict):
        return {"status": "failed", "reason": "Spec Kitty helper returned non-object JSON", "stdout": result.stdout}
    return payload


def git_status_lines(repo: Path) -> tuple[int, list[str], str]:
    result = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=False)
    return result.returncode, [line for line in result.stdout.splitlines() if line.strip()], result.stderr.strip()


def unexpected_status_lines(lines: list[str]) -> list[str]:
    unexpected: list[str] = []
    for line in lines:
        if line.startswith("?? .kittify/derived/"):
            continue
        unexpected.append(line)
    return unexpected


def git_rev_parse(repo: Path, ref: str) -> str | None:
    result = subprocess.run(["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def approved_code_lane_plan(
    repo: Path,
    mission: str,
    *,
    lane_id: str = "",
    target_branch: str = "",
) -> dict[str, Any]:
    path = mission_dir(repo, mission)
    lanes_path = path / "lanes.json"
    if not lanes_path.exists():
        return {"status": "blocked", "reason": f"lanes.json not found for mission {mission}", "mission": mission}

    try:
        lanes_payload = read_json_file(lanes_path)
    except ValueError as exc:
        return {"status": "blocked", "reason": str(exc), "mission": mission}

    resolved_target = resolve_target_branch(path, lanes_payload, target_branch)
    current_lanes = local_lane_items(path)
    lanes = lanes_payload.get("lanes")
    if not isinstance(lanes, list):
        return {"status": "blocked", "reason": "lanes.json has no lanes list", "mission": mission}

    candidates: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    seen_requested_lane = False

    for raw_lane in lanes:
        if not isinstance(raw_lane, dict):
            continue
        current_lane_id = raw_lane.get("lane_id")
        if not isinstance(current_lane_id, str) or not current_lane_id:
            continue
        if lane_id and current_lane_id != lane_id:
            continue
        seen_requested_lane = True
        wp_ids = raw_lane.get("wp_ids")
        if not isinstance(wp_ids, list):
            blockers.append({"lane_id": current_lane_id, "reason": "lane has no wp_ids list"})
            continue
        clean_wp_ids = [str(wp_id) for wp_id in wp_ids if isinstance(wp_id, str) and wp_id]
        if current_lane_id == PLANNING_LANE_ID:
            blockers.append({"lane_id": current_lane_id, "wp_ids": clean_wp_ids, "reason": "planning lane is not eligible for pre-integration"})
            continue
        missing = [wp_id for wp_id in clean_wp_ids if current_lanes.get(wp_id) not in REVIEW_COMPLETE_LANES]
        if missing:
            blockers.append(
                {
                    "lane_id": current_lane_id,
                    "wp_ids": clean_wp_ids,
                    "reason": "lane contains WPs that are not approved/done",
                    "missing": missing,
                    "lanes": {wp_id: current_lanes.get(wp_id, "unknown") for wp_id in missing},
                }
            )
            continue
        candidates.append(
            {
                "lane_id": current_lane_id,
                "wp_ids": clean_wp_ids,
                "branch": spec_kitty_lane_branch(mission, current_lane_id, resolved_target),
            }
        )

    if lane_id and not seen_requested_lane:
        return {"status": "blocked", "reason": f"lane {lane_id} not found in mission {mission}", "mission": mission}
    if not candidates:
        return {
            "status": "blocked",
            "reason": "no approved non-planning code lanes are eligible for pre-integration",
            "mission": mission,
            "target_branch": resolved_target,
            "blockers": blockers,
        }
    return {
        "status": "planned",
        "mission": mission,
        "target_branch": resolved_target,
        "mission_branch": lanes_payload.get("mission_branch"),
        "lanes": candidates,
        "blockers": blockers,
        "execute_hint": [
            "python3",
            "<skill-root>/scripts/spec_backend.py",
            "integrate-approved-lanes",
            "--repo",
            str(repo),
            "--mission",
            mission,
            "--target",
            resolved_target,
            "--execute",
        ],
    }


def integrate_approved_lanes(
    repo: Path,
    mission: str,
    *,
    lane_id: str = "",
    target_branch: str = "",
    execute: bool = False,
) -> dict[str, Any]:
    config = load_config(repo)
    repo = config.repo_root
    plan = approved_code_lane_plan(repo, mission, lane_id=lane_id, target_branch=target_branch)
    if plan.get("status") != "planned":
        return plan
    if not execute:
        return plan

    status_rc, status_lines, status_error = git_status_lines(repo)
    if status_rc != 0:
        return {"status": "blocked", "reason": f"git status failed: {status_error}", "mission": mission}
    dirty = unexpected_status_lines(status_lines)
    if dirty:
        return {
            "status": "blocked",
            "reason": "repo has uncommitted changes outside allowed generated artifacts",
            "mission": mission,
            "dirty": dirty,
        }

    target = str(plan["target_branch"])
    merge_result_payload = merge_lanes_to_mission(repo, mission, target, [str(lane["lane_id"]) for lane in plan["lanes"]])
    if merge_result_payload.get("status") != "merged":
        merge_result_payload.setdefault("mission", mission)
        return merge_result_payload
    merged_by_lane = {str(item["lane_id"]): item for item in merge_result_payload.get("merged_lanes", []) if isinstance(item, dict)}
    merged_lanes = [
        {
            "lane_id": str(lane["lane_id"]),
            "wp_ids": lane["wp_ids"],
            "merged_into": merged_by_lane.get(str(lane["lane_id"]), {}).get("merged_into"),
        }
        for lane in plan["lanes"]
    ]

    switch_result = subprocess.run(["git", "switch", target], cwd=repo, capture_output=True, text=True, check=False)
    if switch_result.returncode != 0:
        return {
            "status": "failed",
            "reason": f"could not switch to target branch {target}",
            "stdout": switch_result.stdout,
            "stderr": switch_result.stderr,
            "mission": mission,
        }

    mission_branch = str(plan["mission_branch"])
    merge_result = subprocess.run(
        [
            "git",
            "merge",
            mission_branch,
            "--no-ff",
            "--no-edit",
            "-m",
            f"Merge {mission_branch} into {target} for acceptance gates",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_result.returncode != 0:
        return {
            "status": "failed",
            "reason": "mission-to-target pre-integration merge failed",
            "mission": mission,
            "target_branch": target,
            "mission_branch": mission_branch,
            "stdout": merge_result.stdout,
            "stderr": merge_result.stderr,
        }

    return {
        "status": "integrated",
        "mission": mission,
        "target_branch": target,
        "mission_branch": mission_branch,
        "merged_lanes": merged_lanes,
        "target_head": git_rev_parse(repo, "HEAD"),
        "note": "No WP status/frontmatter was modified; full Spec Kitty mission merge remains responsible for done transitions and cleanup.",
    }


def write_discovery_seed(
    repo: Path,
    *,
    title: str,
    body: str,
    source: str = "synthetic_human_intake",
    source_kind: str = "",
    source_issue: str = "",
    source_pr: str = "",
    source_url: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "source_kind": source_kind or source,
        "source_issue": source_issue,
        "source_pr": source_pr,
        "source_url": source_url,
        "producer": "consensus-rnd-spec",
        "synthetic_human_intake": source == "synthetic_human_intake",
        "mission_type": config.spec_kitty_mission_type,
        "evidence_hash": evidence_hash(title + "\n" + body),
        "created_at": stamp,
    }
    if metadata:
        payload["metadata"] = metadata
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
    seed.add_argument("--source-kind", default="")
    seed.add_argument("--source-issue", default="")
    seed.add_argument("--source-pr", default="")
    seed.add_argument("--source-url", default="")

    integrate = sub.add_parser("integrate-approved-lanes")
    integrate.add_argument("--repo", default=".")
    integrate.add_argument("--mission", required=True)
    integrate.add_argument("--target", default="")
    integrate.add_argument("--lane-id", default="")
    integrate.add_argument("--execute", action="store_true")

    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    if args.command == "plan":
        print_json(plan_handoff(repo, args.title, args.source_issue, args.audit_artifact))
        return 0
    if args.command == "next-plan":
        print_json(plan_next(repo))
        return 0
    if args.command == "write-discovery-seed":
        print_json(
            write_discovery_seed(
                repo,
                title=args.title,
                body=args.body,
                source=args.source,
                source_kind=args.source_kind,
                source_issue=args.source_issue,
                source_pr=args.source_pr,
                source_url=args.source_url,
            )
        )
        return 0
    if args.command == "integrate-approved-lanes":
        result = integrate_approved_lanes(
            repo,
            args.mission,
            lane_id=args.lane_id,
            target_branch=args.target,
            execute=args.execute,
        )
        print_json(result)
        return 0 if result.get("status") in {"planned", "integrated"} else 1
    print_json(run_specify(repo, args.title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
