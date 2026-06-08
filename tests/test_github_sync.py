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
COMMON_SPEC = importlib.util.spec_from_file_location("backend_common", SCRIPT_DIR / "backend_common.py")
if "backend_common" in sys.modules:
    backend_common = sys.modules["backend_common"]
else:
    backend_common = importlib.util.module_from_spec(COMMON_SPEC)
    assert COMMON_SPEC and COMMON_SPEC.loader
    sys.modules["backend_common"] = backend_common
    COMMON_SPEC.loader.exec_module(backend_common)

SPEC = importlib.util.spec_from_file_location("github_sync", SCRIPT_DIR / "github_sync.py")
if "github_sync" in sys.modules:
    github_sync = sys.modules["github_sync"]
else:
    github_sync = importlib.util.module_from_spec(SPEC)
    assert SPEC and SPEC.loader
    sys.modules["github_sync"] = github_sync
    SPEC.loader.exec_module(github_sync)


def subprocess_completed(command: list[str], returncode: int, stdout: str, stderr: str):
    return github_sync.subprocess.CompletedProcess(command, returncode, stdout, stderr)


def make_mission(repo: Path, mission: str = "001-demo", *, source_issue: str = "") -> Path:
    mission_dir = repo / "kitty-specs" / mission
    (mission_dir / "consensus-rnd").mkdir(parents=True)
    (mission_dir / "tasks").mkdir()
    (mission_dir / "meta.json").write_text(
        json.dumps({"slug": mission, "target_branch": "main"}) + "\n",
        encoding="utf-8",
    )
    (mission_dir / "consensus-rnd" / "intake.json").write_text(
        json.dumps({"title": "Demo mission", "source_issue": source_issue, "source_kind": "synthetic_human_intake"}) + "\n",
        encoding="utf-8",
    )
    return mission_dir


def write_wp_status(mission_dir: Path, lanes: dict[str, str]) -> None:
    mission_slug = mission_dir.name
    payload = {
        "mission_slug": mission_slug,
        "work_packages": {
            wp_id: {"lane": lane, "actor": "test"}
            for wp_id, lane in lanes.items()
        },
        "summary": {
            lane: list(lanes.values()).count(lane)
            for lane in sorted(set(lanes.values()))
        },
    }
    (mission_dir / "status.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")


