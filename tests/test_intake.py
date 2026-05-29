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
for name in ("backend_common", "loop_check", "spec_backend", "discovery", "promote_discovery", "run_loop"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)

SPEC = importlib.util.spec_from_file_location("intake", SCRIPT_DIR / "intake.py")
intake = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["intake"] = intake
SPEC.loader.exec_module(intake)


class IntakeTests(unittest.TestCase):
    def test_parse_slash_intake_preserves_goal_as_synthetic_human(self) -> None:
        parsed = intake.parse_intake("/loop 10min /codex-refactor-loop fix bugs and reload skills")

        self.assertEqual(parsed["duration"], "10min")
        self.assertEqual(parsed["surfaces"], ["codex-refactor-loop"])
        self.assertEqual(parsed["instructions"], "fix bugs and reload skills")
        self.assertEqual(parsed["synthetic_human"], "Human: fix bugs and reload skills")

    def test_parse_slash_intake_normalizes_embedded_human_marker(self) -> None:
        parsed = intake.parse_intake("/loop 10min /codex-refactor-loop reload skills Human\uff1a fix bugs")

        self.assertEqual(parsed["synthetic_human"], "Human: fix bugs")

    def test_plan_intake_creates_spec_kitty_seed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                intake,
                "detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ):
                plan = intake.plan_intake(repo, "/loop 10min /codex-refactor-loop improve loop")

        self.assertEqual(plan["backend"]["backend"], "spec-kitty")
        self.assertEqual(plan["duration_seconds"], 600)
        self.assertEqual(plan["seed"]["source"], "synthetic_human_intake")

    def test_execute_writes_discovery_seed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                intake,
                "detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ):
                plan = intake.plan_intake(repo, "/loop 1min /consensus-rnd-spec Human\uff1a inspect stale work")
                seed = intake.execute_intake_seed(repo, plan)
                assert seed is not None
                artifact = Path(seed["artifact"])
                payload = json.loads(artifact.read_text(encoding="utf-8"))

        self.assertTrue(payload["synthetic_human_intake"])
        self.assertIn("Human: inspect stale work", payload["body"])


if __name__ == "__main__":
    unittest.main()
