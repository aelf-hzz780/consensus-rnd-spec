from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginStructureTests(unittest.TestCase):
    def test_manifest_shape(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "consensus-rnd-spec")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertIsInstance(manifest["interface"]["defaultPrompt"], list)
        self.assertIn("Write", manifest["interface"]["capabilities"])

    def test_skill_frontmatter_and_scripts_exist(self) -> None:
        skill = ROOT / "skills" / "consensus-rnd-spec" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")

        self.assertTrue(text.startswith("---\nname: consensus-rnd-spec\n"))
        for script in (
            "detect_backend.py",
            "discovery.py",
            "intake.py",
            "loop_check.py",
            "native_capabilities.py",
            "promote_discovery.py",
            "run_loop.py",
            "spec_backend.py",
            "native_backend.sh",
        ):
            self.assertTrue((skill.parent / "scripts" / script).exists(), script)


if __name__ == "__main__":
    unittest.main()
