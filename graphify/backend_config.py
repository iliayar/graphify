"""The user's saved LLM backend choice: ``~/.graphify/config.json``.

Written when a backend is chosen by hand (``graphify extract --backend X
[--model M] [--acp-command CMD]``) so later runs reuse it without repeating the
flags; read by :func:`graphify.llm.detect_backend` and the ACP agent lookup.
Keys (all optional)::

    {"backend": "acp", "model": "...", "acp_command": "omp acp"}

Global (per user) on purpose, next to ``~/.graphify/providers.json``: it names
how *this user* reaches an LLM, not a property of any one project. Never holds
credentials. Reads and writes are best effort; a broken file reads as empty.
"""
from __future__ import annotations

import json
from pathlib import Path

FIELDS = ("backend", "model", "acp_command")


def config_path() -> Path:
    return Path.home() / ".graphify" / "config.json"


def load() -> dict[str, str]:
    """The saved choice (only known, non-empty string fields)."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in FIELDS and isinstance(v, str) and v.strip()}


def save(**updates: str | None) -> bool:
    """Merge ``updates`` into the saved choice; ``None`` leaves a field as is.

    Changing ``backend`` drops a saved ``model`` that was not re-supplied: a model
    name is only meaningful for the backend it was chosen with. Returns True when
    the stored choice changed.
    """
    current = load()
    merged = dict(current)
    new_backend = updates.get("backend")
    if new_backend and new_backend != current.get("backend") and not updates.get("model"):
        merged.pop("model", None)
    for key, value in updates.items():
        if key in FIELDS and value:
            merged[key] = value
    if merged == current:
        return False
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
