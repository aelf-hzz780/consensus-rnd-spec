#!/usr/bin/env python3
"""Slash-style intake adapter for consensus-rnd-spec."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from backend_common import detect_backend, load_config, parse_duration_seconds, print_json
from run_loop import run_loop
from spec_backend import evidence_hash, write_discovery_seed


SURFACE_RE = re.compile(r"/(?:codex-refactor-loop|consensus-rnd-spec)\b")
LOOP_RE = re.compile(r"/loop(?:\s+([^\s/]+))?")


def normalize_text(text: str) -> str:
    return text.replace("Human\uff1a", "Human:")


def collapse_ws(text: str) -> str:
    return " ".join(text.split())


def parse_intake(text: str) -> dict[str, Any]:
    normalized = normalize_text(text)
    loop_match = LOOP_RE.search(normalized)
    duration = loop_match.group(1) if loop_match and loop_match.group(1) else None
    surfaces = [match.group(0).lstrip("/") for match in SURFACE_RE.finditer(normalized)]
    without_loop = LOOP_RE.sub(" ", normalized, count=1)
    instructions = collapse_ws(SURFACE_RE.sub(" ", without_loop))
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
    instructions = str(parsed.get("instructions") or "").strip()
    if not instructions:
        return "consensus loop intake"
    compact = collapse_ws(instructions)
    if compact.startswith("Human:"):
        compact = compact[len("Human:") :].strip()
    return f"consensus intake: {compact}"[:96]


def body_from_intake(parsed: dict[str, Any], backend: dict[str, Any]) -> str:
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
        title = title_from_intake(parsed)
        body = body_from_intake(parsed, backend)
        plan["seed"] = {
            "status": "planned",
            "title": title,
            "body": body,
            "source": "synthetic_human_intake",
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
    parser.add_argument("--run", action="store_true", help="Run the controller after intake planning")
    parser.add_argument("--once", action="store_true", help="Run only one controller turn")
    parser.add_argument("--execute", action="store_true", help="Write seed artifacts and allow backend execution")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    text = read_text_arg(args)
    plan = plan_intake(repo, text)
    if args.execute:
        seed = execute_intake_seed(repo, plan)
        if seed is not None:
            plan["seed"] = seed
    if args.run:
        plan["loop"] = run_loop(repo, int(plan["duration_seconds"]), execute=args.execute, once=args.once)
    print_json(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
