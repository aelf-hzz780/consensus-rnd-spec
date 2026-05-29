from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
for name in ("backend_common", "loop_check", "spec_backend", "discovery", "promote_discovery"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)

RUN_SPEC = importlib.util.spec_from_file_location("run_loop", SCRIPT_DIR / "run_loop.py")
run_loop = importlib.util.module_from_spec(RUN_SPEC)
assert RUN_SPEC and RUN_SPEC.loader
sys.modules["run_loop"] = run_loop
RUN_SPEC.loader.exec_module(run_loop)


class RunLoopTests(unittest.TestCase):
    def test_loop_turn_writes_event_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            plan = {
                "backend": "spec-kitty",
                "status": "ready",
                "action_command": ["spec-kitty", "agent", "action", "implement", "WP01"],
            }
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "run_loop.detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ), mock.patch("run_loop.plan_next", return_value=plan), mock.patch("run_loop.count_inflight", return_value=5):
                turn = run_loop.loop_turn(repo, execute=False)

            event_path = repo / ".consensus-rnd-spec" / "state" / "loop-events.jsonl"
            event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(turn["backend"]["backend"], "spec-kitty")
        self.assertEqual(turn["dispatches"][0]["plan"]["status"], "ready")
        self.assertEqual(event["type"], "loop_turn")

    def test_native_execute_stays_blocked_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch("run_loop.count_inflight", return_value=0):
                turn = run_loop.loop_turn(repo, execute=True)

        self.assertEqual(turn["backend"]["backend"], "native")
        self.assertEqual(turn["dispatches"][0]["plan"]["status"], "blocked")

    def test_native_execute_fills_missing_floor_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\n'
                'export CODEX_FLOOR="4"\n'
                'export NATIVE_FULL_LOOP_ENABLE="true"\n'
                'export NATIVE_CONSENSUS_SKILL_ROOT="/native-skill"\n',
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "run_loop.detect_backend",
                return_value={"backend": "native", "reason": "test", "repo_root": str(repo), "signals": {}},
            ), mock.patch("run_loop.count_inflight", return_value=1), mock.patch(
                "run_loop.native_plan", return_value={"backend": "native", "status": "ready"}
            ), mock.patch("run_loop.run_native", return_value={"status": "spawned"}):
                turn = run_loop.loop_turn(repo, execute=True)

        self.assertEqual(turn["backend"]["backend"], "native")
        self.assertEqual(turn["concurrency"]["missing"], 3)
        self.assertEqual(len(turn["dispatches"]), 3)
        self.assertEqual(turn["dispatches"][0]["execution"]["status"], "spawned")

    def test_execute_discovery_needed_writes_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            plan = {"backend": "spec-kitty", "status": "discovery_needed"}
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "run_loop.detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ), mock.patch("backend_common.spec_kitty_callable", return_value=True), mock.patch(
                "run_loop.plan_next", return_value=plan
            ), mock.patch("run_loop.count_inflight", return_value=5), mock.patch(
                "discovery.rg_findings", return_value=[]
            ), mock.patch(
                "discovery.large_python_files", return_value=[]
            ):
                turn = run_loop.loop_turn(repo, execute=True)

            artifact = Path(turn["dispatches"][0]["execution"]["discovery"]["artifact"])

        self.assertTrue(artifact.name.startswith("discovery-"))

    def test_dry_run_discovery_needed_prefers_existing_promotion_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            runs = repo / ".consensus-rnd-spec" / "runs"
            runs.mkdir(parents=True)
            (runs / "discovery-20260101T000000Z.json").write_text(
                '{"evidence_hash":"abc","finding_count":0,"findings":[]}\n',
                encoding="utf-8",
            )
            plan = {"backend": "spec-kitty", "status": "discovery_needed"}
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "run_loop.detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ), mock.patch("backend_common.spec_kitty_callable", return_value=True), mock.patch(
                "run_loop.plan_next", return_value=plan
            ), mock.patch("run_loop.count_inflight", return_value=0):
                turn = run_loop.loop_turn(repo, execute=False)

        self.assertEqual(turn["dispatches"][0]["plan"]["promotion"]["status"], "planned")


if __name__ == "__main__":
    unittest.main()
