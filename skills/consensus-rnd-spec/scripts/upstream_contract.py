#!/usr/bin/env python3
"""Source-regression checks against the upstream consensus-rnd contract."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

from backend_common import print_json


def has_label_projection(source: str, label: str) -> bool:
    if label in source:
        return True
    parts = label.split(":")
    if len(parts) != 3 or parts[0] != "crnd":
        return False
    group, slug = parts[1], parts[2]
    return (
        f'_spec("{group}", "{slug}"' in source
        or f"canonical_name(\"{group}\", \"{slug}\")" in source
        or f"_spec('{group}', '{slug}'" in source
        or f"canonical_name('{group}', '{slug}')" in source
    )


def load_github_sync_contract() -> dict[str, Any]:
    script = Path(__file__).resolve().parent / "github_sync.py"
    spec = importlib.util.spec_from_file_location("github_sync_contract", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["github_sync_contract"] = module
    spec.loader.exec_module(module)
    return module.source_contract()


def check_contract(upstream_skill_root: Path) -> dict[str, Any]:
    contract = load_github_sync_contract()
    skill_path = upstream_skill_root / "SKILL.md"
    labels_path = upstream_skill_root / "scripts" / "codex_refactor_loop" / "labels.py"
    missing: list[str] = []
    if not skill_path.is_file():
        missing.append(str(skill_path))
        skill_text = ""
    else:
        skill_text = skill_path.read_text(encoding="utf-8", errors="ignore")
    if not labels_path.is_file():
        missing.append(str(labels_path))
        labels_text = ""
    else:
        labels_text = labels_path.read_text(encoding="utf-8", errors="ignore")

    checks = {
        "sentinel_in_skill": contract["sentinel_source_anchor"] in skill_text,
        "status_banner_in_skill": contract["status_banner_source_anchor"] in skill_text,
        "managed_label_in_labels": has_label_projection(labels_text, contract["managed_label"]),
        "phase_anchor_in_labels": has_label_projection(labels_text, contract["labels_source_anchor"]),
        "human_auto_in_labels": has_label_projection(labels_text, "crnd:human:auto"),
    }
    missing_checks = [name for name, ok in checks.items() if not ok]
    status = "ready" if not missing and not missing_checks else "blocked"
    return {
        "status": status,
        "upstream_skill_root": str(upstream_skill_root),
        "missing_files": missing,
        "checks": checks,
        "missing_checks": missing_checks,
        "contract": contract,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-skill-root", required=True)
    args = parser.parse_args()
    result = check_contract(Path(args.upstream_skill_root).resolve())
    print_json(result)
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
