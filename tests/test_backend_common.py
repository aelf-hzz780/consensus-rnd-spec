from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "consensus-rnd-spec" / "scripts"
SPEC = importlib.util.spec_from_file_location("backend_common", SCRIPT_DIR / "backend_common.py")
backend_common = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["backend_common"] = backend_common
SPEC.loader.exec_module(backend_common)


class BackendCommonTests(unittest.TestCase):
    def test_parse_host_env_supports_export_and_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "host.env"
            env.write_text(
                'export BACKEND_MODE="native"\nCODEX_FLOOR=1\nNATIVE_FULL_LOOP_ENABLE=true\n',
                encoding="utf-8",
            )

            values = backend_common.parse_host_env(env)

        self.assertEqual(values["BACKEND_MODE"], "native")
        self.assertEqual(values["CODEX_FLOOR"], "1")
        self.assertEqual(values["NATIVE_FULL_LOOP_ENABLE"], "true")

    def test_detect_backend_forced_native_without_spec_kitty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = backend_common.detect_backend(Path(tmp), mode="native")

        self.assertEqual(result["backend"], "native")
        self.assertEqual(result["reason"], "forced native backend")

    def test_forced_spec_kitty_blocks_when_cli_or_files_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = backend_common.detect_backend(Path(tmp), mode="spec-kitty")

        self.assertEqual(result["backend"], "blocked")
        self.assertIn("Spec Kitty is unavailable", result["reason"])

    def test_config_floor_has_hard_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            config_dir = repo / ".consensus-rnd-spec"
            config_dir.mkdir()
            (config_dir / "host.env").write_text('CODEX_FLOOR="1"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                config = backend_common.load_config(repo)

        self.assertEqual(config.code_floor, 2)
        self.assertEqual(config.spec_kitty_scan_limit, 30)

    def test_parse_duration_seconds(self) -> None:
        self.assertEqual(backend_common.parse_duration_seconds("10min", 1), 600)
        self.assertEqual(backend_common.parse_duration_seconds("2h", 1), 7200)
        self.assertEqual(backend_common.parse_duration_seconds(None, 42), 42)


if __name__ == "__main__":
    unittest.main()
