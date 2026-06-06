#!/usr/bin/env python3
"""Slash-style intake adapter for consensus-rnd-spec."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from backend_common import detect_backend, load_config, parse_duration_seconds, print_json
from promote_discovery import promote as promote_discovery
from run_loop import run_loop
from spec_backend import evidence_hash, write_discovery_seed


SURFACE_RE = re.compile(r"/(?:codex-refactor-loop|consensus-rnd-spec)\b")
LOOP_RE = re.compile(r"/loop(?:\s+([^\s/]+))?")
MARKDOWN_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
TITLE_FIELD_RE = re.compile(r"^\s*Title\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
MISSION_TITLE_RE = re.compile(r"^\s*(?:mission\s+)?(\d+)\s*[:.]\s*(.+?)\s*$", re.IGNORECASE)


def normalize_text(text: str) -> str:
    return text.replace("Human\uff1a", "Human:")


def collapse_ws(text: str) -> str:
    return " ".join(text.split())


def trim_structured_text(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def extract_markdown_file(instructions: str) -> Path | None:
    for token in instructions.split():
        candidate = Path(token).expanduser()
        if candidate.suffix.lower() == ".md" and candidate.is_file():
            return candidate.resolve()
    return None


def current_branch(repo: Path) -> str:
    import subprocess

    result = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def seed_title(parsed: dict[str, Any]) -> str:
    return title_from_intake(parsed)


def first_markdown_h1(content: str) -> str:
    match = MARKDOWN_H1_RE.search(content)
    if not match:
        return ""
    return collapse_ws(match.group(1).strip())


def parse_intake(text: str) -> dict[str, Any]:
    normalized = normalize_text(text)
    loop_match = LOOP_RE.search(normalized)
    duration = loop_match.group(1) if loop_match and loop_match.group(1) else None
    surfaces = [match.group(0).lstrip("/") for match in SURFACE_RE.finditer(normalized)]
    without_loop = LOOP_RE.sub(" ", normalized, count=1)
    instructions = trim_structured_text(SURFACE_RE.sub(" ", without_loop))
    synthetic_human = ""
    if instructions:
        synthetic_human = f"Human: {instructions}"
    return {
        "raw_text": text,
        "duration": duration,
        "surfaces": surfaces,
        "instructions": instructions,
        "synthetic_human": synthetic_human,
    }


def title_from_intake(parsed: dict[str, Any]) -> str:
    prompt_file = parsed.get("prompt_file")
    if isinstance(prompt_file, dict):
        heading = prompt_file.get("heading")
        if isinstance(heading, str) and heading.strip():
            return heading.strip()[:96]
        title = prompt_file.get("title")
        if isinstance(title, str) and title.strip():
            return f"consensus intake: {title.strip()}"[:96]
    instructions = str(parsed.get("instructions") or "").strip()
    if not instructions:
        return "consensus loop intake"
    explicit_title = TITLE_FIELD_RE.search(instructions)
    if explicit_title:
        return collapse_ws(explicit_title.group(1).strip())[:96]
    compact = collapse_ws(instructions)
    if compact.startswith("Human:"):
        compact = compact[len("Human:") :].strip()
    mission_title = MISSION_TITLE_RE.match(compact)
    if mission_title:
        number, title = mission_title.groups()
        return f"{number}.{title.strip()}"[:96]
    return f"consensus intake: {compact}"[:96]


def body_from_intake(parsed: dict[str, Any], backend: dict[str, Any]) -> str:
    prompt_file = parsed.get("prompt_file")
    prompt_section: list[str] = []
    if isinstance(prompt_file, dict):
        prompt_section = [
            "",
            "## Referenced Plan",
            "",
            f"- Path: {prompt_file.get('path')}",
            f"- SHA256: {prompt_file.get('sha256')}",
            "",
            str(prompt_file.get("content") or "").strip(),
            "",
        ]
    lines = [
        "# Consensus R&D slash intake",
        "",
        f"- Source: consensus-rnd-spec",
        f"- Backend detected: {backend.get('backend')}",
        f"- Requested surfaces: {', '.join(parsed.get('surfaces') or []) or 'default'}",
        f"- Requested duration: {parsed.get('duration') or 'host default'}",
        f"- Evidence hash: {evidence_hash(str(parsed.get('raw_text') or ''))}",
        "",
        "## Synthetic intake",
        "",
        str(parsed.get("synthetic_human") or "Human: continue the unattended Consensus R&D loop."),
        *prompt_section,
        "## Branch Contract",
        "",
        "- Primary landing branch: `feature/cockpit`.",
        "- If additional branches are needed, they must match `xxx/cockpit-xxxx`.",
        "- Do not create unrelated `codex/socialops-*` or generic mission branches for this intake.",
        "",
        "## Guardrails",
        "",
        "- Synthetic `Human:` text is intake only, not maintainer approval.",
        "- Spec Kitty repositories must use mission and work-package state transitions.",
        "- Native fallback must delegate to codex-refactor-loop when explicitly enabled.",
    ]
    return "\n".join(lines) + "\n"


def plan_intake(repo: Path, text: str) -> dict[str, Any]:
    config = load_config(repo)
    parsed = parse_intake(text)
    prompt_file = extract_markdown_file(str(parsed.get("instructions") or ""))
    if prompt_file is not None:
        content = prompt_file.read_text(encoding="utf-8")
        parsed["prompt_file"] = {
            "path": str(prompt_file),
            "title": prompt_file.stem,
            "heading": first_markdown_h1(content),
            "sha256": evidence_hash(content),
            "content": content,
        }
    backend = detect_backend(config.repo_root, mode=config.backend_mode)
    duration_seconds = parse_duration_seconds(parsed["duration"], config.loop_interval_seconds)
    plan: dict[str, Any] = {
        "repo_root": str(config.repo_root),
        "parsed": parsed,
        "duration_seconds": duration_seconds,
        "backend": backend,
        "seed": None,
    }
    if parsed["synthetic_human"] and config.synthetic_human_intake_enable:
        title = seed_title(parsed)
        body = body_from_intake(parsed, backend)
        plan["seed"] = {
            "status": "planned",
            "title": title,
            "body": body,
            "source": "synthetic_human_intake",
            "metadata": {
                "slash_surface": parsed.get("surfaces") or [],
                "prompt_file": parsed.get("prompt_file"),
                "branch_contract": {
                    "primary": "feature/cockpit",
                    "additional_pattern": "xxx/cockpit-xxxx",
                    "current_branch": current_branch(config.repo_root),
                },
            },
            "mission_type": config.spec_kitty_mission_type,
            "evidence_hash": evidence_hash(title + "\n" + body),
            "handoff": "spec-kitty" if backend.get("backend") == "spec-kitty" else "artifact-only",
        }
    return plan


def execute_intake_seed(repo: Path, plan: dict[str, Any]) -> dict[str, Any] | None:
    seed = plan.get("seed")
    if not isinstance(seed, dict):
        return None
    return write_discovery_seed(
        repo,
        title=str(seed["title"]),
        body=str(seed["body"]),
        source=str(seed.get("source") or "synthetic_human_intake"),
        source_kind=str(seed.get("source_kind") or seed.get("source") or "synthetic_human_intake"),
        source_issue=str(seed.get("source_issue") or ""),
        source_pr=str(seed.get("source_pr") or ""),
        source_url=str(seed.get("source_url") or ""),
        metadata=seed.get("metadata") if isinstance(seed.get("metadata"), dict) else None,
    )


def read_text_arg(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    if args.text:
        return args.text
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Host repository root")
    parser.add_argument("--text", help="Slash-style intake text")
    parser.add_argument("--prompt-file", help="Read slash-style intake text from a file")
    parser.add_argument("--source-kind", default="", help="Optional source kind for issue/PR/native intake artifacts")
    parser.add_argument("--source-issue", default="", help="Optional source GitHub issue number")
    parser.add_argument("--source-pr", default="", help="Optional source GitHub PR number")
    parser.add_argument("--source-url", default="", help="Optional source URL")
    parser.add_argument("--run", action="store_true", help="Run the controller after intake planning")
    parser.add_argument("--once", action="store_true", help="Run only one controller turn")
    parser.add_argument("--execute", action="store_true", help="Write seed artifacts and allow backend execution")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    text = read_text_arg(args)
    plan = plan_intake(repo, text)
    if isinstance(plan.get("seed"), dict):
        plan["seed"].update(
            {
                "source_kind": args.source_kind or plan["seed"].get("source"),
                "source_issue": args.source_issue,
                "source_pr": args.source_pr,
                "source_url": args.source_url,
            }
        )
    if args.execute:
        seed = execute_intake_seed(repo, plan)
        if seed is not None:
            plan["seed"] = seed
            if plan.get("backend", {}).get("backend") == "spec-kitty":
                artifact = seed.get("artifact")
                if isinstance(artifact, str) and artifact:
                    plan["promotion"] = promote_discovery(repo, artifact=Path(artifact), execute=True)
    if args.run:
        plan["loop"] = run_loop(repo, int(plan["duration_seconds"]), execute=args.execute, once=args.once)
    print_json(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
