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
    payload["updated_at"] = utc_now()
    path = bindings_path(mission_path)
    write_json(path, payload)
    update_meta_github_summary(mission_path, payload)
    return path


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
        "bindings": str(bindings_path(mission_path)),
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


def ensure_sentinel(body: str) -> str:
    text = body.rstrip()
    if text.splitlines()[-1:] == [AUTO_LOOP_SENTINEL]:
        return text + "\n"
    return text + "\n\n" + AUTO_LOOP_SENTINEL + "\n"


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
    lines.extend(["", f"Closes #{parent_issue}"])
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


def write_body_file(mission_path: Path, name: str, body: str) -> Path:
    out_dir = mission_path / "consensus-rnd" / "github"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(body, encoding="utf-8")
    return path


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
        items.append({"wp_id": wp_id, "path": str(path), "title": wp_prompt_title(path, wp_id)})
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
    child["phase"] = phase
    child["last_status_at"] = utc_now()
    append_event(bindings, {"kind": "wp-status-synced", "wp_id": wp_id, "issue": number, "phase": phase})
    out = write_bindings(path, bindings)
    return {"status": "synced", "issue": number, "phase": phase, "results": results, "bindings_path": str(out)}


def target_branch_for_mission(mission_path: Path) -> str:
    meta = load_json(mission_path / "meta.json")
    value = meta.get("target_branch")
    return str(value) if value else "main"


def candidate_mission_branches(repo: Path, mission: str) -> list[str]:
    branches = [
        f"kitty/{mission}",
        f"kitty/mission-{mission}",
        f"codex/{mission}",
        mission,
    ]
    workspaces = repo / ".kittify" / "workspaces"
    if workspaces.is_dir():
        for path in sorted(workspaces.glob(f"*{mission}*.json")):
            data = load_json(path)
            for key in ("branch", "branch_name", "head", "head_ref"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    branches.insert(0, value)
    return list(dict.fromkeys(branches))


def branch_has_commits(repo: Path, branch: str, base: str) -> bool:
    result = subprocess.run(["git", "rev-list", "--count", f"{base}..{branch}"], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return False


def resolve_mission_branch(repo: Path, mission: str, base: str) -> str:
    for branch in candidate_mission_branches(repo, mission):
        if branch_has_commits(repo, branch, base):
            return branch
    return ""


def open_or_update_mission_pr(repo: Path, mission: str, *, execute: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
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
    base = target_branch_for_mission(path)
    head = resolve_mission_branch(config.repo_root, mission, base)
    if not head:
        return {"status": "blocked", "reason": "mission branch has no commits or cannot be resolved", "base": base}
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


def mark_mission_merged(repo: Path, mission: str, *, execute: bool = False) -> dict[str, Any]:
    config = load_config(repo)
    path = mission_dir(config.repo_root, mission)
    bindings = load_bindings(path)
    preflight = github_write_preflight(config.repo_root, config, execute=execute)
    if preflight.get("status") in {"disabled", "blocked"}:
        return {"status": preflight["status"], "reason": preflight.get("reason"), "preflight": preflight}
    repo_slug = planned_repo_slug(preflight, config)
    commands: list[list[str]] = []
    parent_issue = str(bindings.get("parent_issue", {}).get("number") or "")
    if parent_issue:
        commands.append(label_edit_command("issue", parent_issue, repo_slug, PHASE_MERGED))
    for child in (bindings.get("child_issues") or {}).values():
        if isinstance(child, dict) and child.get("number"):
            commands.append(label_edit_command("issue", str(child["number"]), repo_slug, PHASE_MERGED))
            commands.append(["gh", "issue", "close", str(child["number"]), "--repo", repo_slug])
    if not execute:
        return {"status": "planned", "commands": commands}
    results = [run_command(command, config.repo_root).as_dict() for command in commands]
    if any(result["returncode"] != 0 for result in results):
        return {"status": "blocked", "reason": "failed to mark mission merged", "results": results}
    append_event(bindings, {"kind": "mission-merged"})
    out = write_bindings(path, bindings)
    return {"status": "synced", "results": results, "bindings_path": str(out)}


def source_contract() -> dict[str, Any]:
    return {
        "labels_source_anchor": LABELS_SOURCE_ANCHOR,
        "sentinel_source_anchor": SENTINEL_SOURCE_ANCHOR,
        "status_banner_source_anchor": STATUS_BANNER_SOURCE_ANCHOR,
        "managed_label": MANAGED,
        "phase_labels": list(PHASE_LABELS),
        "human_labels": list(HUMAN_LABELS),
    }


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
    pr = sub.add_parser("open-pr")
    pr.add_argument("--repo", default=".")
    pr.add_argument("--mission", required=True)
    pr.add_argument("--execute", action="store_true")
    contract = sub.add_parser("contract")
    contract.add_argument("--repo", default=".")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if args.command == "ensure-parent":
        print_json(ensure_parent_issue(repo, args.mission, execute=args.execute))
    elif args.command == "ensure-children":
        print_json(ensure_child_issues(repo, args.mission, execute=args.execute))
    elif args.command == "sync-wp-status":
        print_json(sync_wp_status(repo, args.mission, args.wp_id, args.phase, detail=args.detail, execute=args.execute))
    elif args.command == "open-pr":
        print_json(open_or_update_mission_pr(repo, args.mission, execute=args.execute))
    else:
        print_json(source_contract())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
