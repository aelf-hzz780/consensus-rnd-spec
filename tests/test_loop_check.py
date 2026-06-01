from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
for name in ("backend_common", "loop_check"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)

import loop_check


class LoopCheckCountTests(unittest.TestCase):
    def test_count_inflight_only_counts_spawn_codex_workers_for_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            repo = Path(tmp)
            other_repo = Path(other)
            stdout = "\n".join(
                [
                    f"python3 -c wrapper concurrency_monitor {repo} python3 /native/consensus-rnd-cli concurrency --daemon",
                    f"python3 /native/consensus-rnd-cli spawn-codex --cd {repo} --prompt {repo}/p1.md --log {repo}/l1.log --stall 3600",
                    f"python3 /native/consensus-rnd-cli spawn-codex --cd {repo} --prompt {repo}/p1.md --log {repo}/l1.log --stall 3600",
                    f"node /bin/codex exec --skip-git-repo-check -C {repo} -",
                    f"python3 /native/consensus-rnd-cli peek --cd {repo}",
                    f"python3 /native/consensus-rnd-cli spawn-codex --cd {other_repo} --prompt {other_repo}/p.md --log {other_repo}/l.log",
                    f"python3 /native/consensus-rnd-cli spawn-codex --cd {repo} --prompt {repo}/p2.md --log {repo}/l2.log --stall 3600",
                ]
            )
            result = SimpleNamespace(returncode=0, stdout=stdout)

            with mock.patch("subprocess.run", return_value=result):
                self.assertEqual(loop_check.count_inflight(repo), 2)


if __name__ == "__main__":
    unittest.main()
