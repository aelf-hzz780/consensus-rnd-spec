from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
for name in ("backend_common", "spec_backend"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)

SPEC = importlib.util.spec_from_file_location("promote_discovery", SCRIPT_DIR / "promote_discovery.py")
promote_discovery = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["promote_discovery"] = promote_discovery
SPEC.loader.exec_module(promote_discovery)


class PromoteDiscoveryTests(unittest.TestCase):
    def test_promote_plans_latest_unpromoted_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            runs = repo / ".consensus-rnd-spec" / "runs"
            runs.mkdir(parents=True)
            artifact = runs / "discovery-20260101T000000Z.json"
            artifact.write_text(
                json.dumps({"evidence_hash": "abc123", "finding_count": 0, "findings": []}),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True):
                result = promote_discovery.promote(repo, execute=False)

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["seed"]["evidence_hash"], "abc123")

    def test_execute_records_promotion_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            runs = repo / ".consensus-rnd-spec" / "runs"
            runs.mkdir(parents=True)
            artifact = runs / "discovery-20260101T000000Z.json"
            artifact.write_text(
                json.dumps({"evidence_hash": "abc123", "finding_count": 0, "findings": []}),
                encoding="utf-8",
            )
            with mock.patch.object(
                promote_discovery,
                "run_specify",
                return_value={"command": ["spec-kitty"], "returncode": 0, "payload": {"slug": "demo"}, "stderr": ""},
            ), mock.patch.dict(os.environ, {}, clear=True):
                result = promote_discovery.promote(repo, execute=True)
            log = repo / ".consensus-rnd-spec" / "state" / "promotions.jsonl"
            log_exists = log.exists()

        self.assertEqual(result["status"], "promoted")
        self.assertTrue(log_exists)

    def test_execute_writes_mission_intake_when_specify_returns_feature_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            runs = repo / ".consensus-rnd-spec" / "runs"
            runs.mkdir(parents=True)
            artifact = runs / "discovery-20260101T000000Z.json"
            artifact.write_text(
                json.dumps(
                    {
                        "evidence_hash": "abc123",
                        "finding_count": 0,
                        "findings": [],
                        "synthetic_human_intake": True,
                    }
                ),
                encoding="utf-8",
            )
            mission_dir = repo / "kitty-specs" / "demo-mission"
            mission_dir.mkdir(parents=True)
            (mission_dir / "meta.json").write_text('{"mission_slug":"demo-mission"}\n', encoding="utf-8")
            with mock.patch.object(
                promote_discovery,
                "run_specify",
                return_value={
                    "command": ["spec-kitty"],
                    "returncode": 0,
                    "payload": {"mission_slug": "demo-mission", "feature_dir": str(mission_dir)},
                    "stderr": "",
                },
            ), mock.patch.dict(os.environ, {}, clear=True):
                result = promote_discovery.promote(repo, execute=True)

            intake_path = mission_dir / "consensus-rnd" / "intake.md"
            meta = json.loads((mission_dir / "meta.json").read_text(encoding="utf-8"))
            intake_exists = intake_path.exists()

        self.assertEqual(result["mission_slug"], "demo-mission")
        self.assertTrue(intake_exists)
        self.assertEqual(meta["consensus_rnd_spec"]["evidence_hash"], "abc123")

    def test_execute_writes_issue_pr_source_metadata_to_mission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            runs = repo / ".consensus-rnd-spec" / "runs"
            runs.mkdir(parents=True)
            artifact = runs / "discovery-20260101T000000Z-pr.json"
            artifact.write_text(
                json.dumps(
                    {
                        "evidence_hash": "pr123",
                        "finding_count": 0,
                        "findings": [],
                        "source_kind": "github_pr",
                        "source_pr": "123",
                        "source_url": "https://github.com/example/repo/pull/123",
                    }
                ),
                encoding="utf-8",
            )
            mission_dir = repo / "kitty-specs" / "demo-pr-mission"
            mission_dir.mkdir(parents=True)
            (mission_dir / "meta.json").write_text('{"mission_slug":"demo-pr-mission"}\n', encoding="utf-8")
            with mock.patch.object(
                promote_discovery,
                "run_specify",
                return_value={
                    "command": ["spec-kitty"],
                    "returncode": 0,
                    "payload": {"mission_slug": "demo-pr-mission", "feature_dir": str(mission_dir)},
                    "stderr": "",
                },
            ), mock.patch.dict(os.environ, {}, clear=True):
                result = promote_discovery.promote(repo, execute=True)

            intake_json = json.loads((mission_dir / "consensus-rnd" / "intake.json").read_text(encoding="utf-8"))
            meta = json.loads((mission_dir / "meta.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "promoted")
        self.assertEqual(intake_json["source_kind"], "github_pr")
        self.assertEqual(intake_json["source_pr"], "123")
        self.assertEqual(meta["consensus_rnd_spec"]["source_url"], "https://github.com/example/repo/pull/123")


if __name__ == "__main__":
    unittest.main()
