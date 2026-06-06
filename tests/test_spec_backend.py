from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
COMMON_SPEC = importlib.util.spec_from_file_location("backend_common", SCRIPT_DIR / "backend_common.py")
backend_common = importlib.util.module_from_spec(COMMON_SPEC)
assert COMMON_SPEC and COMMON_SPEC.loader
sys.modules["backend_common"] = backend_common
COMMON_SPEC.loader.exec_module(backend_common)
sys.modules["backend_common"] = backend_common

GITHUB_SPEC = importlib.util.spec_from_file_location("github_sync", SCRIPT_DIR / "github_sync.py")
github_sync = importlib.util.module_from_spec(GITHUB_SPEC)
assert GITHUB_SPEC and GITHUB_SPEC.loader
sys.modules["github_sync"] = github_sync
GITHUB_SPEC.loader.exec_module(github_sync)

SPEC = importlib.util.spec_from_file_location("spec_backend", SCRIPT_DIR / "spec_backend.py")
spec_backend = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["spec_backend"] = spec_backend
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

    def test_choose_mission_keeps_pre_wp_mission_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = repo / "kitty-specs" / "001-demo"
            mission.mkdir(parents=True)
            (mission / "meta.json").write_text('{"slug":"001-demo"}\n', encoding="utf-8")
            state = {
                "command": ["spec-kitty", "orchestrator-api", "mission-state", "--mission", "001-demo"],
                "returncode": 0,
                "payload": {
                    "success": True,
                    "data": {
                        "summary": {
                            "planned": 0,
                            "claimed": 0,
                            "in_progress": 0,
                            "for_review": 0,
                            "in_review": 0,
                            "approved": 0,
                            "done": 0,
                            "blocked": 0,
                            "canceled": 0,
                        },
                        "work_packages": [],
                    },
                },
                "stderr": "",
            }
            with mock.patch.object(spec_backend, "mission_state", return_value=state):
                chosen = spec_backend.choose_mission(repo)

        self.assertEqual(chosen["mission"], "001-demo")
        self.assertEqual(chosen["source"], "scan-pre-wp")

    def test_plan_next_starts_preview_step_for_new_mission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport SPEC_KITTY_AGENT="codex"\n',
                encoding="utf-8",
            )
            chosen = {"mission": "001-demo", "source": "scan-pre-wp"}
            decision = {
                "command": ["spec-kitty", "next", "--agent", "codex", "--mission", "001-demo", "--json"],
                "returncode": 0,
                "payload": {
                    "kind": "query",
                    "mission_slug": "001-demo",
                    "action": None,
                    "is_query": True,
                    "preview_step": "discovery",
                },
                "stderr": "",
            }
            with mock.patch.object(spec_backend, "choose_mission", return_value=chosen), mock.patch.object(
                spec_backend, "next_decision", return_value=decision
            ), mock.patch.object(spec_backend, "ensure_child_issues", return_value={"status": "planned"}):
                plan = spec_backend.plan_next(repo)

        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["execution_kind"], "kitty-next-step")
        self.assertEqual(plan["advance_command"], ["spec-kitty", "next", "--agent", "codex", "--mission", "001-demo", "--json", "--result", "success"])

    def test_plan_next_prefers_pending_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".consensus-rnd-spec" / "state").mkdir(parents=True)
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport SPEC_KITTY_AGENT="codex"\n',
                encoding="utf-8",
            )
            spec_backend.write_pending_result(
                repo,
                {
                    "mission_slug": "001-demo",
                    "result": "success",
                    "completed_action": "research",
                },
            )

            plan = spec_backend.plan_next(repo)

        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["execution_kind"], "kitty-next-step")
        self.assertEqual(plan["reason"], "advance_after_worker_result")
        self.assertEqual(plan["advance_command"], ["spec-kitty", "next", "--agent", "codex", "--mission", "001-demo", "--json", "--result", "success"])

    def test_wp_action_from_chosen_uses_kitty_agent_action(self) -> None:
        chosen = {
            "mission": "001-demo",
            "state": {
                "payload": {
                    "success": True,
                    "data": {
                        "work_packages": [
                            {"wp_id": "WP01", "lane": "done"},
                            {"wp_id": "WP02", "lane": "for_review"},
                        ]
                    },
                }
            },
        }

        plan = spec_backend.wp_action_from_chosen(chosen, "001-demo", "codex")

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan["execution_kind"], "kitty-agent-action")
        self.assertEqual(plan["action"], "review")
        self.assertEqual(plan["action_command"], ["spec-kitty", "agent", "action", "review", "WP02", "--mission", "001-demo", "--agent", "codex"])

    def test_wp_action_from_chosen_resumes_in_progress_before_planned_dependency(self) -> None:
        chosen = {
            "mission": "001-demo",
            "state": {
                "payload": {
                    "success": True,
                    "data": {
                        "work_packages": [
                            {"wp_id": "WP01", "lane": "in_progress", "dependencies": []},
                            {"wp_id": "WP02", "lane": "planned", "dependencies": ["WP01"]},
                        ]
                    },
                }
            },
        }

        plan = spec_backend.wp_action_from_chosen(chosen, "001-demo", "codex")

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan["action"], "implement")
        self.assertEqual(plan["wp_id"], "WP01")

    def test_wp_action_from_chosen_skips_planned_wp_with_unmet_dependencies(self) -> None:
        chosen = {
            "mission": "001-demo",
            "state": {
                "payload": {
                    "success": True,
                    "data": {
                        "work_packages": [
                            {"wp_id": "WP01", "lane": "in_progress", "dependencies": []},
                            {"wp_id": "WP02", "lane": "planned", "dependencies": ["WP01"]},
                        ]
                    },
                }
            },
        }

        packages = spec_backend.state_work_packages(chosen["state"])
        lanes = spec_backend.state_lane_items(chosen["state"])
        packages = [package for package in packages if package["lane"] == "planned"]
        target = spec_backend.first_actionable_wp(packages, lanes)

        self.assertIsNone(target)

    def test_plan_next_adds_github_child_issue_plan_for_wp_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport SPEC_KITTY_AGENT="codex"\n',
                encoding="utf-8",
            )
            chosen = {
                "mission": "001-demo",
                "state": {
                    "payload": {
                        "success": True,
                        "data": {
                            "summary": {"planned": 1},
                            "work_packages": [{"wp_id": "WP01", "lane": "planned"}],
                        },
                    }
                },
            }
            decision = {
                "returncode": 0,
                "payload": {"success": True, "data": {}, "reason": "no next step"},
                "stderr": "",
            }
            with mock.patch.object(spec_backend, "choose_mission", return_value=chosen), mock.patch.object(
                spec_backend, "next_decision", return_value=decision
            ), mock.patch.object(spec_backend, "ensure_child_issues", return_value={"status": "planned"}) as ensure:
                plan = spec_backend.plan_next(repo)

        self.assertEqual(plan["execution_kind"], "kitty-agent-action")
        self.assertEqual(plan["github_sync"]["status"], "planned")
        ensure.assert_called_once_with(repo.resolve(), "001-demo", execute=False)

    def test_audit_wp_owned_files_blocks_runtime_cors_config_without_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tasks = repo / "kitty-specs" / "001-demo" / "tasks"
            tasks.mkdir(parents=True)
            (tasks / "WP02-cors.md").write_text(
                """---
work_package_id: WP02
owned_files:
- apps/cockpit-api/src/app.ts
- apps/cockpit-api/src/server.ts
---

# WP02

Implement production restricted CORS config validation for COCKPIT_CORS_ALLOWED_ORIGINS.
""",
                encoding="utf-8",
            )

            audit = spec_backend.audit_wp_owned_files(repo, "001-demo", "WP02")

        self.assertEqual(audit["status"], "blocked")
        self.assertEqual(audit["checks"][0]["rule"], "cockpit-runtime-cors-config-ownership")
        self.assertEqual(
            audit["checks"][0]["missing_owned_files"],
            [
                "apps/cockpit-api/src/runtime-config.ts",
                "apps/cockpit-api/tests/cockpit-runtime-entry.test.ts",
            ],
        )

    def test_plan_next_blocks_wp_action_when_owned_files_preflight_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport SPEC_KITTY_AGENT="codex"\n',
                encoding="utf-8",
            )
            tasks = repo / "kitty-specs" / "001-demo" / "tasks"
            tasks.mkdir(parents=True)
            (tasks / "WP02-cors.md").write_text(
                """---
work_package_id: WP02
owned_files:
- apps/cockpit-api/src/app.ts
---

# WP02

Add production CORS runtime config validation.
""",
                encoding="utf-8",
            )
            chosen = {
                "mission": "001-demo",
                "state": {
                    "payload": {
                        "success": True,
                        "data": {
                            "summary": {"planned": 1},
                            "work_packages": [{"wp_id": "WP02", "lane": "planned"}],
                        },
                    }
                },
            }
            decision = {
                "returncode": 0,
                "payload": {"success": True, "data": {}, "reason": "no next step"},
                "stderr": "",
            }
            with mock.patch.object(spec_backend, "choose_mission", return_value=chosen), mock.patch.object(
                spec_backend, "next_decision", return_value=decision
            ), mock.patch.object(spec_backend, "ensure_child_issues", return_value={"status": "planned"}):
                plan = spec_backend.plan_next(repo)

        self.assertEqual(plan["status"], "blocked")
        self.assertIsNone(plan["action_command"])
        self.assertEqual(plan["preflight"]["wp_id"], "WP02")

    def test_approved_code_lane_plan_excludes_planning_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = repo / "kitty-specs" / "001-demo"
            mission.mkdir(parents=True)
            (mission / "meta.json").write_text('{"slug":"001-demo","target_branch":"feature/demo"}\n', encoding="utf-8")
            (mission / "lanes.json").write_text(
                """
{
  "mission_slug": "001-demo",
  "mission_branch": "kitty/mission-001-demo",
  "target_branch": "feature/demo",
  "lanes": [
    {"lane_id": "lane-a", "wp_ids": ["WP01"]},
    {"lane_id": "lane-planning", "wp_ids": ["WP02"]}
  ]
}
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (mission / "status.events.jsonl").write_text(
                '{"wp_id":"WP01","to_lane":"approved"}\n{"wp_id":"WP02","to_lane":"approved"}\n',
                encoding="utf-8",
            )

            plan = spec_backend.approved_code_lane_plan(repo, "001-demo")

        self.assertEqual(plan["status"], "planned")
        self.assertEqual([lane["lane_id"] for lane in plan["lanes"]], ["lane-a"])
        self.assertEqual(plan["blockers"][0]["lane_id"], "lane-planning")

    def test_approved_code_lane_plan_blocks_unapproved_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = repo / "kitty-specs" / "001-demo"
            mission.mkdir(parents=True)
            (mission / "meta.json").write_text('{"slug":"001-demo","target_branch":"feature/demo"}\n', encoding="utf-8")
            (mission / "lanes.json").write_text(
                '{"mission_branch":"kitty/mission-001-demo","target_branch":"feature/demo","lanes":[{"lane_id":"lane-a","wp_ids":["WP01","WP02"]}]}\n',
                encoding="utf-8",
            )
            (mission / "status.events.jsonl").write_text(
                '{"wp_id":"WP01","to_lane":"approved"}\n{"wp_id":"WP02","to_lane":"in_progress"}\n',
                encoding="utf-8",
            )

            plan = spec_backend.approved_code_lane_plan(repo, "001-demo")

        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["blockers"][0]["missing"], ["WP02"])
        self.assertEqual(plan["blockers"][0]["lanes"]["WP02"], "in_progress")

    def test_integrate_approved_lanes_dry_run_does_not_call_merge_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = repo / "kitty-specs" / "001-demo"
            mission.mkdir(parents=True)
            (mission / "meta.json").write_text('{"slug":"001-demo","target_branch":"feature/demo"}\n', encoding="utf-8")
            (mission / "lanes.json").write_text(
                '{"mission_branch":"kitty/mission-001-demo","target_branch":"feature/demo","lanes":[{"lane_id":"lane-a","wp_ids":["WP01"]}]}\n',
                encoding="utf-8",
            )
            (mission / "status.events.jsonl").write_text('{"wp_id":"WP01","to_lane":"approved"}\n', encoding="utf-8")
            with mock.patch.object(spec_backend.subprocess, "run") as run:
                result = spec_backend.integrate_approved_lanes(repo, "001-demo", execute=False)

        self.assertEqual(result["status"], "planned")
        run.assert_not_called()

    def test_integrate_approved_lanes_blocks_dirty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = repo / "kitty-specs" / "001-demo"
            mission.mkdir(parents=True)
            (mission / "meta.json").write_text('{"slug":"001-demo","target_branch":"feature/demo"}\n', encoding="utf-8")
            (mission / "lanes.json").write_text(
                '{"mission_branch":"kitty/mission-001-demo","target_branch":"feature/demo","lanes":[{"lane_id":"lane-a","wp_ids":["WP01"]}]}\n',
                encoding="utf-8",
            )
            (mission / "status.events.jsonl").write_text('{"wp_id":"WP01","to_lane":"approved"}\n', encoding="utf-8")

            def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
                if command == ["git", "status", "--porcelain"]:
                    return mock.Mock(returncode=0, stdout=" M src/app.ts\n?? .kittify/derived/demo/status.json\n", stderr="")
                raise AssertionError(command)

            with mock.patch.object(spec_backend.subprocess, "run", side_effect=fake_run):
                result = spec_backend.integrate_approved_lanes(repo, "001-demo", execute=True)

        self.assertEqual(result["status"], "blocked")
        self.assertIn(" M src/app.ts", result["dirty"])
        self.assertNotIn("?? .kittify/derived/demo/status.json", result["dirty"])

    def test_ensure_spec_kitty_import_path_uses_cli_shebang(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "tool" / "bin"
            package_dir = root / "tool" / "lib" / "python3.11" / "site-packages" / "specify_cli"
            bin_dir.mkdir(parents=True)
            package_dir.mkdir(parents=True)
            fake_python = bin_dir / "python3"
            fake_python.write_text("", encoding="utf-8")
            spec_kitty = bin_dir / "spec-kitty"
            spec_kitty.write_text(f"#!{fake_python}\n", encoding="utf-8")
            spec_kitty.chmod(0o755)
            (package_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            old_path = list(sys.path)
            sys.modules.pop("specify_cli", None)
            with mock.patch.object(spec_backend.shutil, "which", return_value=str(spec_kitty)):
                ok = spec_backend.ensure_spec_kitty_import_path()

            self.assertTrue(ok)
            self.assertIn(str(package_dir.parent), sys.path)
            sys.modules.pop("specify_cli", None)
            sys.path[:] = old_path


if __name__ == "__main__":
    unittest.main()
