#!/usr/bin/env python3
"""GitHub projection sync for Spec Kitty missions.

Spec Kitty remains the mission/WP/worktree authority. This module only projects
mission and WP state into GitHub issues, comments, labels, and one mission PR.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend_common import HostConfig, load_config, print_json, utc_now


AUTO_LOOP_SENTINEL = "⟦AI:AUTO-LOOP⟧"
CONTROLLER_STATUS_MARKER = "🤖 controller status banner"
CONTROLLER_COMMENTARY_MARKER = "🤖 controller commentary"

MANAGED = "crnd:lifecycle:managed"
HUMAN_AUTO = "crnd:human:auto"
HUMAN_MAINTAINER_DECISION = "crnd:human:maintainer-decision"
PHASE_DESIGN_SOLVING = "crnd:phase:design-solving"
PHASE_CONSENSUS_REACHED = "crnd:phase:consensus-reached"
PHASE_IMPLEMENTING = "crnd:phase:implementing"
PHASE_PR_OPEN = "crnd:phase:pr-open"
PHASE_REVIEWING = "crnd:phase:reviewing"
PHASE_FIXING = "crnd:phase:fixing"
PHASE_CI_RUNNING = "crnd:phase:ci-running"
PHASE_BLOCKED = "crnd:phase:blocked"
PHASE_MERGED = "crnd:phase:merged"
PHASE_CLOSED = "crnd:phase:closed"

PHASE_LABELS = (
    PHASE_DESIGN_SOLVING,
    PHASE_CONSENSUS_REACHED,
    PHASE_IMPLEMENTING,
    PHASE_PR_OPEN,
    PHASE_REVIEWING,
    PHASE_FIXING,
    PHASE_CI_RUNNING,
    PHASE_BLOCKED,
    PHASE_MERGED,
    PHASE_CLOSED,
)
HUMAN_LABELS = (HUMAN_AUTO, HUMAN_MAINTAINER_DECISION)

LABELS_SOURCE_ANCHOR = "crnd:phase:consensus-reached"
SENTINEL_SOURCE_ANCHOR = "⟦AI:AUTO-LOOP⟧"
STATUS_BANNER_SOURCE_ANCHOR = "🤖 controller status banner"
PR_CLOSES_RE = re.compile(r"(?im)\bCloses\s+#(\d+)\b")
COMMENTARY_KINDS = (
    "review",
    "solver",
    "meta-judge",
    "scope",
    "evidence",
    "decision",
    "handoff",
)


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
        }


@dataclass(frozen=True)
class LabelSpec:
    name: str
    description: str
    color: str


LABEL_SPECS = (
    LabelSpec(MANAGED, "Item is managed by consensus-rnd-spec.", "ededed"),
    LabelSpec("crnd:lifecycle:stuck", "Item is stalled and waiting for loop recovery.", "b60205"),
    LabelSpec("crnd:lifecycle:no-framing", "Item has no actionable framing.", "d4c5f9"),
    LabelSpec("crnd:triage:pending", "External issue is pending intake triage.", "fbca04"),
    LabelSpec("crnd:triage:resume-requested", "Maintainer requested resumed implementation.", "1d76db"),
    LabelSpec("crnd:milestone:current", "Milestone-priority item.", "f9d0c4"),
    LabelSpec("crnd:milestone:release-target", "Release countdown target issue/PR.", "f9d0c4"),
    LabelSpec(HUMAN_AUTO, "Controller may continue without maintainer intervention.", "bfd4f2"),
    LabelSpec(HUMAN_MAINTAINER_DECISION, "Maintainer decision is required.", "b60205"),
    LabelSpec(PHASE_DESIGN_SOLVING, "Consensus design solving is active.", "0e8a16"),
    LabelSpec(PHASE_CONSENSUS_REACHED, "Consensus is reached and implementation is ready.", "1d76db"),
    LabelSpec(PHASE_IMPLEMENTING, "Implementation worker is active.", "c5def5"),
    LabelSpec(PHASE_PR_OPEN, "Pull request is open and awaiting review or CI routing.", "5319e7"),
    LabelSpec(PHASE_REVIEWING, "Review workers are active.", "5319e7"),
    LabelSpec(PHASE_FIXING, "Fix worker is active.", "d93f0b"),
    LabelSpec(PHASE_CI_RUNNING, "CI watch is active.", "fbca04"),
    LabelSpec(PHASE_BLOCKED, "Work is blocked or explicitly waiting.", "b60205"),
    LabelSpec(PHASE_MERGED, "Work has landed.", "0e8a16"),
    LabelSpec(PHASE_CLOSED, "Closed terminal protocol state without merged evidence.", "ededed"),
)


def mission_dir(repo: Path, mission: str) -> Path:
    return repo / "kitty-specs" / mission


def bindings_path(mission_path: Path) -> Path:
    return mission_path / "consensus-rnd" / "github-bindings.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_bindings(mission_path: Path) -> dict[str, Any]:
    payload = load_json(bindings_path(mission_path))
    payload.setdefault("schema", "consensus-rnd-spec.github-bindings.v1")
    payload.setdefault("parent_issue", {})
    payload.setdefault("child_issues", {})
    payload.setdefault("mission_pr", {})
    payload.setdefault("events", [])
    return payload


def write_bindings(mission_path: Path, payload: dict[str, Any]) -> Path:
    normalize_binding_paths(mission_path, payload)
    payload["updated_at"] = utc_now()
    path = bindings_path(mission_path)
    write_json(path, payload)
    update_meta_github_summary(mission_path, payload)
    return path


def repo_root_for_mission(mission_path: Path) -> Path:
    return mission_path.parent.parent


def mission_relative_path(mission_path: Path, path: Path) -> str:
    try:
        return path.relative_to(repo_root_for_mission(mission_path)).as_posix()
    except ValueError:
        return str(path)


def normalize_binding_paths(mission_path: Path, payload: dict[str, Any]) -> None:
    child_issues = payload.get("child_issues")
    if not isinstance(child_issues, dict):
        return
    repo_root = repo_root_for_mission(mission_path)
    for child in child_issues.values():
        if not isinstance(child, dict):
            continue
        wp_path = child.get("wp_path")
        if not isinstance(wp_path, str) or not wp_path:
            continue
        path = Path(wp_path)
        if not path.is_absolute():
            child["wp_path"] = path.as_posix()
            continue
        try:
            child["wp_path"] = path.relative_to(repo_root).as_posix()
        except ValueError:
            child["wp_path"] = str(path)


def update_meta_github_summary(mission_path: Path, bindings: dict[str, Any]) -> None:
    meta_path = mission_path / "meta.json"
    if not meta_path.exists():
        return
    meta = load_json(meta_path)
    if not isinstance(meta, dict):
        return
    existing = meta.get("consensus_rnd_spec")
    if not isinstance(existing, dict):
        existing = {}
    github = {
        "bindings": mission_relative_path(mission_path, bindings_path(mission_path)),
        "parent_issue": bindings.get("parent_issue", {}),
        "mission_pr": bindings.get("mission_pr", {}),
        "child_issue_count": len(bindings.get("child_issues", {}) if isinstance(bindings.get("child_issues"), dict) else {}),
        "updated_at": bindings.get("updated_at"),
    }
    existing["github"] = github
    meta["consensus_rnd_spec"] = existing
    write_json(meta_path, meta)


def append_event(bindings: dict[str, Any], event: dict[str, Any]) -> None:
    events = bindings.get("events")
    if not isinstance(events, list):
        events = []
    payload = dict(event)
    payload.setdefault("timestamp", utc_now())
    events.append(payload)
    bindings["events"] = events[-200:]


def append_binding_event(mission_path: Path, event: dict[str, Any]) -> Path:
    """Append an event after reloading bindings to preserve concurrent writers."""
    bindings = load_bindings(mission_path)
    append_event(bindings, event)
    return write_bindings(mission_path, bindings)


def record_wp_status_sync(mission_path: Path, wp_id: str, issue: str, phase: str) -> Path:
    """Record WP status after reloading bindings to preserve concurrent commentary events."""
    bindings = load_bindings(mission_path)
    child_issues = bindings.get("child_issues")
    if not isinstance(child_issues, dict):
        child_issues = {}
    child = child_issues.get(wp_id)
    if not isinstance(child, dict):
        child = {"number": issue}
    child["number"] = issue
    child["phase"] = phase
    child["last_status_at"] = utc_now()
    child_issues[wp_id] = child
    bindings["child_issues"] = child_issues
    append_event(bindings, {"kind": "wp-status-synced", "wp_id": wp_id, "issue": issue, "phase": phase})
    return write_bindings(mission_path, bindings)


def mark_child_issue_bindings_merged(bindings: dict[str, Any]) -> None:
    child_issues = bindings.get("child_issues")
    if not isinstance(child_issues, dict):
        return
    now = utc_now()
    for child in child_issues.values():
        if not isinstance(child, dict) or not child.get("number"):
            continue
        child["phase"] = PHASE_MERGED
        child["last_status_at"] = now


WP_PHASE_LANE_ALLOWLIST: dict[str, set[str]] = {
    PHASE_DESIGN_SOLVING: {"planned"},
    PHASE_CONSENSUS_REACHED: {"planned", "approved"},
    PHASE_IMPLEMENTING: {"claimed", "in_progress"},
    PHASE_REVIEWING: {"for_review", "in_review"},
    PHASE_FIXING: {"planned", "claimed", "in_progress"},
    PHASE_MERGED: {"approved", "done"},
    PHASE_CLOSED: {"approved", "done", "blocked", "canceled"},
}


def wp_lane_from_status(mission_path: Path, wp_id: str) -> str:
    status = load_json(mission_path / "status.json")
    work_packages = status.get("work_packages")
    if isinstance(work_packages, dict):
        wp = work_packages.get(wp_id)
        if isinstance(wp, dict):
            lane = wp.get("lane")
            if isinstance(lane, str) and lane.strip():
                return lane.strip()
    return ""


def validate_wp_phase_against_kitty_lane(mission_path: Path, wp_id: str, phase: str) -> dict[str, Any]:
    allowed = WP_PHASE_LANE_ALLOWLIST.get(phase)
    if not allowed:
        return {"status": "ready", "reason": "phase has no WP lane guard"}
    lane = wp_lane_from_status(mission_path, wp_id)
    if not lane:
        return {"status": "blocked", "reason": "missing Spec Kitty WP lane", "wp_id": wp_id, "phase": phase}
    if lane not in allowed:
        return {
            "status": "blocked",
            "reason": "GitHub WP phase would lead Spec Kitty lane",
            "wp_id": wp_id,
            "phase": phase,
            "kitty_lane": lane,
            "allowed_lanes": sorted(allowed),
        }
    return {"status": "ready", "wp_id": wp_id, "phase": phase, "kitty_lane": lane, "allowed_lanes": sorted(allowed)}


def validate_mission_done_lanes(mission_path: Path, bindings: dict[str, Any]) -> dict[str, Any]:
    """Require Spec Kitty done transitions before closing issue projections."""
    status_path = mission_path / "status.json"
    if not status_path.exists():
        return {"status": "blocked", "reason": "missing Spec Kitty status.json; refusing to close issues"}
    status = load_json(status_path)
    work_packages = status.get("work_packages")
    if not isinstance(work_packages, dict) or not work_packages:
        return {"status": "blocked", "reason": "missing Spec Kitty WP status; refusing to close issues"}

    child_issues = bindings.get("child_issues") if isinstance(bindings.get("child_issues"), dict) else {}
    child_wp_ids = [
        str(wp_id)
        for wp_id, child in sorted(child_issues.items())
        if isinstance(child, dict) and child.get("number")
    ]
    wp_ids = child_wp_ids or [str(wp_id) for wp_id in sorted(work_packages)]

    lanes: list[dict[str, str]] = []
    not_done: list[dict[str, str]] = []
    for wp_id in wp_ids:
        wp = work_packages.get(wp_id)
        lane = ""
        if isinstance(wp, dict):
            lane_value = wp.get("lane")
            if isinstance(lane_value, str):
                lane = lane_value.strip()
        entry = {"wp_id": wp_id, "kitty_lane": lane}
        lanes.append(entry)
        if lane != "done":
            not_done.append(entry)

    if not_done:
        return {
            "status": "blocked",
            "reason": "Spec Kitty WPs are not done; refusing to close issues",
            "required_lane": "done",
            "not_done": not_done,
            "lanes": lanes,
        }
    return {"status": "ready", "required_lane": "done", "lanes": lanes}


def ensure_sentinel(body: str) -> str:
    text = body.rstrip()
    if text.splitlines()[-1:] == [AUTO_LOOP_SENTINEL]:
        return text + "\n"
    return text + "\n\n" + AUTO_LOOP_SENTINEL + "\n"


def is_preformatted_commentary(body: str) -> bool:
    lines = [line.strip() for line in body.strip().splitlines()]
    return CONTROLLER_COMMENTARY_MARKER in lines and lines[-1:] == [AUTO_LOOP_SENTINEL]


def build_status_banner(
    *,
    phase: str,
    mission: str,
    wp_id: str = "",
    issue: str = "",
    pr: str = "",
    detail: str = "",
    next_step: str = "",
    needs_human: bool = False,
) -> str:
    target = f"{mission} {wp_id}".strip()
    intervention = "✅ 需要人介入" if needs_human else "不需要人介入"
    body = "\n".join(
        [
            f"## 📊 当前状态 — {phase}({intervention})",
            "",
            "| 维度 | 值 |",
            "|---|---|",
            f"| 阶段 | **{phase}** |",
            f"| Mission | `{target}` |",
            f"| 关联 issue | {('#' + issue) if issue else 'n/a'} |",
            f"| 关联 PR | {('#' + pr) if pr else 'n/a'} |",
            f"| 详情 | {detail or 'n/a'} |",
            f"| **是否需要人介入** | **{'✅ 是' if needs_human else '❌ 否'}** |",
            "",
            f"**下一步自动会做**: {next_step or '继续由 Spec Kitty 推进 mission/WP 状态机。'}",
            "",
            "**何时需要人介入**:",
            "- GitHub 写入、Spec Kitty 状态推进、CI 或 merge preflight fail-closed。",
            "- maintainer 在 issue/PR 中明确要求调整方向或停止。",
            "",
            CONTROLLER_STATUS_MARKER,
        ]
    )
    return ensure_sentinel(body)


def build_commentary_body(
    *,
    kind: str,
    title: str,
    mission: str,
    summary: str,
    details: str = "",
    wp_id: str = "",
    issue: str = "",
    pr: str = "",
    score: str = "",
    verdict: str = "",
    next_step: str = "",
) -> str:
    if details.strip() and is_preformatted_commentary(details):
        return ensure_sentinel(details.strip())

    target = f"{mission} {wp_id}".strip()
    normalized_kind = normalize_commentary_kind(kind)
    lines = [
        f"## 🤖 {normalized_kind}: {title}",
        "",
        "### TL;DR",
        f"- 这是什么: {title}",
        f"- 现在到哪一步 / 结论是什么: {summary or 'n/a'}",
        f"- 需要 maintainer 做什么 OR controller 下一步: {next_step or 'controller 继续按 Spec Kitty / consensus-rnd-spec 流程推进。'}",
        "",
        "### 结构化记录",
        "",
        "| 维度 | 值 |",
        "|---|---|",
        f"| 类型 | `{normalized_kind}` |",
        f"| Mission | `{target}` |",
        f"| 关联 issue | {('#' + issue) if issue else 'n/a'} |",
        f"| 关联 PR | {('#' + pr) if pr else 'n/a'} |",
        f"| 评分 | {score or 'n/a'} |",
        f"| 结论 | {verdict or 'n/a'} |",
    ]
    if details.strip():
        lines.extend(["", "### 详细说明", "", details.strip()])
    lines.extend(["", CONTROLLER_COMMENTARY_MARKER])
    return ensure_sentinel("\n".join(lines))


def normalize_commentary_kind(kind: str) -> str:
    normalized = kind.strip().lower()
    if normalized not in COMMENTARY_KINDS:
        raise ValueError(f"unsupported commentary kind: {kind}")
    return normalized


def build_parent_issue_body(mission: str, mission_path: Path, title: str, source: dict[str, Any]) -> str:
    body = "\n".join(
        [
            "# Consensus R&D mission tracking",
            "",
            f"- Mission: `{mission}`",
            f"- Mission path: `{mission_path}`",
            f"- Source kind: {source.get('source_kind') or source.get('source') or 'consensus-rnd-spec'}",
            f"- Source issue: {source.get('source_issue') or 'n/a'}",
            f"- Source PR: {source.get('source_pr') or 'n/a'}",
            f"- Source URL: {source.get('source_url') or 'n/a'}",
            "",
            "This issue tracks the Spec Kitty mission. Child issues are bound one-to-one to WPs after task finalization.",
            "",
            f"Initial title: {title}",
        ]
    )
    return ensure_sentinel(body)


def build_child_issue_body(mission: str, wp: dict[str, str], parent_issue: str, mission_pr: str = "") -> str:
    body = "\n".join(
        [
            "# Consensus R&D WP tracking",
            "",
            f"- Mission: `{mission}`",
            f"- WP: `{wp['wp_id']}`",
            f"- WP artifact: `{wp['path']}`",
            f"- Parent issue: #{parent_issue}",
            f"- Mission PR: {('#' + mission_pr) if mission_pr else 'n/a'}",
            "",
            "This child issue mirrors one Spec Kitty WP. Spec Kitty remains the lane/worktree/merge authority.",
        ]
    )
    return ensure_sentinel(body)


def build_mission_pr_body(mission: str, parent_issue: str, child_issues: dict[str, Any]) -> str:
    lines = [
        "# Consensus R&D mission PR",
        "",
        f"- Mission: `{mission}`",
        f"- Parent issue: #{parent_issue}",
        "",
        "## WP issues",
    ]
    for wp_id in sorted(child_issues):
        issue = child_issues.get(wp_id, {}).get("number")
        lines.append(f"- `{wp_id}`: #{issue}" if issue else f"- `{wp_id}`: pending")
    lines.extend(
        [
            "",
            "## Closure Guard",
            "",
            "Do not auto-close the parent issue from the PR body. Issue closure is",
            "controlled by `mark-merged` after it verifies merged PR evidence and",
            "updates parent/child projection state.",
            "",
            f"Related: #{parent_issue}",
        ]
    )
    return ensure_sentinel("\n".join(lines))


def phase_labels_to_remove(phase: str) -> list[str]:
    return [label for label in PHASE_LABELS if label != phase]


def human_labels_to_remove(human: str) -> list[str]:
    return [label for label in HUMAN_LABELS if label != human]


def label_edit_command(kind: str, number: str, repo_slug: str, phase: str, human: str = HUMAN_AUTO) -> list[str]:
    command = ["gh", kind, "edit", str(number), "--repo", repo_slug]
    for label in phase_labels_to_remove(phase) + human_labels_to_remove(human):
        command.extend(["--remove-label", label])
    command.extend(["--add-label", ",".join((MANAGED, phase, human))])
    return command


def issue_comment_command(number: str, repo_slug: str, body_file: Path) -> list[str]:
    return ["gh", "issue", "comment", str(number), "--repo", repo_slug, "--body-file", str(body_file)]


def pr_comment_command(number: str, repo_slug: str, body_file: Path) -> list[str]:
    return ["gh", "pr", "comment", str(number), "--repo", repo_slug, "--body-file", str(body_file)]


def pr_body_edit_command(number: str, repo_slug: str, body_file: Path) -> list[str]:
    return ["gh", "pr", "edit", str(number), "--repo", repo_slug, "--body-file", str(body_file)]


def label_list_command(repo_slug: str) -> list[str]:
    return ["gh", "label", "list", "--repo", repo_slug, "--json", "name", "--limit", "1000"]


def label_create_command(repo_slug: str, spec: LabelSpec) -> list[str]:
    return [
        "gh",
        "label",
        "create",
        spec.name,
        "--repo",
        repo_slug,
        "--description",
        spec.description,
        "--color",
        spec.color,
    ]


def label_edit_catalog_command(repo_slug: str, spec: LabelSpec) -> list[str]:
    return [
        "gh",
        "label",
        "edit",
        spec.name,
        "--repo",
        repo_slug,
        "--description",
        spec.description,
        "--color",
        spec.color,
    ]


def is_github_sync_enabled(config: HostConfig) -> bool:
    return config.github_sync_enable


def transient_gh_error(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in text for marker in (" eof", "timeout", "timed out", "connection reset", "connection refused", "temporary failure"))


def run_command(command: list[str], repo: Path) -> CommandResult:
    attempts = 3 if command and command[0] == "gh" else 1
    last: subprocess.CompletedProcess[str] | None = None
    try:
        for attempt in range(attempts):
            result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
            last = result
            if not transient_gh_error(result) or attempt == attempts - 1:
                break
            time.sleep(0.5 * (attempt + 1))
    except FileNotFoundError as exc:
        return CommandResult(command=command, returncode=127, stdout="", stderr=str(exc))
    result = last
    if result is None:
        return CommandResult(command=command, returncode=1, stdout="", stderr="command did not run")
    return CommandResult(command=command, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def parse_issue_number(text: str) -> str:
    match = re.search(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d+)\b", text)
    return match.group(1) if match else ""


def parse_pr_number(text: str) -> str:
    match = re.search(r"https://github\.com/[^/]+/[^/]+/pull/(\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d+)\b", text)
    return match.group(1) if match else ""


def pr_view_command(number: str, repo_slug: str) -> list[str]:
    return ["gh", "pr", "view", str(number), "--repo", repo_slug, "--json", "state,mergedAt,mergeCommit,url,headRefName,baseRefName"]


def validate_mission_pr_merge_evidence(
    repo: Path,
    config: HostConfig,
    bindings: dict[str, Any],
    repo_slug: str,
    *,
    execute: bool,
    merged_pr: str = "",
) -> dict[str, Any]:
    mission_pr = bindings.get("mission_pr") if isinstance(bindings.get("mission_pr"), dict) else {}
    number = str(merged_pr or mission_pr.get("number") or "")
    if not number:
        return {"status": "blocked", "reason": "missing mission PR binding; refusing to close issues without merged PR evidence"}
    command = pr_view_command(number, repo_slug)
    if not execute:
        planned_pr = dict(mission_pr)
        if merged_pr:
            planned_pr["number"] = str(merged_pr)
            planned_pr["binding_source"] = "verified-merged-pr-override"
        return {"status": "ready", "mission_pr": planned_pr, "merge_evidence_command": command}
    result = run_command(command, config.repo_root)
    if result.returncode != 0:
        return {"status": "blocked", "reason": "failed to read mission PR merge evidence", "mission_pr": mission_pr, "result": result.as_dict()}
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"status": "blocked", "reason": "invalid mission PR merge evidence JSON", "mission_pr": mission_pr, "result": result.as_dict()}
    state = str(payload.get("state") or "")
    merged_at = str(payload.get("mergedAt") or "")
    merge_commit = payload.get("mergeCommit")
    has_merge_commit = isinstance(merge_commit, dict) and bool(merge_commit.get("oid"))
    if state != "MERGED" or not merged_at or not has_merge_commit:
        return {
            "status": "blocked",
            "reason": "mission PR is not merged; refusing to close issues",
            "mission_pr": mission_pr,
            "state": state,
            "merged_at": merged_at,
            "has_merge_commit": has_merge_commit,
            "result": result.as_dict(),
        }
    verified_pr = dict(mission_pr)
    verified_pr["number"] = number
    if payload.get("url"):
        verified_pr["url"] = str(payload.get("url"))
    if payload.get("headRefName"):
        verified_pr["head"] = str(payload.get("headRefName"))
    if payload.get("baseRefName"):
        verified_pr["base"] = str(payload.get("baseRefName"))
    if merged_pr:
        verified_pr["binding_source"] = "verified-merged-pr-override"
    verified_pr["merged_at"] = merged_at
    verified_pr["merge_commit"] = str(merge_commit.get("oid"))
    evidence = {
        "state": state,
        "merged_at": merged_at,
        "merge_commit": str(merge_commit.get("oid")),
        "url": str(payload.get("url") or ""),
        "head": str(payload.get("headRefName") or ""),
        "base": str(payload.get("baseRefName") or ""),
        "result": result.as_dict(),
    }
    return {"status": "ready", "mission_pr": verified_pr, "merge_evidence": evidence}


def resolve_repo_slug(repo: Path, config: HostConfig, *, execute: bool) -> dict[str, Any]:
    if not is_github_sync_enabled(config):
        return {"status": "disabled", "reason": "GITHUB_SYNC_ENABLE is false"}
    if config.gh_repo_slug:
        return {"status": "ready", "repo_slug": config.gh_repo_slug, "source": "GH_REPO_SLUG"}
    command = ["gh", "repo", "view", "--json", "nameWithOwner"]
    if not execute:
        return {"status": "planned", "repo_slug": "", "command": command, "reason": "GH_REPO_SLUG missing"}
    if not shutil.which("gh"):
        return {"status": "blocked", "reason": "gh CLI not found"}
    result = run_command(command, repo)
    if result.returncode != 0:
        return {"status": "blocked", "reason": "failed to resolve repo slug", "result": result.as_dict()}
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"status": "blocked", "reason": "invalid gh repo view JSON", "result": result.as_dict()}
    slug = payload.get("nameWithOwner")
    if not isinstance(slug, str) or "/" not in slug:
        return {"status": "blocked", "reason": "gh repo view did not return nameWithOwner", "result": result.as_dict()}
    return {"status": "ready", "repo_slug": slug, "source": "gh repo view", "result": result.as_dict()}


def github_preflight(repo: Path, config: HostConfig, *, execute: bool) -> dict[str, Any]:
    slug = resolve_repo_slug(repo, config, execute=execute)
    if slug.get("status") in {"disabled", "blocked"}:
        return slug
    if slug.get("status") == "planned":
        return slug
    if not execute:
        return slug
    if not shutil.which("gh"):
        return {"status": "blocked", "reason": "gh CLI not found"}
    auth = run_command(["gh", "auth", "status", "--hostname", "github.com"], repo)
    if auth.returncode != 0:
        return {"status": "blocked", "reason": "gh auth status failed", "result": auth.as_dict()}
    payload = dict(slug)
    payload["auth"] = auth.as_dict()
    return payload


def ensure_label_catalog(
    repo: Path,
    config: HostConfig,
    *,
    execute: bool = False,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preflight = preflight or github_preflight(repo, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    repo_slug = planned_repo_slug(preflight, config)
    planned = [label_create_command(repo_slug, spec) for spec in LABEL_SPECS]
    if not execute:
        return {"status": "planned", "preflight": preflight, "commands": planned}
    listed = run_command(label_list_command(repo_slug), repo)
    if listed.returncode != 0:
        return {"status": "blocked", "reason": "failed to list GitHub labels", "result": listed.as_dict()}
    try:
        payload = json.loads(listed.stdout) if listed.stdout.strip() else []
    except json.JSONDecodeError:
        return {"status": "blocked", "reason": "invalid gh label list JSON", "result": listed.as_dict()}
    existing = {str(item.get("name")) for item in payload if isinstance(item, dict) and item.get("name")}
    results: list[dict[str, Any]] = [listed.as_dict()]
    created: list[str] = []
    updated: list[str] = []
    for spec in LABEL_SPECS:
        command = label_edit_catalog_command(repo_slug, spec) if spec.name in existing else label_create_command(repo_slug, spec)
        result = run_command(command, repo)
        results.append(result.as_dict())
        if result.returncode != 0:
            return {"status": "blocked", "reason": f"failed to sync label {spec.name}", "results": results}
        if spec.name in existing:
            updated.append(spec.name)
        else:
            created.append(spec.name)
    return {"status": "ready", "created": created, "updated": updated, "results": results}


def github_write_preflight(repo: Path, config: HostConfig, *, execute: bool = False) -> dict[str, Any]:
    preflight = github_preflight(repo, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return preflight
    labels = ensure_label_catalog(repo, config, execute=execute, preflight=preflight)
    if labels.get("status") in {"disabled", "blocked"}:
        return {"status": labels["status"], "reason": labels.get("reason"), "preflight": preflight, "labels": labels}
    result = dict(preflight)
    result["labels"] = labels
    return result


def planned_repo_slug(preflight: dict[str, Any], config: HostConfig) -> str:
    return str(preflight.get("repo_slug") or config.gh_repo_slug or "<repo-slug>")


def mission_title(mission: str, source: dict[str, Any]) -> str:
    title = source.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()[:96]
    return f"Consensus R&D mission: {mission}"[:96]


def mission_source(mission_path: Path) -> dict[str, Any]:
    intake = load_json(mission_path / "consensus-rnd" / "intake.json")
    meta = load_json(mission_path / "meta.json")
    existing = meta.get("consensus_rnd_spec") if isinstance(meta, dict) else None
    if isinstance(existing, dict):
        merged = dict(existing)
        merged.update({key: value for key, value in intake.items() if value})
        return merged
    return intake


def existing_source_issue(source: dict[str, Any]) -> str:
    value = source.get("source_issue")
    return str(value) if value else ""


def is_issue_bound_as_child(repo: Path, issue_number: str) -> bool:
    if not issue_number:
        return False
    specs_dir = repo / "kitty-specs"
    if not specs_dir.is_dir():
        return False
    for path in specs_dir.glob("*/consensus-rnd/github-bindings.json"):
        bindings = load_json(path)
        children = bindings.get("child_issues") if isinstance(bindings, dict) else None
        if not isinstance(children, dict):
            continue
        for child in children.values():
            if isinstance(child, dict) and str(child.get("number") or "") == issue_number:
                return True
    return False


def write_body_file(mission_path: Path, name: str, body: str) -> Path:
    out_dir = mission_path / "consensus-rnd" / "github"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return stem[:80] or "commentary"


def read_optional_details(config: HostConfig, details_file: str = "", detail: str = "") -> str:
    if details_file:
        path = Path(details_file).expanduser()
        if not path.is_absolute():
            path = config.repo_root / path
        return path.read_text(encoding="utf-8")
    return detail


def ensure_parent_issue(repo: Path, mission: str, *, execute: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    bindings = load_bindings(path)
    parent = bindings.get("parent_issue") if isinstance(bindings.get("parent_issue"), dict) else {}
    if parent.get("number"):
        return {"status": "ready", "parent_issue": parent, "bindings_path": str(bindings_path(path))}
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    source = mission_source(path)
    source_issue = existing_source_issue(source)
    if source_issue and is_issue_bound_as_child(config.repo_root, source_issue):
        source_issue = ""
    title = mission_title(mission, source)
    if source_issue:
        parent = {"number": source_issue, "source": "source_issue", "title": title}
        bindings["parent_issue"] = parent
        append_event(bindings, {"kind": "parent-issue-reused", "number": source_issue})
        out = write_bindings(path, bindings)
        return {"status": "ready", "parent_issue": parent, "bindings_path": str(out)}
    body = build_parent_issue_body(mission, path, title, source)
    body_file = write_body_file(path, "parent-issue.md", body)
    repo_slug = planned_repo_slug(preflight, config)
    command = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo_slug,
        "--title",
        title,
        "--body-file",
        str(body_file),
        "--label",
        ",".join((MANAGED, PHASE_DESIGN_SOLVING, HUMAN_AUTO)),
    ]
    if not execute:
        return {"status": "planned", "command": command, "body_file": str(body_file), "preflight": preflight}
    result = run_command(command, config.repo_root)
    if result.returncode != 0:
        return {"status": "blocked", "reason": "failed to create parent issue", "result": result.as_dict()}
    number = parse_issue_number(result.stdout)
    if not number:
        return {"status": "blocked", "reason": "failed to parse parent issue number", "result": result.as_dict()}
    parent = {"number": number, "source": "created", "title": title, "url": result.stdout.strip()}
    bindings["parent_issue"] = parent
    append_event(bindings, {"kind": "parent-issue-created", "number": number})
    out = write_bindings(path, bindings)
    return {"status": "created", "parent_issue": parent, "bindings_path": str(out), "result": result.as_dict()}


def wp_files(mission_path: Path) -> list[dict[str, str]]:
    tasks = mission_path / "tasks"
    if not tasks.is_dir():
        return []
    items: list[dict[str, str]] = []
    for path in sorted(tasks.glob("WP*.md")):
        wp_id = path.name.split("-", 1)[0]
        items.append(
            {
                "wp_id": wp_id,
                "path": mission_relative_path(mission_path, path),
                "title": wp_prompt_title(path, wp_id),
            }
        )
    return items


def wp_prompt_title(path: Path, wp_id: str) -> str:
    fallback = path.stem.split("-", 1)[1].replace("-", " ") if "-" in path.stem else path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    if not text.startswith("---"):
        return fallback
    for line in text.splitlines()[1:80]:
        if line.strip() == "---":
            break
        match = re.match(r"\s*title:\s*(.+?)\s*$", line)
        if match:
            value = match.group(1).strip().strip('"').strip("'")
            return value or fallback
    return fallback


def child_issue_title(mission: str, wp: dict[str, str]) -> str:
    wp_id = wp["wp_id"]
    readable = wp.get("title") or wp_id
    prefix = f"{wp_id}: {readable}"
    suffix = f" ({mission})"
    max_len = 96
    if len(prefix) + len(suffix) <= max_len:
        return prefix + suffix
    remaining = max_len - len(prefix) - 4
    if remaining <= 12:
        return prefix[:max_len]
    return f"{prefix} ({mission[:remaining]}...)"


def ensure_child_issues(repo: Path, mission: str, *, execute: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    bindings = load_bindings(path)
    parent = ensure_parent_issue(config.repo_root, mission, execute=execute)
    if parent.get("status") in {"disabled", "blocked"}:
        return {"status": parent["status"], "reason": parent.get("reason"), "parent": parent}
    bindings = load_bindings(path)
    parent_issue = parent.get("parent_issue", {}).get("number") or bindings.get("parent_issue", {}).get("number")
    if not parent_issue and not execute and parent.get("status") == "planned":
        parent_issue = "<parent_issue>"
    if not parent_issue:
        return {"status": "blocked", "reason": "missing parent issue", "parent": parent}
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    repo_slug = planned_repo_slug(preflight, config)
    child_issues = bindings.get("child_issues")
    if not isinstance(child_issues, dict):
        child_issues = {}
    planned: list[dict[str, Any]] = []
    created: list[dict[str, Any]] = []
    for wp in wp_files(path):
        wp_id = wp["wp_id"]
        existing = child_issues.get(wp_id)
        if isinstance(existing, dict) and existing.get("number"):
            continue
        body = build_child_issue_body(mission, wp, str(parent_issue), str(bindings.get("mission_pr", {}).get("number") or ""))
        body_file = write_body_file(path, f"{wp_id}-issue.md", body)
        title = child_issue_title(mission, wp)
        command = [
            "gh",
            "issue",
            "create",
            "--repo",
            repo_slug,
            "--title",
            title,
            "--body-file",
            str(body_file),
            "--label",
            ",".join((MANAGED, PHASE_CONSENSUS_REACHED, HUMAN_AUTO)),
        ]
        planned_item = {"wp_id": wp_id, "command": command, "body_file": str(body_file)}
        if not execute:
            planned.append(planned_item)
            continue
        result = run_command(command, config.repo_root)
        if result.returncode != 0:
            created.append({"wp_id": wp_id, "status": "blocked", "reason": "failed to create child issue", "result": result.as_dict()})
            continue
        number = parse_issue_number(result.stdout)
        if not number:
            created.append({"wp_id": wp_id, "status": "blocked", "reason": "failed to parse child issue number", "result": result.as_dict()})
            continue
        child_issues[wp_id] = {"number": number, "title": title, "wp_path": wp["path"], "url": result.stdout.strip()}
        created.append({"wp_id": wp_id, "status": "created", "number": number, "result": result.as_dict()})
        append_event(bindings, {"kind": "child-issue-created", "wp_id": wp_id, "number": number})
    bindings["child_issues"] = child_issues
    out = write_bindings(path, bindings)
    status = "planned" if planned else "ready"
    if execute and any(item.get("status") == "blocked" for item in created):
        status = "blocked"
    elif execute and created:
        status = "created"
    return {
        "status": status,
        "parent": parent,
        "planned": planned,
        "created": created,
        "child_issues": child_issues,
        "bindings_path": str(out),
    }


def sync_wp_status(repo: Path, mission: str, wp_id: str, phase: str, *, detail: str = "", execute: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    lane_guard = validate_wp_phase_against_kitty_lane(path, wp_id, phase)
    if lane_guard.get("status") == "blocked":
        return lane_guard
    bindings = load_bindings(path)
    child = bindings.get("child_issues", {}).get(wp_id) if isinstance(bindings.get("child_issues"), dict) else None
    if not isinstance(child, dict) or not child.get("number"):
        child_sync = ensure_child_issues(config.repo_root, mission, execute=execute)
        bindings = load_bindings(path)
        child = bindings.get("child_issues", {}).get(wp_id) if isinstance(bindings.get("child_issues"), dict) else None
        if not isinstance(child, dict) or not child.get("number"):
            return {"status": "blocked", "reason": "missing child issue binding", "child_sync": child_sync}
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    number = str(child["number"])
    repo_slug = planned_repo_slug(preflight, config)
    banner = build_status_banner(
        phase=phase,
        mission=mission,
        wp_id=wp_id,
        issue=number,
        pr=str(bindings.get("mission_pr", {}).get("number") or ""),
        detail=detail,
    )
    body_file = write_body_file(path, f"{wp_id}-{phase}-banner.md", banner)
    commands = [
        label_edit_command("issue", number, repo_slug, phase),
        issue_comment_command(number, repo_slug, body_file),
    ]
    if not execute:
        return {"status": "planned", "commands": commands, "body_file": str(body_file), "preflight": preflight}
    results = [run_command(command, config.repo_root).as_dict() for command in commands]
    if any(result["returncode"] != 0 for result in results):
        return {"status": "blocked", "reason": "failed to sync WP issue status", "results": results}
    out = record_wp_status_sync(path, wp_id, number, phase)
    return {"status": "synced", "issue": number, "phase": phase, "results": results, "bindings_path": str(out)}


def commentary_issue_number(bindings: dict[str, Any], *, issue: str = "", wp_id: str = "") -> str:
    if issue:
        return issue
    if wp_id:
        child = bindings.get("child_issues", {}).get(wp_id) if isinstance(bindings.get("child_issues"), dict) else None
        if isinstance(child, dict) and child.get("number"):
            return str(child["number"])
        return ""
    parent = bindings.get("parent_issue") if isinstance(bindings.get("parent_issue"), dict) else None
    return str(parent.get("number") or "") if isinstance(parent, dict) else ""


def post_commentary(
    repo: Path,
    mission: str,
    *,
    kind: str,
    title: str,
    summary: str,
    detail: str = "",
    details_file: str = "",
    issue: str = "",
    wp_id: str = "",
    score: str = "",
    verdict: str = "",
    next_step: str = "",
    execute: bool = False,
) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    bindings = load_bindings(path)
    try:
        normalized_kind = normalize_commentary_kind(kind)
        details = read_optional_details(config, details_file=details_file, detail=detail)
    except (OSError, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}
    number = commentary_issue_number(bindings, issue=issue, wp_id=wp_id)
    if not number:
        return {"status": "blocked", "reason": "missing target issue binding"}
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    repo_slug = planned_repo_slug(preflight, config)
    body = build_commentary_body(
        kind=normalized_kind,
        title=title,
        mission=mission,
        summary=summary,
        details=details,
        wp_id=wp_id,
        issue=number,
        pr=str(bindings.get("mission_pr", {}).get("number") or ""),
        score=score,
        verdict=verdict,
        next_step=next_step,
    )
    stem = safe_file_stem("-".join(part for part in (wp_id, normalized_kind, title) if part))
    body_file = write_body_file(path, f"{stem}.md", body)
    command = issue_comment_command(number, repo_slug, body_file)
    if not execute:
        return {"status": "planned", "command": command, "body_file": str(body_file), "preflight": preflight}
    result = run_command(command, config.repo_root)
    if result.returncode != 0:
        return {"status": "blocked", "reason": "failed to post commentary", "result": result.as_dict()}
    out = append_binding_event(
        path,
        {
            "kind": "commentary-posted",
            "commentary_kind": normalized_kind,
            "issue": number,
            "wp_id": wp_id,
            "title": title,
        },
    )
    return {
        "status": "posted",
        "issue": number,
        "commentary_kind": normalized_kind,
        "body_file": str(body_file),
        "result": result.as_dict(),
        "bindings_path": str(out),
    }


FINAL_BRANCH_PATTERNS = (
    re.compile(r"(?im)^\s*-\s*(?:Final merge target|Planning/base branch)\s*:\s*`([^`]+)`"),
    re.compile(r"(?im)^\s*-\s*GitHub mission PR base and final landing branch\s*:\s*`([^`]+)`"),
    re.compile(r"(?im)^\s*-\s*Base branch\s*:\s*`([^`]+)`"),
)


def final_landing_branch_for_mission(mission_path: Path) -> str:
    for relative in ("plan.md", "spec.md", "consensus-rnd/intake.md"):
        path = mission_path / relative
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern in FINAL_BRANCH_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1).strip()
    return ""


def target_branch_for_mission(mission_path: Path) -> str:
    final_branch = final_landing_branch_for_mission(mission_path)
    if final_branch:
        return final_branch
    meta = load_json(mission_path / "meta.json")
    value = meta.get("target_branch")
    return str(value) if value else "main"


def is_spec_kitty_lane_branch(branch: str) -> bool:
    return "-lane-" in branch


def meta_target_branch_for_mission(mission_path: Path) -> str:
    meta = load_json(mission_path / "meta.json")
    value = meta.get("target_branch")
    return str(value).strip() if value else ""


def candidate_mission_branches(repo: Path, mission: str, mission_path: Path | None = None) -> list[str]:
    branches = [
        meta_target_branch_for_mission(mission_path) if mission_path else "",
        f"kitty/{mission}",
        f"kitty/mission-{mission}",
        f"codex/{mission}",
        f"codex/mission-{mission}",
        mission,
    ]
    return [branch for branch in dict.fromkeys(branches) if not is_spec_kitty_lane_branch(branch)]


def branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=repo, capture_output=True, text=True, check=False)
    return result.returncode == 0


def branch_has_commits(repo: Path, branch: str, base: str) -> bool:
    result = subprocess.run(["git", "rev-list", "--count", f"{base}..{branch}"], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return False


def github_base_validation_ref(repo: Path, base: str) -> str:
    """Use the remote-tracking PR base when available.

    A controller workspace may have a local ``feature/*`` branch advanced to
    the candidate head while GitHub's PR base is still ``origin/feature/*``.
    Validate PR head commits against the remote-tracking base to match GitHub's
    comparison semantics.
    """
    origin_base = f"origin/{base}"
    return origin_base if branch_exists(repo, origin_base) else base


def branch_or_origin_ref_has_commits(repo: Path, branch: str, base: str) -> bool:
    """Return True when a local branch or its origin tracking ref has commits.

    GitHub PR heads are often remote-only from the controller workspace after a
    push. Keep the public GitHub head as ``branch`` while using
    ``origin/<branch>`` only for local git validation.
    """
    validation_base = github_base_validation_ref(repo, base)
    if branch_exists(repo, branch) and branch_has_commits(repo, branch, validation_base):
        return True
    origin_branch = f"origin/{branch}"
    return branch_exists(repo, origin_branch) and branch_has_commits(repo, origin_branch, validation_base)


def resolve_mission_branch(repo: Path, mission: str, base: str, *, explicit_head: str = "", mission_path: Path | None = None) -> str:
    if explicit_head:
        if is_spec_kitty_lane_branch(explicit_head):
            return ""
        return explicit_head if branch_or_origin_ref_has_commits(repo, explicit_head, base) else ""
    for branch in candidate_mission_branches(repo, mission, mission_path=mission_path):
        if branch_or_origin_ref_has_commits(repo, branch, base):
            return branch
    return ""


def open_or_update_mission_pr(repo: Path, mission: str, *, head_override: str = "", execute: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    base = target_branch_for_mission(path)
    if head_override and is_spec_kitty_lane_branch(head_override):
        return {
            "status": "blocked",
            "reason": "mission PR head must be a mission branch, not a WP lane branch",
            "base": base,
            "head_override": head_override,
        }
    head = resolve_mission_branch(config.repo_root, mission, base, explicit_head=head_override, mission_path=path)
    if not head:
        reason = "explicit head branch does not exist or has no commits" if head_override else "mission branch has no commits or cannot be resolved"
        return {"status": "blocked", "reason": reason, "base": base, "head_override": head_override}
    if head == base:
        return {
            "status": "blocked",
            "reason": "mission PR head must differ from base",
            "base": base,
            "head": head,
        }
    bindings = load_bindings(path)
    parent = ensure_parent_issue(config.repo_root, mission, execute=execute)
    if parent.get("status") in {"disabled", "blocked"}:
        return {"status": parent["status"], "reason": parent.get("reason"), "parent": parent}
    parent_issue = str(parent.get("parent_issue", {}).get("number") or bindings.get("parent_issue", {}).get("number") or "")
    if not parent_issue:
        return {"status": "blocked", "reason": "missing parent issue", "parent": parent}
    child_sync = ensure_child_issues(config.repo_root, mission, execute=execute)
    if child_sync.get("status") in {"disabled", "blocked"}:
        return {"status": child_sync["status"], "reason": child_sync.get("reason"), "parent": parent, "child_sync": child_sync}
    bindings = load_bindings(path)
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    repo_slug = planned_repo_slug(preflight, config)
    list_command = ["gh", "pr", "list", "--repo", repo_slug, "--head", head, "--base", base, "--json", "number,url"]
    body = build_mission_pr_body(mission, parent_issue, bindings.get("child_issues", {}) if isinstance(bindings.get("child_issues"), dict) else {})
    body_file = write_body_file(path, "mission-pr.md", body)
    create_command = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo_slug,
        "--base",
        base,
        "--head",
        head,
        "--title",
        f"{mission}: Consensus R&D mission",
        "--body-file",
        str(body_file),
        "--label",
        ",".join((MANAGED, PHASE_REVIEWING, HUMAN_AUTO)),
    ]
    if not execute:
        return {
            "status": "planned",
            "list_command": list_command,
            "create_command": create_command,
            "body_file": str(body_file),
            "parent": parent,
            "child_sync": child_sync,
        }
    existing = run_command(list_command, config.repo_root)
    pr_number = ""
    pr_url = ""
    if existing.returncode == 0:
        try:
            prs = json.loads(existing.stdout) if existing.stdout.strip() else []
        except json.JSONDecodeError:
            prs = []
        if isinstance(prs, list) and prs:
            first = prs[0]
            if isinstance(first, dict):
                pr_number = str(first.get("number") or "")
                pr_url = str(first.get("url") or "")
    results: list[dict[str, Any]] = [existing.as_dict()]
    if not pr_number:
        created = run_command(create_command, config.repo_root)
        results.append(created.as_dict())
        if created.returncode != 0:
            return {"status": "blocked", "reason": "failed to create mission PR", "results": results}
        pr_number = parse_pr_number(created.stdout)
        pr_url = created.stdout.strip()
    if not pr_number:
        return {"status": "blocked", "reason": "failed to parse mission PR number", "results": results}
    pr_body = run_command(pr_body_edit_command(pr_number, repo_slug, body_file), config.repo_root)
    pr_labels = run_command(label_edit_command("pr", pr_number, repo_slug, PHASE_REVIEWING), config.repo_root)
    parent_labels = run_command(label_edit_command("issue", parent_issue, repo_slug, PHASE_PR_OPEN), config.repo_root)
    parent_banner = build_status_banner(phase=PHASE_PR_OPEN, mission=mission, issue=parent_issue, pr=pr_number, detail=f"Mission PR open from `{head}` to `{base}`.")
    parent_banner_file = write_body_file(path, "parent-pr-open-banner.md", parent_banner)
    parent_comment = run_command(issue_comment_command(parent_issue, repo_slug, parent_banner_file), config.repo_root)
    results.extend([pr_body.as_dict(), pr_labels.as_dict(), parent_labels.as_dict(), parent_comment.as_dict()])
    if any(result["returncode"] != 0 for result in results):
        return {"status": "blocked", "reason": "failed to sync mission PR status", "results": results}
    bindings["mission_pr"] = {"number": pr_number, "url": pr_url, "head": head, "base": base}
    append_event(bindings, {"kind": "mission-pr-open", "number": pr_number, "head": head, "base": base})
    out = write_bindings(path, bindings)
    return {"status": "ready", "mission_pr": bindings["mission_pr"], "results": results, "bindings_path": str(out)}


def mark_mission_merged(repo: Path, mission: str, *, merged_pr: str = "", skip_parent: bool = False, execute: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    bindings = load_bindings(path)
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    repo_slug = planned_repo_slug(preflight, config)
    lane_guard = validate_mission_done_lanes(path, bindings)
    if lane_guard.get("status") == "blocked":
        return {"status": "blocked", "reason": lane_guard.get("reason"), "preflight": preflight, "lane_guard": lane_guard}
    evidence = validate_mission_pr_merge_evidence(config.repo_root, config, bindings, repo_slug, execute=execute, merged_pr=merged_pr)
    if evidence.get("status") in {"disabled", "blocked"}:
        return {
            "status": evidence["status"],
            "reason": evidence.get("reason"),
            "preflight": preflight,
            "lane_guard": lane_guard,
            "merge_evidence": evidence,
        }
    commands: list[list[str]] = []
    parent_issue = str(bindings.get("parent_issue", {}).get("number") or "")
    if parent_issue and not skip_parent:
        commands.append(label_edit_command("issue", parent_issue, repo_slug, PHASE_MERGED))
        commands.append(["gh", "issue", "close", parent_issue, "--repo", repo_slug])
    evidence_pr = evidence.get("mission_pr") if isinstance(evidence.get("mission_pr"), dict) else {}
    mission_pr = str(evidence_pr.get("number") or bindings.get("mission_pr", {}).get("number") or "")
    if mission_pr:
        commands.append(label_edit_command("pr", mission_pr, repo_slug, PHASE_MERGED))
    for child in (bindings.get("child_issues") or {}).values():
        if isinstance(child, dict) and child.get("number"):
            commands.append(label_edit_command("issue", str(child["number"]), repo_slug, PHASE_MERGED))
            commands.append(["gh", "issue", "close", str(child["number"]), "--repo", repo_slug])
    if not execute:
        return {"status": "planned", "commands": commands, "lane_guard": lane_guard, "merge_evidence": evidence}
    results = [run_command(command, config.repo_root).as_dict() for command in commands]
    if any(result["returncode"] != 0 for result in results):
        return {"status": "blocked", "reason": "failed to mark mission merged", "results": results}
    if evidence_pr:
        bindings["mission_pr"] = evidence_pr
    mark_child_issue_bindings_merged(bindings)
    append_event(
        bindings,
        {
            "kind": "mission-merged",
            "merge_evidence": evidence.get("merge_evidence", {}),
            "merged_pr_override": str(merged_pr or ""),
            "parent_skipped": bool(skip_parent),
        },
    )
    out = write_bindings(path, bindings)
    return {"status": "synced", "results": results, "bindings_path": str(out), "lane_guard": lane_guard, "merge_evidence": evidence}


def bind_merged_pr(repo: Path, mission: str, *, merged_pr: str, execute: bool = False) -> dict[str, Any]:
    """Bind a verified merged PR without closing issues or changing phase labels."""
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    bindings = load_bindings(path)
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    if not str(merged_pr or "").strip():
        return {"status": "blocked", "reason": "--merged-pr is required for bind-merged-pr", "preflight": preflight}
    repo_slug = planned_repo_slug(preflight, config)
    evidence = validate_mission_pr_merge_evidence(config.repo_root, config, bindings, repo_slug, execute=execute, merged_pr=merged_pr)
    if evidence.get("status") in {"disabled", "blocked"}:
        return {"status": evidence["status"], "reason": evidence.get("reason"), "preflight": preflight, "merge_evidence": evidence}
    evidence_pr = evidence.get("mission_pr") if isinstance(evidence.get("mission_pr"), dict) else {}
    if not execute:
        return {"status": "planned", "preflight": preflight, "mission_pr": evidence_pr, "merge_evidence": evidence}
    bindings["mission_pr"] = evidence_pr
    append_event(
        bindings,
        {
            "kind": "merged-pr-bound",
            "merge_evidence": evidence.get("merge_evidence", {}),
            "merged_pr_override": str(merged_pr or ""),
            "issues_closed": False,
        },
    )
    out = write_bindings(path, bindings)
    return {"status": "synced", "bindings_path": str(out), "mission_pr": evidence_pr, "merge_evidence": evidence}


def source_contract() -> dict[str, Any]:
    return {
        "labels_source_anchor": LABELS_SOURCE_ANCHOR,
        "sentinel_source_anchor": SENTINEL_SOURCE_ANCHOR,
        "status_banner_source_anchor": STATUS_BANNER_SOURCE_ANCHOR,
        "commentary_source_anchor": CONTROLLER_COMMENTARY_MARKER,
        "commentary_kinds": list(COMMENTARY_KINDS),
        "managed_label": MANAGED,
        "phase_labels": list(PHASE_LABELS),
        "human_labels": list(HUMAN_LABELS),
    }


def exit_code_for_result(result: dict[str, Any]) -> int:
    return 1 if result.get("status") in {"blocked", "failed", "error"} else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    parent = sub.add_parser("ensure-parent")
    parent.add_argument("--repo", default=".")
    parent.add_argument("--mission", required=True)
    parent.add_argument("--execute", action="store_true")
    children = sub.add_parser("ensure-children")
    children.add_argument("--repo", default=".")
    children.add_argument("--mission", required=True)
    children.add_argument("--execute", action="store_true")
    status = sub.add_parser("sync-wp-status")
    status.add_argument("--repo", default=".")
    status.add_argument("--mission", required=True)
    status.add_argument("--wp-id", required=True)
    status.add_argument("--phase", required=True)
    status.add_argument("--detail", default="")
    status.add_argument("--execute", action="store_true")
    commentary = sub.add_parser("post-commentary")
    commentary.add_argument("--repo", default=".")
    commentary.add_argument("--mission", required=True)
    commentary.add_argument("--kind", required=True, choices=COMMENTARY_KINDS)
    commentary.add_argument("--title", required=True)
    commentary.add_argument("--summary", required=True)
    commentary.add_argument("--detail", default="")
    commentary.add_argument("--details-file", default="")
    commentary.add_argument("--issue", default="")
    commentary.add_argument("--wp-id", default="")
    commentary.add_argument("--score", default="")
    commentary.add_argument("--verdict", default="")
    commentary.add_argument("--next-step", default="")
    commentary.add_argument("--execute", action="store_true")
    pr = sub.add_parser("open-pr")
    pr.add_argument("--repo", default=".")
    pr.add_argument("--mission", required=True)
    pr.add_argument("--head", default="")
    pr.add_argument("--execute", action="store_true")
    bind_pr = sub.add_parser("bind-merged-pr")
    bind_pr.add_argument("--repo", default=".")
    bind_pr.add_argument("--mission", required=True)
    bind_pr.add_argument("--merged-pr", required=True, help="Verified merged PR number to bind without closing issues.")
    bind_pr.add_argument("--execute", action="store_true")
    merged = sub.add_parser("mark-merged")
    merged.add_argument("--repo", default=".")
    merged.add_argument("--mission", required=True)
    merged.add_argument("--merged-pr", default="", help="Verified merged PR number to bind before closing legacy/catch-up mission projection.")
    merged.add_argument("--skip-parent", action="store_true", help="Do not close the parent issue; useful when a source issue is reused by later missions.")
    merged.add_argument("--execute", action="store_true")
    contract = sub.add_parser("contract")
    contract.add_argument("--repo", default=".")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if args.command == "ensure-parent":
        result = ensure_parent_issue(repo, args.mission, execute=args.execute)
    elif args.command == "ensure-children":
        result = ensure_child_issues(repo, args.mission, execute=args.execute)
    elif args.command == "sync-wp-status":
        result = sync_wp_status(repo, args.mission, args.wp_id, args.phase, detail=args.detail, execute=args.execute)
    elif args.command == "post-commentary":
        result = post_commentary(
            repo,
            args.mission,
            kind=args.kind,
            title=args.title,
            summary=args.summary,
            detail=args.detail,
            details_file=args.details_file,
            issue=args.issue,
            wp_id=args.wp_id,
            score=args.score,
            verdict=args.verdict,
            next_step=args.next_step,
            execute=args.execute,
        )
    elif args.command == "open-pr":
        result = open_or_update_mission_pr(repo, args.mission, head_override=args.head, execute=args.execute)
    elif args.command == "bind-merged-pr":
        result = bind_merged_pr(repo, args.mission, merged_pr=args.merged_pr, execute=args.execute)
    elif args.command == "mark-merged":
        result = mark_mission_merged(repo, args.mission, merged_pr=args.merged_pr, skip_parent=args.skip_parent, execute=args.execute)
    else:
        result = source_contract()
    print_json(result)
    return exit_code_for_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
