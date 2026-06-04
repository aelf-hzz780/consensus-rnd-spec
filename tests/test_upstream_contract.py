from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
COMMON_SPEC = importlib.util.spec_from_file_location("backend_common", SCRIPT_DIR / "backend_common.py")
backend_common = importlib.util.module_from_spec(COMMON_SPEC)
assert COMMON_SPEC and COMMON_SPEC.loader
sys.modules["backend_common"] = backend_common
COMMON_SPEC.loader.exec_module(backend_common)

SPEC = importlib.util.spec_from_file_location("upstream_contract", SCRIPT_DIR / "upstream_contract.py")
upstream_contract = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["upstream_contract"] = upstream_contract
SPEC.loader.exec_module(upstream_contract)


class UpstreamContractTests(unittest.TestCase):
    def test_contract_passes_when_required_anchors_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "SKILL.md"
            labels = root / "scripts" / "codex_refactor_loop" / "labels.py"
            labels.parent.mkdir(parents=True)
            skill.write_text("🤖 controller status banner\n⟦AI:AUTO-LOOP⟧\n", encoding="utf-8")
            labels.write_text(
                '_spec("lifecycle", "managed", "x", "ededed")\n'
                '_spec("phase", "consensus-reached", "x", "1d76db")\n'
                '_spec("human", "auto", "x", "bfd4f2")\n',
                encoding="utf-8",
            )

            result = upstream_contract.check_contract(root)

        self.assertEqual(result["status"], "ready")
        self.assertTrue(all(result["checks"].values()))

    def test_contract_blocks_when_upstream_anchor_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "SKILL.md"
            labels = root / "scripts" / "codex_refactor_loop" / "labels.py"
            labels.parent.mkdir(parents=True)
            skill.write_text("missing anchors\n", encoding="utf-8")
            labels.write_text('"crnd:lifecycle:managed"\n', encoding="utf-8")

            result = upstream_contract.check_contract(root)

        self.assertEqual(result["status"], "blocked")
        self.assertIn("sentinel_in_skill", result["missing_checks"])
        self.assertIn("phase_anchor_in_labels", result["missing_checks"])


if __name__ == "__main__":
    unittest.main()
