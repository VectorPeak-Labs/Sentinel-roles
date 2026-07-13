"""Ensure the repo root is importable regardless of how pytest is invoked
(bare `pytest` does not add the CWD to sys.path; `python -m pytest` does)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
