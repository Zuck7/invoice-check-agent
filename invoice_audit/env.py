"""Minimal .env loading.

No dependency: the audit engine has none and an API key is not a good reason to
start. This reads the handful of ``KEY=value`` lines a .env actually contains.

**A real environment variable always wins.** The shell, a CI secret and a
systemd unit are all more authoritative than a file someone left in the working
directory, and quietly overriding them is how a deploy ends up using a
developer's key.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_NAME = ".env"


def parse(text: str) -> dict[str, str]:
    """Parse .env contents. Ignores blanks, comments and `export ` prefixes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def find(start: Path | None = None, name: str = DEFAULT_NAME) -> Path | None:
    """Look for a .env in this directory and its parents."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def load(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Load a .env into ``os.environ``. Returns what it applied."""
    target = path or find()
    if target is None or not Path(target).is_file():
        return {}

    applied: dict[str, str] = {}
    for key, value in parse(Path(target).read_text(encoding="utf-8")).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
