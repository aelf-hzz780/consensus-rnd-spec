#!/usr/bin/env python3
"""Detect the backend for a host repository."""

from __future__ import annotations

import argparse
from pathlib import Path

from backend_common import detect_backend, load_config, print_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="Host repository root")
    parser.add_argument("--mode", choices=("auto", "spec-kitty", "native"), default=None)
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    config = load_config(repo)
    print_json(detect_backend(config.repo_root, mode=args.mode or config.backend_mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
