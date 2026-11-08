from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    """Tiny .env loader (does not override variables already set)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()


def get(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"Missing env var {name}. Run: python tools/init_env.py")
    return v
