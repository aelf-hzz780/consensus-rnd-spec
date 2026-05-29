#!/usr/bin/env python3
"""Lightweight discovery producer for consensus-rnd-spec.

This intentionally produces artifacts only. Converting findings to GitHub issues
or Spec Kitty missions belongs to the controller/backend handoff.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from backend_common import load_config, print_json
from spec_backend import evidence_hash


PATTERNS = {
    "todo": "TODO|FIXME|HACK",
    "large_python_file": "",
}


def rg_findings(repo: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["rg", "-n", "--glob", "!node_modules", "--glob", "!.git", "--glob", "!frontend/package-lock.json", PATTERNS["todo"], "."],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    findings: list[dict[str, Any]] = []
    if result.returncode not in (0, 1):
        return findings
    for line in result.stdout.splitlines()[:100]:
        parts = line.split(":", 2)
        if len(parts) == 3:
            findings.append({"kind": "todo-marker", "path": parts[0], "line": parts[1], "text": parts[2].strip()})
    return findings


def large_python_files(repo: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in sorted(repo.glob("**/*.py")):
        rel = path.relative_to(repo)
        if any(part in {".git", ".worktrees", "__pycache__"} for part in rel.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        if len(lines) >= 800:
            findings.append({"kind": "large-python-file", "path": str(rel), "line_count": len(lines)})
    return findings[:50]


def produce(repo: Path) -> dict[str, Any]:
    config = load_config(repo)
    findings = rg_findings(config.repo_root) + large_python_files(config.repo_root)
    runs = config.repo_root / ".consensus-rnd-spec" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifact = runs / f"discovery-{stamp}.json"
    payload = {
        "producer": "consensus-rnd-spec",
        "source": "repository-scan",
        "created_at": stamp,
        "repo_root": str(config.repo_root),
        "finding_count": len(findings),
        "findings": findings,
        "evidence_hash": evidence_hash(json.dumps(findings, ensure_ascii=False, sort_keys=True)),
    }
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"artifact": str(artifact), "payload": payload}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    print_json(produce(Path(args.repo).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
