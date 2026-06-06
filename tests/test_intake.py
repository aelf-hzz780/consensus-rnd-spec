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
for name in ("backend_common", "github_sync", "loop_check", "spec_backend", "discovery", "promote_discovery", "native_capabilities", "run_loop"):
    if name in sys.modules:
        continue
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

    def test_parse_slash_intake_preserves_structured_lines_and_title_field(self) -> None:
        parsed = intake.parse_intake(
            "/loop 10min /consensus-rnd-spec Mission 16 follow-up.\n\n"
            "Title: 16.Cockpit Production Contract Closure and Durable Query Trust\n\n"
            "Scores:\n- Technical architecture: 6.5/10.\n"
        )

        self.assertIn("\n\nTitle:", parsed["instructions"])
        self.assertEqual(
            intake.title_from_intake(parsed),
            "16.Cockpit Production Contract Closure and Durable Query Trust",
        )

    def test_title_from_intake_preserves_mission_number_prefix(self) -> None:
        parsed = intake.parse_intake(
            "/loop 10min /codex-refactor-loop Mission 21: Cockpit Empty Dataset Trust, "
            "CI Retry Gate, and Evidence Closure"
        )

        self.assertEqual(
            intake.title_from_intake(parsed),
            "21.Cockpit Empty Dataset Trust, CI Retry Gate, and Evidence Closure",
        )

    def test_parse_slash_intake_normalizes_embedded_human_marker(self) -> None:
        parsed = intake.parse_intake("/loop 10min /codex-refactor-loop reload skills Human\uff1a fix bugs")

        self.assertEqual(parsed["synthetic_human"], "Human: reload skills Human: fix bugs")

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
        self.assertEqual(plan["seed"]["handoff"], "spec-kitty")

    def test_plan_intake_expands_markdown_prompt_file_for_cockpit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            prompt = repo / "AutoTwitter驾驶舱.md"
            prompt.write_text("# 多策略多账号 Twitter 驾驶舱方案\n\n新增 Cockpit 系统。\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                intake,
                "detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ):
                plan = intake.plan_intake(repo, f"/loop 10min /codex-refactor-loop {prompt}")

        self.assertEqual(plan["seed"]["title"], "多策略多账号 Twitter 驾驶舱方案")
        self.assertIn("# 多策略多账号 Twitter 驾驶舱方案", plan["seed"]["body"])
        self.assertEqual(plan["seed"]["metadata"]["prompt_file"]["path"], str(prompt.resolve()))
        self.assertEqual(plan["seed"]["metadata"]["branch_contract"]["primary"], "feature/cockpit")

    def test_plan_intake_creates_artifact_only_seed_for_native_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                intake,
                "detect_backend",
                return_value={"backend": "native", "reason": "test", "repo_root": str(repo), "signals": {}},
            ):
                plan = intake.plan_intake(repo, "/loop 10min /codex-refactor-loop reload skills")

        self.assertEqual(plan["backend"]["backend"], "native")
        self.assertEqual(plan["seed"]["source"], "synthetic_human_intake")
        self.assertEqual(plan["seed"]["handoff"], "artifact-only")

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

    def test_execute_preserves_github_issue_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                intake,
                "detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ):
                plan = intake.plan_intake(repo, "/loop 1min /consensus-rnd-spec fix issue 123")
                assert isinstance(plan["seed"], dict)
                plan["seed"].update(
                    {
                        "source_kind": "github_issue",
                        "source_issue": "123",
                        "source_url": "https://github.com/example/repo/issues/123",
                    }
                )
                seed = intake.execute_intake_seed(repo, plan)
                assert seed is not None
                payload = json.loads(Path(seed["artifact"]).read_text(encoding="utf-8"))

        self.assertEqual(payload["source_kind"], "github_issue")
        self.assertEqual(payload["source_issue"], "123")
        self.assertEqual(payload["source_url"], "https://github.com/example/repo/issues/123")

    def test_main_execute_run_promotes_explicit_seed_before_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            prompt = repo / "AutoTwitter驾驶舱.md"
            prompt.write_text("# Cockpit plan\n", encoding="utf-8")
            promoted: list[Path] = []

            def fake_promote(_repo: Path, *, artifact: Path | None = None, execute: bool = False):
                assert artifact is not None
                promoted.append(artifact)
                return {"status": "promoted", "artifact": str(artifact), "execute": execute}

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                intake,
                "detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ), mock.patch.object(intake, "promote_discovery", side_effect=fake_promote), mock.patch.object(
                intake,
                "run_loop",
                return_value={"turns": [], "turn_count": 0, "execute": True},
            ), mock.patch.object(intake, "print_json"):
                rc = intake.main(
                    [
                        "--repo",
                        str(repo),
                        "--text",
                        f"/loop 10min /codex-refactor-loop {prompt}",
                        "--execute",
                        "--run",
                        "--once",
                    ]
                )

        self.assertEqual(rc, 0)
        self.assertEqual(len(promoted), 1)
        self.assertTrue(promoted[0].name.startswith("discovery-"))


if __name__ == "__main__":
    unittest.main()
