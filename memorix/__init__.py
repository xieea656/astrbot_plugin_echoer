"""Memorix runtime package for AstrBot plugin."""

from __future__ import annotations

import sys
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    # Keep compatibility for migrated A_memorix absolute imports: `core.*`, `amemorix.*`
    sys.path.insert(0, str(_BASE_DIR))

