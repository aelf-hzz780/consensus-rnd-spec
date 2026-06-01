from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts" / "native_capabilities.py"
SPEC = importlib.util.spec_from_file_location("native_capabilities", SCRIPT)
native_capabilities = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(native_capabilities)


class NativeCapabilitiesTests(unittest.TestCase):
    def test_invalid_skill_root_blocks(self) -> None:
        payload = native_capabilities.detect_native_capabilities("/does/not/exist")

        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["reason"], "NATIVE_CONSENSUS_SKILL_ROOT is invalid")

    def test_legacy_cli_is_preferred_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp)
            scripts = skill / "scripts"
            scripts.mkdir()
            (skill / "SKILL.md").write_text("native skill\n", encoding="utf-8")
            legacy = scripts / "consensus-rnd-cli"
            legacy.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            legacy.chmod(0o755)
            (scripts / "spawn-codex.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            payload = native_capabilities.detect_native_capabilities(skill)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["entrypoint"], "legacy-cli")

    def test_spawn_wrapper_supports_upstream_main_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp)
            scripts = skill / "scripts"
            scripts.mkdir()
            (skill / "SKILL.md").write_text("native skill\n", encoding="utf-8")
            (scripts / "spawn-codex.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            payload = native_capabilities.detect_native_capabilities(skill)

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["entrypoint"], "spawn-wrapper")


if __name__ == "__main__":
    unittest.main()
