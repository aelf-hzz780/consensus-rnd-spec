from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
COMMON_SPEC = importlib.util.spec_from_file_location("backend_common", SCRIPT_DIR / "backend_common.py")
backend_common = importlib.util.module_from_spec(COMMON_SPEC)
assert COMMON_SPEC and COMMON_SPEC.loader
sys.modules["backend_common"] = backend_common
COMMON_SPEC.loader.exec_module(backend_common)
sys.modules["backend_common"] = backend_common

SPEC = importlib.util.spec_from_file_location("spec_backend", SCRIPT_DIR / "spec_backend.py")
spec_backend = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(spec_backend)


class SpecBackendTests(unittest.TestCase):
    def test_plan_handoff_records_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = spec_backend.plan_handoff(
                Path(tmp),
                "fix stale review state",
                source_issue="123",
                audit_artifact=".consensus-rnd-spec/runs/audit.md",
            )

        self.assertEqual(plan["backend"], "spec-kitty")
        self.assertEqual(plan["seed"]["source"], "consensus-rnd-spec")
        self.assertEqual(plan["seed"]["source_issue"], "123")
        self.assertEqual(plan["seed"]["mission_type"], "software-dev")
        self.assertEqual(plan["next_commands"][0][:2], ["spec-kitty", "specify"])

    def test_action_command_for_implement_decision(self) -> None:
        decision = {
            "payload": {
                "action": "implement",
                "mission_slug": "demo-mission",
                "wp_id": "WP01",
            }
        }

        command = spec_backend.action_command(decision, "codex")

        self.assertEqual(
            command,
            ["spec-kitty", "agent", "action", "implement", "WP01", "--mission", "demo-mission", "--agent", "codex"],
        )

    def test_score_zero_mission_is_not_actionable(self) -> None:
        state = {
            "success": True,
            "data": {
                "summary": {
                    "planned": 0,
                    "for_review": 0,
                    "in_progress": 0,
                    "claimed": 0,
                    "done": 3,
                }
            },
        }

        self.assertFalse(spec_backend.mission_has_actionable_work(state))

    def test_local_lane_summary_scores_planned_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mission = Path(tmp)
            tasks = mission / "tasks"
            tasks.mkdir()
            (tasks / "WP01-demo.md").write_text("---\nwork_package_id: WP01\n---\n", encoding="utf-8")

            summary = spec_backend.local_lane_summary(mission)

        self.assertEqual(summary["planned"], 1)
        self.assertEqual(spec_backend.local_actionable_score(summary), 80)

    def test_local_lane_summary_uses_latest_status_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mission = Path(tmp)
            (mission / "status.events.jsonl").write_text(
                '{"wp_id":"WP01","to_lane":"planned"}\n{"wp_id":"WP01","to_lane":"done"}\n',
                encoding="utf-8",
            )

            summary = spec_backend.local_lane_summary(mission)

        self.assertEqual(summary["done"], 1)
        self.assertEqual(spec_backend.local_actionable_score(summary), 0)


if __name__ == "__main__":
    unittest.main()
