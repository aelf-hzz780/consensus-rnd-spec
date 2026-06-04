from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
for name in ("backend_common", "github_sync", "loop_check", "spec_backend", "discovery", "promote_discovery", "native_capabilities"):
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
    def test_codex_worker_command_uses_current_unattended_cli_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            prompt = repo / "prompt.md"
            prompt.write_text("Run the worker.\n", encoding="utf-8")
            config = type("Config", (), {"codex_model": "", "codex_extra_args": ""})()

            command = run_loop.codex_command(repo, str(prompt), config)

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--ask-for-approval", command)

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

    def test_spec_kitty_strict_blocks_native_lifecycle_when_native_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\n'
                'export NATIVE_FULL_LOOP_ENABLE="true"\n'
                'export NATIVE_CONSENSUS_SKILL_ROOT="/native-skill"\n'
                'export KITTY_FLOW_ENFORCEMENT="strict"\n',
                encoding="utf-8",
            )
            kitty_plan = {
                "backend": "spec-kitty",
                "status": "ready",
                "action_command": ["spec-kitty", "agent", "action", "implement", "WP01"],
            }
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "run_loop.detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ), mock.patch("run_loop.count_inflight", return_value=5), mock.patch(
                "run_loop.native_plan",
                return_value={"backend": "native", "status": "ready", "entrypoint": "legacy-cli"},
            ), mock.patch("run_loop.plan_next", return_value=kitty_plan), mock.patch(
                "run_loop.execute_spec_kitty_action", return_value={"status": "kitty-action"}
            ), mock.patch("run_loop.run_native") as run_native:
                turn = run_loop.loop_turn(repo, execute=True)

        self.assertEqual(turn["backend"]["backend"], "spec-kitty")
        self.assertEqual(turn["native_companion"]["status"], "blocked")
        self.assertIn("native-implementation", turn["native_companion"]["forbidden_actions"])
        self.assertEqual(turn["dispatches"][0]["plan"]["status"], "ready")
        run_native.assert_not_called()

    def test_spec_kitty_execute_runs_one_state_machine_step_per_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "kitty-specs").mkdir()
            plan = {
                "backend": "spec-kitty",
                "status": "ready",
                "execution_kind": "kitty-next-step",
                "advance_command": ["spec-kitty", "next", "--agent", "codex", "--mission", "001-demo", "--json", "--result", "success"],
            }
            with mock.patch.dict("os.environ", {}, clear=True), mock.patch(
                "run_loop.detect_backend",
                return_value={"backend": "spec-kitty", "reason": "test", "repo_root": str(repo), "signals": {}},
            ), mock.patch("run_loop.count_inflight", return_value=0), mock.patch(
                "run_loop.plan_next", return_value=plan
            ) as plan_next, mock.patch(
                "run_loop.execute_spec_kitty_action", return_value={"status": "kitty-next-only"}
            ):
                turn = run_loop.loop_turn(repo, execute=True)

        self.assertEqual(turn["concurrency"]["missing"], 5)
        self.assertEqual(len(turn["dispatches"]), 1)
        plan_next.assert_called_once()

    def test_execute_kitty_next_step_runs_prompt_and_records_pending_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            prompt = repo / "kitty-prompt.md"
            prompt.write_text("Implement the Spec Kitty discovery step.\n", encoding="utf-8")
            plan = {
                "backend": "spec-kitty",
                "status": "ready",
                "execution_kind": "kitty-next-step",
                "chosen": {"mission": "001-demo"},
                "advance_command": ["spec-kitty", "next", "--agent", "codex", "--mission", "001-demo", "--json", "--result", "success"],
            }
            next_stdout = json.dumps({"mission_slug": "001-demo", "action": "research", "prompt_file": str(prompt)})

            def fake_run(command: list[str], _repo: Path) -> dict[str, object]:
                if command[:2] == ["spec-kitty", "next"]:
                    return {"command": command, "returncode": 0, "stdout_tail": next_stdout, "stderr_tail": ""}
                if command[:2] == ["codex", "exec"]:
                    return {"command": command, "returncode": 0, "stdout_tail": "ok", "stderr_tail": ""}
                raise AssertionError(command)

            config = type("Config", (), {"codex_model": "", "codex_extra_args": ""})()
            with mock.patch("run_loop.run_command", side_effect=fake_run):
                result = run_loop.execute_spec_kitty_action(repo, plan, config)

            pending_path = repo / ".consensus-rnd-spec" / "state" / "spec-kitty-pending-result.json"
            pending = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "worker-finished")
        self.assertEqual(result["execution_kind"], "kitty-next-step")
        self.assertEqual(pending["mission_slug"], "001-demo")
        self.assertEqual(pending["result"], "success")
        self.assertEqual(pending["completed_action"], "research")

    def test_execute_kitty_agent_action_runs_stdout_prompt_and_records_pending_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            plan = {
                "backend": "spec-kitty",
                "status": "ready",
                "execution_kind": "kitty-agent-action",
                "chosen": {"mission": "001-demo"},
                "action": "implement",
                "wp_id": "WP01",
                "action_command": ["spec-kitty", "agent", "action", "implement", "WP01", "--mission", "001-demo", "--agent", "codex"],
            }

            def fake_run(command: list[str], _repo: Path) -> dict[str, object]:
                if command[:4] == ["spec-kitty", "agent", "action", "implement"]:
                    return {"command": command, "returncode": 0, "stdout_tail": "Implement WP01\n", "stderr_tail": ""}
                if command[:2] == ["codex", "exec"]:
                    return {"command": command, "returncode": 0, "stdout_tail": "ok", "stderr_tail": ""}
                raise AssertionError(command)

            config = type("Config", (), {"codex_model": "", "codex_extra_args": ""})()
            with mock.patch("run_loop.run_command", side_effect=fake_run), mock.patch(
                "run_loop.sync_wp_status",
                side_effect=[
                    {"status": "synced", "issue": "10", "phase": "crnd:phase:implementing"},
                    {"status": "synced", "issue": "10", "phase": "crnd:phase:reviewing"},
                ],
            ) as sync, mock.patch("run_loop.open_or_update_mission_pr", return_value={"status": "ready", "mission_pr": {"number": "20"}}) as pr_sync:
                result = run_loop.execute_spec_kitty_action(repo, plan, config)

            pending_path = repo / ".consensus-rnd-spec" / "state" / "spec-kitty-pending-result.json"
            pending = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "worker-finished")
        self.assertEqual(result["execution_kind"], "kitty-agent-action")
        self.assertEqual(pending["mission_slug"], "001-demo")
        self.assertEqual(pending["completed_action"], "implement")
        self.assertEqual(sync.call_count, 2)
        pr_sync.assert_called_once_with(repo, "001-demo", execute=True)

    def test_execute_kitty_agent_action_blocks_worker_when_github_presync_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            plan = {
                "backend": "spec-kitty",
                "status": "ready",
                "execution_kind": "kitty-agent-action",
                "chosen": {"mission": "001-demo"},
                "action": "implement",
                "wp_id": "WP01",
                "action_command": ["spec-kitty", "agent", "action", "implement", "WP01", "--mission", "001-demo", "--agent", "codex"],
            }
            calls: list[list[str]] = []

            def fake_run(command: list[str], _repo: Path) -> dict[str, object]:
                calls.append(command)
                if command[:4] == ["spec-kitty", "agent", "action", "implement"]:
                    return {"command": command, "returncode": 0, "stdout_tail": "Implement WP01\n", "stderr_tail": ""}
                if command[:2] == ["codex", "exec"]:
                    raise AssertionError("worker should not be dispatched after GitHub sync failure")
                raise AssertionError(command)

            config = type("Config", (), {"codex_model": "", "codex_extra_args": ""})()
            with mock.patch("run_loop.run_command", side_effect=fake_run), mock.patch(
                "run_loop.sync_wp_status",
                return_value={"status": "blocked", "reason": "failed to sync WP issue status"},
            ), mock.patch("run_loop.open_or_update_mission_pr") as pr_sync:
                result = run_loop.execute_spec_kitty_action(repo, plan, config)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "GitHub status sync failed before worker dispatch")
        self.assertTrue(any(command[:4] == ["spec-kitty", "agent", "action", "implement"] for command in calls))
        self.assertFalse(any(command[:2] == ["codex", "exec"] for command in calls))
        pr_sync.assert_not_called()

    def test_execute_kitty_agent_action_does_not_sync_github_when_action_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            plan = {
                "backend": "spec-kitty",
                "status": "ready",
                "execution_kind": "kitty-agent-action",
                "chosen": {"mission": "001-demo"},
                "action": "implement",
                "wp_id": "WP01",
                "action_command": ["spec-kitty", "agent", "action", "implement", "WP01", "--mission", "001-demo", "--agent", "codex"],
            }

            def fake_run(command: list[str], _repo: Path) -> dict[str, object]:
                if command[:4] == ["spec-kitty", "agent", "action", "implement"]:
                    return {"command": command, "returncode": 1, "stdout_tail": "", "stderr_tail": "blocked"}
                raise AssertionError(command)

            config = type("Config", (), {"codex_model": "", "codex_extra_args": ""})()
            with mock.patch("run_loop.run_command", side_effect=fake_run), mock.patch("run_loop.sync_wp_status") as sync:
                result = run_loop.execute_spec_kitty_action(repo, plan, config)

        self.assertEqual(result["status"], "action-command-only")
        self.assertIsNone(result["github_before"])
        sync.assert_not_called()

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
