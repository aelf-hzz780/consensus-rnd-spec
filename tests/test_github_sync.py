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
backend_common = importlib.util.module_from_spec(COMMON_SPEC)
assert COMMON_SPEC and COMMON_SPEC.loader
sys.modules["backend_common"] = backend_common
COMMON_SPEC.loader.exec_module(backend_common)

SPEC = importlib.util.spec_from_file_location("github_sync", SCRIPT_DIR / "github_sync.py")
github_sync = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["github_sync"] = github_sync
SPEC.loader.exec_module(github_sync)


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


class GitHubSyncTests(unittest.TestCase):
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

        self.assertEqual(len(github_sync.PR_CLOSES_RE.findall(body)), 1)
        self.assertIn("Closes #9", body)
        self.assertIn("`WP01`: #10", body)
        self.assertTrue(body.rstrip().endswith("⟦AI:AUTO-LOOP⟧"))

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
            (repo / ".consensus-rnd-spec").mkdir()
            (repo / ".consensus-rnd-spec" / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport GH_REPO_SLUG="owner/repo"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(github_sync.shutil, "which", return_value=None):
                result = github_sync.sync_wp_status(repo, "001-demo", "WP01", github_sync.PHASE_IMPLEMENTING, execute=True)

        self.assertEqual(result["status"], "blocked")
        self.assertIn("gh CLI not found", result["reason"])


if __name__ == "__main__":
    unittest.main()