class GitHubSyncTests(unittest.TestCase):
    def test_run_command_retries_transient_gh_eof(self) -> None:
        calls = 0

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 1:
                return subprocess_completed(["gh", "label", "edit"], 1, "", "Patch url: EOF")
            return subprocess_completed(["gh", "label", "edit"], 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(github_sync.subprocess, "run", side_effect=fake_run), mock.patch.object(
            github_sync.time, "sleep"
        ):
            result = github_sync.run_command(["gh", "label", "edit"], Path(tmp))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, 2)

    def test_status_banner_shape_and_sentinel(self) -> None:
        body = github_sync.build_status_banner(
            phase=github_sync.PHASE_IMPLEMENTING,
            mission="001-demo",
            wp_id="WP01",
            issue="12",
            detail="dispatch",
        )

        self.assertTrue(body.startswith("## 📊 当前状态 — crnd:phase:implementing("))
        self.assertIn("🤖 controller status banner", body)
        self.assertTrue(body.rstrip().endswith("⟦AI:AUTO-LOOP⟧"))

    def test_pr_body_has_exactly_one_parent_closes_link(self) -> None:
        body = github_sync.build_mission_pr_body("001-demo", "9", {"WP01": {"number": "10"}, "WP02": {"number": "11"}})

        self.assertEqual(len(github_sync.PR_CLOSES_RE.findall(body)), 0)
        self.assertIn("Related: #9", body)
        self.assertIn("Closure Guard", body)
        self.assertIn("`WP01`: #10", body)
        self.assertTrue(body.rstrip().endswith("⟦AI:AUTO-LOOP⟧"))

    def test_target_branch_prefers_final_landing_branch_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo)
            (mission_dir / "meta.json").write_text(
                json.dumps({"slug": "001-demo", "target_branch": "codex/mission-001-demo"}) + "\n",
                encoding="utf-8",
            )
            (mission_dir / "plan.md").write_text(
                "- GitHub mission PR base and final landing branch: `feature/cockpit`\n",
                encoding="utf-8",
            )

            branch = github_sync.target_branch_for_mission(mission_dir)

        self.assertEqual(branch, "feature/cockpit")

    def test_open_pr_uses_meta_target_branch_as_head_when_base_is_final_landing_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = "24-cockpit-viewport-level-mobile-regression-gate-hardening-01KTFCZ2"
            mission_dir = make_mission(repo, mission=mission, source_issue="42")
            (mission_dir / "meta.json").write_text(
                json.dumps({"slug": mission, "target_branch": "codex/24-cockpit-viewport-regression-gate"}) + "\n",
                encoding="utf-8",
            )
            (mission_dir / "spec.md").write_text("- Base branch: `feature/cockpit`\n", encoding="utf-8")
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {},
                    "mission_pr": {},
                },
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                github_sync,
                "branch_or_origin_ref_has_commits",
                side_effect=lambda _repo, branch, base: branch == "codex/24-cockpit-viewport-regression-gate" and base == "feature/cockpit",
            ), mock.patch.object(github_sync, "github_write_preflight", return_value={"status": "ready", "repo_slug": "owner/repo"}), mock.patch.object(
                github_sync, "run_command"
            ) as run_command:
                result = github_sync.open_or_update_mission_pr(repo, mission, execute=False)

        self.assertEqual(result["status"], "planned")
        create_command = result["create_command"]
        self.assertEqual(create_command[create_command.index("--base") + 1], "feature/cockpit")
        self.assertEqual(create_command[create_command.index("--head") + 1], "codex/24-cockpit-viewport-regression-gate")
        run_command.assert_not_called()

    def test_config_defaults_github_sync_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with mock.patch.dict(os.environ, {}, clear=True):
                config = backend_common.load_config(repo)

        self.assertTrue(config.github_sync_enable)
        self.assertEqual(config.gh_repo_slug, "")

    def test_dry_run_parent_issue_plans_without_gh_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            make_mission(repo)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync, "run_command") as run_command:
                result = github_sync.ensure_parent_issue(repo, "001-demo", execute=False)

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["command"][:3], ["gh", "issue", "create"])
        run_command.assert_not_called()

    def test_execute_parent_issue_creates_binding_with_mocked_gh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )

            def fake_run(command: list[str], _repo: Path) -> github_sync.CommandResult:
                if command[:3] == ["gh", "auth", "status"]:
                    return github_sync.CommandResult(command, 0, "", "")
                if command[:3] == ["gh", "label", "list"]:
                    return github_sync.CommandResult(command, 0, "[]", "")
                if command[:3] == ["gh", "label", "create"]:
                    return github_sync.CommandResult(command, 0, "", "")
                if command[:3] == ["gh", "issue", "create"]:
                    return github_sync.CommandResult(command, 0, "https://github.com/owner/repo/issues/42\n", "")
                raise AssertionError(command)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
                github_sync, "run_command", side_effect=fake_run
            ):
                result = github_sync.ensure_parent_issue(repo, "001-demo", execute=True)

            bindings = json.loads((mission_dir / "consensus-rnd" / "github-bindings.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "created")
        self.assertEqual(bindings["parent_issue"]["number"], "42")

    def test_parent_issue_does_not_reuse_source_issue_bound_as_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            existing = make_mission(repo, "020-existing")
            github_sync.write_bindings(
                existing,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "9"},
                    "child_issues": {"WP04": {"number": "10"}},
                    "mission_pr": {},
                },
            )
            make_mission(repo, "021-next", source_issue="10")
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync, "run_command") as run_command:
                result = github_sync.ensure_parent_issue(repo, "021-next", execute=False)
                source_issue_is_child = github_sync.is_issue_bound_as_child(repo, "10")

        self.assertTrue(source_issue_is_child)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["command"][:3], ["gh", "issue", "create"])
        run_command.assert_not_called()

    def test_label_catalog_dry_run_plans_without_gh_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync, "run_command") as run_command:
                config = backend_common.load_config(repo)
                result = github_sync.ensure_label_catalog(repo, config, execute=False)

        self.assertEqual(result["status"], "planned")
        self.assertEqual(len(result["commands"]), len(github_sync.LABEL_SPECS))
        self.assertIn("crnd:lifecycle:managed", {command[3] for command in result["commands"]})
        run_command.assert_not_called()

    def test_label_catalog_execute_creates_missing_and_updates_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], _repo: Path) -> github_sync.CommandResult:
                calls.append(command)
                if command[:3] == ["gh", "auth", "status"]:
                    return github_sync.CommandResult(command, 0, "", "")
                if command[:3] == ["gh", "label", "list"]:
                    existing = [{"name": github_sync.MANAGED}, {"name": github_sync.PHASE_IMPLEMENTING}]
                    return github_sync.CommandResult(command, 0, json.dumps(existing), "")
                if command[:3] in (["gh", "label", "create"], ["gh", "label", "edit"]):
                    return github_sync.CommandResult(command, 0, "", "")
                raise AssertionError(command)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync.shutil, "which", return_value="/usr/bin/gh"), mock.patch.object(
                github_sync, "run_command", side_effect=fake_run
            ):
                config = backend_common.load_config(repo)
                result = github_sync.ensure_label_catalog(repo, config, execute=True)

        self.assertEqual(result["status"], "ready")
        self.assertIn(github_sync.MANAGED, result["updated"])
        self.assertIn(github_sync.PHASE_IMPLEMENTING, result["updated"])
        self.assertIn(github_sync.PHASE_CONSENSUS_REACHED, result["created"])
        self.assertIn(github_sync.MANAGED, {command[3] for command in calls if command[:3] == ["gh", "label", "edit"]})
        self.assertIn(github_sync.PHASE_CONSENSUS_REACHED, {command[3] for command in calls if command[:3] == ["gh", "label", "create"]})

    def test_ensure_child_issues_plans_one_issue_per_wp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            make_mission(repo, source_issue="9")
            (repo / "kitty-specs" / "001-demo" / "tasks" / "WP01-demo.md").write_text("---\nwork_package_id: WP01\n---\n", encoding="utf-8")
            (repo / "kitty-specs" / "001-demo" / "tasks" / "WP02-demo.md").write_text("---\nwork_package_id: WP02\n---\n", encoding="utf-8")
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync, "run_command") as run_command:
                result = github_sync.ensure_child_issues(repo, "001-demo", execute=False)

        self.assertEqual(result["status"], "planned")
        self.assertEqual(len(result["planned"]), 2)
        self.assertIn("WP01", {item["wp_id"] for item in result["planned"]})
        run_command.assert_not_called()

    def test_sync_wp_status_fail_closed_when_gh_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo, source_issue="9")
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "9"},
                    "child_issues": {"WP01": {"number": "10"}},
                    "mission_pr": {},
                },
            )
            (mission_dir / "status.json").write_text(
                json.dumps({"work_packages": {"WP01": {"lane": "in_progress"}}}) + "\n",
                encoding="utf-8",
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync.shutil, "which", return_value=None):
                result = github_sync.sync_wp_status(repo, "001-demo", "WP01", github_sync.PHASE_IMPLEMENTING, execute=True)

        self.assertEqual(result["status"], "blocked")
        self.assertIn("gh CLI not found", result["reason"])

    def test_cli_returns_nonzero_for_blocked_result(self) -> None:
        with mock.patch.object(github_sync, "sync_wp_status", return_value={"status": "blocked", "reason": "gh auth status failed"}), mock.patch.object(
            github_sync, "print_json"
        ):
            rc = github_sync.main(
                [
                    "sync-wp-status",
                    "--repo",
                    ".",
                    "--mission",
                    "001-demo",
                    "--wp-id",
                    "WP01",
                    "--phase",
                    github_sync.PHASE_REVIEWING,
                    "--execute",
                ]
            )

        self.assertEqual(rc, 1)

    def test_cli_returns_zero_for_planned_result(self) -> None:
        with mock.patch.object(github_sync, "sync_wp_status", return_value={"status": "planned"}), mock.patch.object(github_sync, "print_json"):
            rc = github_sync.main(
                [
                    "sync-wp-status",
                    "--repo",
                    ".",
                    "--mission",
                    "001-demo",
                    "--wp-id",
                    "WP01",
                    "--phase",
                    github_sync.PHASE_REVIEWING,
                ]
            )

        self.assertEqual(rc, 0)

    def test_open_or_update_mission_pr_reuses_existing_pr_and_refreshes_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo, source_issue="42")
            write_wp_status(mission_dir, {"WP01": "done", "WP02": "done"})
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {"WP01": {"number": "43"}, "WP02": {"number": "44"}},
                    "mission_pr": {},
                },
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], _repo: Path) -> github_sync.CommandResult:
                calls.append(command)
                if command[:3] == ["gh", "pr", "list"]:
                    payload = [{"number": 7, "url": "https://github.com/owner/repo/pull/7"}]
                    return github_sync.CommandResult(command, 0, json.dumps(payload), "")
                if command[:3] == ["gh", "pr", "edit"]:
                    return github_sync.CommandResult(command, 0, "", "")
                if command[:3] == ["gh", "issue", "edit"]:
                    return github_sync.CommandResult(command, 0, "", "")
                if command[:3] == ["gh", "issue", "comment"]:
                    return github_sync.CommandResult(command, 0, "", "")
                raise AssertionError(command)

            preflight = {"status": "ready", "repo_slug": "owner/repo"}
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync, "resolve_mission_branch", return_value="kitty/001-demo"), mock.patch.object(
                github_sync, "github_write_preflight", return_value=preflight
            ), mock.patch.object(github_sync, "run_command", side_effect=fake_run):
                result = github_sync.open_or_update_mission_pr(repo, "001-demo", execute=True)

            body = (mission_dir / "consensus-rnd" / "github" / "mission-pr.md").read_text(encoding="utf-8")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["mission_pr"]["number"], "7")
        self.assertEqual(len(github_sync.PR_CLOSES_RE.findall(body)), 0)
        self.assertIn("Related: #42", body)
        self.assertFalse(any(command[:3] == ["gh", "pr", "create"] for command in calls))
        self.assertTrue(any(command[:3] == ["gh", "pr", "edit"] and "--body-file" in command for command in calls))

    def test_open_or_update_mission_pr_does_not_use_wp_lane_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = "001-demo"
            mission_dir = make_mission(repo, mission=mission, source_issue="42")
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {"WP01": {"number": "43"}},
                    "mission_pr": {},
                },
            )
            workspace_dir = repo / ".kittify" / "workspaces"
            workspace_dir.mkdir(parents=True)
            (workspace_dir / f"{mission}-lane-a.json").write_text(
                json.dumps(
                    {
                        "mission_slug": mission,
                        "branch_name": f"kitty/mission-{mission}-lane-a",
                        "base_branch": f"kitty/mission-{mission}",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                github_sync, "branch_has_commits", side_effect=lambda _repo, branch, _base: branch.endswith("-lane-a")
            ), mock.patch.object(github_sync, "github_write_preflight") as preflight, mock.patch.object(github_sync, "run_command") as run_command:
                result = github_sync.open_or_update_mission_pr(repo, mission, execute=False)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "mission branch has no commits or cannot be resolved")
        self.assertNotIn(f"kitty/mission-{mission}-lane-a", github_sync.candidate_mission_branches(repo, mission, mission_path=mission_dir))
        preflight.assert_not_called()
        run_command.assert_not_called()

    def test_open_or_update_mission_pr_rejects_explicit_wp_lane_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission = "001-demo"
            mission_dir = make_mission(repo, mission=mission, source_issue="42")
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {"WP01": {"number": "43"}},
                    "mission_pr": {},
                },
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync, "github_write_preflight") as preflight, mock.patch.object(
                github_sync, "run_command"
            ) as run_command:
                result = github_sync.open_or_update_mission_pr(
                    repo,
                    mission,
                    head_override=f"kitty/mission-{mission}-lane-a",
                    execute=False,
                )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "mission PR head must be a mission branch, not a WP lane branch")
        preflight.assert_not_called()
        run_command.assert_not_called()

    def test_mark_merged_can_bind_verified_legacy_merged_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo, source_issue="42")
            write_wp_status(mission_dir, {"WP01": "done", "WP02": "done"})
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {"WP01": {"number": "43"}, "WP02": {"number": "44"}},
                    "mission_pr": {},
                },
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], _repo: Path) -> github_sync.CommandResult:
                calls.append(command)
                if command[:3] == ["gh", "pr", "view"]:
                    payload = {
                        "state": "MERGED",
                        "mergedAt": "2026-06-06T19:49:20Z",
                        "mergeCommit": {"oid": "ba7f0461888c07515bb7afa81985747df35ee91e"},
                        "url": "https://github.com/owner/repo/pull/93",
                        "headRefName": "codex/catch-up",
                        "baseRefName": "feature/cockpit",
                    }
                    return github_sync.CommandResult(command, 0, json.dumps(payload), "")
                if command[:3] == ["gh", "issue", "edit"]:
                    return github_sync.CommandResult(command, 0, "", "")
                if command[:3] == ["gh", "issue", "close"]:
                    return github_sync.CommandResult(command, 0, "", "")
                if command[:3] == ["gh", "pr", "edit"]:
                    return github_sync.CommandResult(command, 0, "", "")
                raise AssertionError(command)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                github_sync, "github_write_preflight", return_value={"status": "ready", "repo_slug": "owner/repo"}
            ), mock.patch.object(github_sync, "run_command", side_effect=fake_run):
                result = github_sync.mark_mission_merged(repo, "001-demo", merged_pr="93", execute=True)

            bindings = json.loads((mission_dir / "consensus-rnd" / "github-bindings.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "synced")
        self.assertEqual(bindings["mission_pr"]["number"], "93")
        self.assertEqual(bindings["mission_pr"]["binding_source"], "verified-merged-pr-override")
        self.assertEqual(bindings["mission_pr"]["merge_commit"], "ba7f0461888c07515bb7afa81985747df35ee91e")
        self.assertEqual(bindings["child_issues"]["WP01"]["phase"], github_sync.PHASE_MERGED)
        self.assertEqual(bindings["child_issues"]["WP02"]["phase"], github_sync.PHASE_MERGED)
        self.assertIn("last_status_at", bindings["child_issues"]["WP01"])
        self.assertTrue(any(command[:3] == ["gh", "issue", "close"] and command[3] == "42" for command in calls))
        self.assertTrue(any(command[:3] == ["gh", "issue", "close"] and command[3] == "43" for command in calls))

    def test_bind_merged_pr_records_verified_pr_without_closing_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo, source_issue="42")
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {"WP01": {"number": "43"}},
                    "mission_pr": {},
                },
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], _repo: Path) -> github_sync.CommandResult:
                calls.append(command)
                if command[:3] == ["gh", "pr", "view"]:
                    payload = {
                        "state": "MERGED",
                        "mergedAt": "2026-06-06T19:49:20Z",
                        "mergeCommit": {"oid": "ba7f0461888c07515bb7afa81985747df35ee91e"},
                        "url": "https://github.com/owner/repo/pull/93",
                        "headRefName": "codex/catch-up",
                        "baseRefName": "feature/cockpit",
                    }
                    return github_sync.CommandResult(command, 0, json.dumps(payload), "")
                raise AssertionError(command)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                github_sync, "github_write_preflight", return_value={"status": "ready", "repo_slug": "owner/repo"}
            ), mock.patch.object(github_sync, "run_command", side_effect=fake_run):
                result = github_sync.bind_merged_pr(repo, "001-demo", merged_pr="93", execute=True)

            bindings = json.loads((mission_dir / "consensus-rnd" / "github-bindings.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "synced")
        self.assertEqual(bindings["mission_pr"]["number"], "93")
        self.assertEqual(bindings["mission_pr"]["binding_source"], "verified-merged-pr-override")
        self.assertEqual(bindings["mission_pr"]["merge_commit"], "ba7f0461888c07515bb7afa81985747df35ee91e")
        self.assertEqual([command[:3] for command in calls], [["gh", "pr", "view"]])

    def test_mark_merged_rejects_unmerged_legacy_pr_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo, source_issue="42")
            write_wp_status(mission_dir, {"WP01": "done"})
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )

            def fake_run(command: list[str], _repo: Path) -> github_sync.CommandResult:
                if command[:3] == ["gh", "pr", "view"]:
                    return github_sync.CommandResult(
                        command,
                        0,
                        json.dumps({"state": "OPEN", "mergedAt": "", "mergeCommit": None}),
                        "",
                    )
                raise AssertionError(command)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                github_sync, "github_write_preflight", return_value={"status": "ready", "repo_slug": "owner/repo"}
            ), mock.patch.object(github_sync, "run_command", side_effect=fake_run):
                result = github_sync.mark_mission_merged(repo, "001-demo", merged_pr="93", execute=True)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "mission PR is not merged; refusing to close issues")

    def test_mark_merged_can_skip_reused_parent_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo, source_issue="42")
            write_wp_status(mission_dir, {"WP01": "done"})
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {"WP01": {"number": "43"}},
                    "mission_pr": {},
                },
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(command: list[str], _repo: Path) -> github_sync.CommandResult:
                calls.append(command)
                if command[:3] == ["gh", "pr", "view"]:
                    payload = {
                        "state": "MERGED",
                        "mergedAt": "2026-06-06T19:49:20Z",
                        "mergeCommit": {"oid": "ba7f0461888c07515bb7afa81985747df35ee91e"},
                    }
                    return github_sync.CommandResult(command, 0, json.dumps(payload), "")
                if command[:3] in (["gh", "issue", "edit"], ["gh", "issue", "close"], ["gh", "pr", "edit"]):
                    return github_sync.CommandResult(command, 0, "", "")
                raise AssertionError(command)

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                github_sync, "github_write_preflight", return_value={"status": "ready", "repo_slug": "owner/repo"}
            ), mock.patch.object(github_sync, "run_command", side_effect=fake_run):
                result = github_sync.mark_mission_merged(repo, "001-demo", merged_pr="93", skip_parent=True, execute=True)

            bindings = json.loads((mission_dir / "consensus-rnd" / "github-bindings.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "synced")
        self.assertFalse(any(command[:3] == ["gh", "issue", "close"] and command[3] == "42" for command in calls))
        self.assertTrue(any(command[:3] == ["gh", "issue", "close"] and command[3] == "43" for command in calls))
        self.assertEqual(bindings["child_issues"]["WP01"]["phase"], github_sync.PHASE_MERGED)

    def test_mark_merged_blocks_when_spec_kitty_wps_are_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            mission_dir = make_mission(repo, source_issue="42")
            write_wp_status(mission_dir, {"WP01": "approved", "WP02": "done"})
            github_sync.write_bindings(
                mission_dir,
                {
                    "schema": "consensus-rnd-spec.github-bindings.v1",
                    "parent_issue": {"number": "42"},
                    "child_issues": {"WP01": {"number": "43"}, "WP02": {"number": "44"}},
                    "mission_pr": {},
                },
            )
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                github_sync, "github_write_preflight", return_value={"status": "ready", "repo_slug": "owner/repo"}
            ), mock.patch.object(github_sync, "run_command") as run_command:
                result = github_sync.mark_mission_merged(repo, "001-demo", merged_pr="93", execute=False)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "Spec Kitty WPs are not done; refusing to close issues")
        self.assertEqual(result["lane_guard"]["not_done"], [{"wp_id": "WP01", "kitty_lane": "approved"}])
        run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
