"""
missions.py
-----------
Deterministic, file-based checkpoint system for breaking one large task
into multiple "missions" -- each mission runs its own short conversation
(so it never needs context compression or hits memory.py's history cap),
and continuity BETWEEN missions happens through an explicit, compact
PROGRESS checkpoint instead of carrying the full chat transcript forward.

Why this instead of a SQLite/FTS5/three-zone-compression system: that
architecture assumes the right unit of "session" is one giant, ever-growing
conversation that eventually needs smart summarization to stay within a
token budget. This takes a different, more IDE-agent-appropriate approach:
don't let any one conversation grow unbounded in the first place. Decompose
"build a full-stack app" into missions ("design schema", "build backend",
"build frontend", "wire them together"); each mission gets a small, fresh
context, and hands off to the next via a short, LLM-authored summary of
what's done / what's next / which files matter -- not a raw message log.

This is a genuinely different bug fix than "raise memory.py's 20-turn cap"
or "search past conversations with FTS5": it directly targets the actual
failure mode (a single session growing too large to reason about or fit in
context) rather than making the container for that growth bigger or
smarter. If cross-session SEARCH ever becomes a real, separately-justified
need, that's still a reasonable candidate for FTS5 later -- but it's not
required to solve the "10-hour session eventually hits a wall" problem.

Storage: .agent_missions/<mission_id>/progress.json (machine-readable) and
progress.md (human-readable) -- plain files, no new dependencies, easy to
inspect by hand exactly like memory.json already is. Each mission also gets
its own memory_<mission_id>.json (see memory.py's path parameter) so one
mission's turn-by-turn history never mixes with another's.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

MISSIONS_DIR = Path(__file__).parent / ".agent_missions"


def _safe_id(mission_id: str) -> str:
    """Sanitize a mission id to something safe for use as a directory name."""
    cleaned = "".join(c for c in str(mission_id) if c.isalnum() or c in "-_")
    return cleaned or "default"


def mission_dir(mission_id: str) -> Path:
    return MISSIONS_DIR / _safe_id(mission_id)


def mission_memory_path(mission_id: str) -> Path:
    """Where this mission's turn-by-turn conversation history lives --
    separate from other missions' history and from the global memory.json."""
    return mission_dir(mission_id) / "memory.json"


def save_progress(
    mission_id: str,
    summary: str,
    next_step: str = "",
    key_files: Optional[list[str]] = None,
) -> Path:
    """
    Persist a checkpoint for `mission_id`: what's been done, what should
    happen next, and which files matter. This OVERWRITES any previous
    checkpoint for this mission id -- it's meant to be "the current state
    of this mission", not a growing log (the mission's own memory.json
    already keeps the turn-by-turn record for as long as that mission is
    actively running; this is specifically the compact handoff to whoever
    -- human or a fresh mission -- picks this up next).
    """
    key_files = key_files or []
    d = mission_dir(mission_id)
    d.mkdir(parents=True, exist_ok=True)

    state = {
        "mission_id": mission_id,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "next_step": next_step,
        "key_files": key_files,
    }
    (d / "progress.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    md_lines = [
        f"# Mission: {mission_id}",
        f"_Last updated: {state['updated_at']}_",
        "",
        "## Completed so far",
        summary.strip() or "(nothing recorded yet)",
        "",
        "## Next step",
        next_step.strip() or "(not specified)",
        "",
        "## Key files",
        "\n".join(f"- {f}" for f in key_files) if key_files else "(none listed)",
        "",
    ]
    (d / "progress.md").write_text("\n".join(md_lines), encoding="utf-8")
    return d / "progress.md"


def load_progress(mission_id: str) -> Optional[dict[str, Any]]:
    """Return the saved checkpoint dict for `mission_id`, or None if there
    isn't one yet (e.g. this is the mission's first-ever run)."""
    path = mission_dir(mission_id) / "progress.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_missions() -> list[dict[str, Any]]:
    """List every mission that has at least one saved checkpoint, most
    recently updated first."""
    if not MISSIONS_DIR.exists():
        return []
    missions = []
    for d in MISSIONS_DIR.iterdir():
        if not d.is_dir():
            continue
        state = load_progress(d.name)
        if state:
            missions.append(state)
    return sorted(missions, key=lambda m: m.get("updated_at", ""), reverse=True)
