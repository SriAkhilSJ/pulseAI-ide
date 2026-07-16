"""
memory.py
---------
Very small persistence layer for the agent's "memory": conversation history
across runs plus a list of free-form notes the agent can accumulate about
the project. Stored as plain JSON so it's easy to inspect/edit by hand.

Schema:
{
    "history": [ {"user": "...", "assistant": "..."}, ... ],
    "notes":   [ "free-form string note", ... ]
}

MAX_HISTORY_TURNS caps how many turns are kept ON DISK for a single memory
file -- confirmed via direct testing that turns beyond this cap are
silently and permanently dropped (turn 30 of a 50-turn session survives;
turns 0-29 do not, even across process restarts). For a single, ever-
growing conversation this is a real loss of context. The intended fix is
NOT to raise this cap indefinitely (that just delays hitting the LLM's own
context window instead) -- it's to keep any one conversation short by
design via missions.py, and use a separate memory.json PER MISSION (see
missions.mission_memory_path) so no single conversation needs more than
MAX_HISTORY_TURNS to begin with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MEMORY_PATH = Path(__file__).parent / "memory.json"
MAX_HISTORY_TURNS = 20  # keep any single memory file from growing unbounded

DEFAULT_MEMORY: dict[str, Any] = {"history": [], "notes": []}


def load(path: Path | None = None) -> dict[str, Any]:
    """Load memory from disk, creating a fresh default file if missing/corrupt.

    `path` defaults to the shared global MEMORY_PATH; pass a different path
    (e.g. missions.mission_memory_path(mission_id)) to keep a mission's
    history in its own file, isolated from other missions and from the
    global conversation."""
    path = path or MEMORY_PATH
    if not path.exists():
        save(DEFAULT_MEMORY, path)
        return json.loads(json.dumps(DEFAULT_MEMORY))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("history", [])
        data.setdefault("notes", [])
        return data
    except (json.JSONDecodeError, OSError):
        save(DEFAULT_MEMORY, path)
        return json.loads(json.dumps(DEFAULT_MEMORY))


def save(data: dict[str, Any], path: Path | None = None) -> None:
    path = path or MEMORY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_turn(data: dict[str, Any], user: str, assistant: str, path: Path | None = None) -> None:
    """Add a user/assistant exchange to history and persist, trimming old turns."""
    data.setdefault("history", []).append({"user": user, "assistant": assistant})
    data["history"] = data["history"][-MAX_HISTORY_TURNS:]
    save(data, path)


def add_note(note: str, path: Path | None = None) -> None:
    """Append a standalone note (e.g. project fact) the agent wants to remember."""
    data = load(path)
    data.setdefault("notes", []).append(note)
    save(data, path)


def reset(path: Path | None = None) -> None:
    """Wipe memory back to defaults."""
    save(json.loads(json.dumps(DEFAULT_MEMORY)), path)

