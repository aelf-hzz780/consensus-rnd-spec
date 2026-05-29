from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "consensus-rnd-spec" / "scripts" / "native_backend.sh"


class NativeBackendTests(unittest.TestCase):
    def test_native_plan_requires_opt_in(self) -> None:
        result = subprocess.run(["bash", str(SCRIPT), "plan"], cwd=ROOT, capture_output=True, text=True, check=False)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")

    def test_native_run_delegates_to_spawn_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            skill = Path(tmp) / "native-skill"
            scripts = skill / "scripts"
            scripts.mkdir(parents=True)
            (skill / "SKILL.md").write_text("native skill\n", encoding="utf-8")
            spawn = scripts / "spawn-codex.sh"
            spawn.write_text("#!/usr/bin/env bash\necho \"$@\" > \"$REPO_ROOT/spawn.args\"\n", encoding="utf-8")
            spawn.chmod(0o755)
            env_dir = repo / ".consensus-rnd-spec"
            env_dir.mkdir()
            (env_dir / "host.env").write_text(
                f'export REPO_ROOT="{repo}"\nexport NATIVE_FULL_LOOP_ENABLE="true"\nexport NATIVE_CONSENSUS_SKILL_ROOT="{skill}"\n',
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["REPO_ROOT"] = str(repo)

            result = subprocess.run(["bash", str(SCRIPT), "run"], cwd=repo, env=env, capture_output=True, text=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--prompt", (repo / "spawn.args").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
