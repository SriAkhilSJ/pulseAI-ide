"""
checkpoint.py
-------------
Deterministic, non-agent-controlled safety net for file writes.

Design principle (per the discussion that led here): safety must be
deterministic, never probabilistic. The agent (an LLM) decides *what* to
write, but it never decides *whether a backup happens* or *whether it can
be undone* -- those are unconditional, plain-Python behaviors that run the
same way every time, regardless of what the model does or says.

What this module does NOT do: it doesn't try to be git. If the project is
already a git repo, real git integration would be a fine (better, even)
implementation later -- but that's a separate, larger feature (needs to
handle staging, branches, existing history). This is the minimal thing that
makes write_file safe to use today: every overwrite of an existing file is
preceded by a timestamped backup, unconditionally, so nothing is ever lost
even if a confirmation prompt is accidentally approved.

Persistence: the backup FILES were already durable (they're just files on
disk), but the in-memory record of "which backup is the most recent one for
this path" used to live only in a Python dict -- so it was lost the moment
the process restarted or crashed, even though the actual backup was sitting
right there on disk the whole time. That defeated the exact scenario undo
exists for (recovering after something goes wrong). The history is now
persisted to a small JSON index file alongside the backups, and reloaded on
every CheckpointManager construction.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


class CheckpointManager:
    """Backs up files before they're overwritten, and can restore them.

    The history of "which backups exist for which file, in what order" is
    persisted to `<backup_dir>/index.json` so undo_last_edit keeps working
    across process restarts, not just within one run_agent() call.
    """

    def __init__(self, project_root: Path, backup_dirname: str = ".agent_backups") -> None:
        self.project_root = Path(project_root)
        self.backup_dir = self.project_root / backup_dirname
        self._index_path = self.backup_dir / "index.json"
        # {str(resolved absolute path) -> [backup_filename, ...]}, oldest first
        self._history: dict[str, list[str]] = self._load_index()

    def _load_index(self) -> dict[str, list[str]]:
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt/unreadable index shouldn't crash the whole agent --
            # just start fresh; existing backup files on disk aren't lost,
            # only their newest-first ordering record is.
            return {}

    def _save_index(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(self._history, indent=2), encoding="utf-8")

    def checkpoint_before_write(self, target: Path) -> Path | None:
        """
        Back up `target`'s CURRENT on-disk content before it gets
        overwritten. Called unconditionally by write_file, before any
        confirmation prompt is even shown -- so the backup exists
        regardless of what the user or the model does next.

        Returns the backup path, or None if `target` didn't exist yet
        (a brand-new file has nothing to back up).
        """
        target = target.resolve()
        if not target.exists() or not target.is_file():
            return None

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        # Microseconds too, so rapid-fire writes to the same file in one
        # task don't collide on the same filename.
        micros = f"{time.time():.6f}".split(".")[1]
        backup_name = f"{target.name}.{timestamp}.{micros}.bak"
        backup_path = self.backup_dir / backup_name

        shutil.copy2(target, backup_path)
        self._history.setdefault(str(target), []).append(backup_name)
        self._save_index()
        return backup_path

    def undo_last_write(self, target: Path) -> tuple[bool, str]:
        """Restore `target` to its content from immediately before the most
        recent write() for this path -- persisted across process restarts,
        not just within one run. Returns (success, message)."""
        target = target.resolve()
        key = str(target)
        backups = self._history.get(key)
        if not backups:
            return False, f"No checkpoint found for {target.name} -- nothing to undo."

        last_backup_name = backups[-1]
        last_backup_path = self.backup_dir / last_backup_name
        if not last_backup_path.exists():
            return False, f"Backup file {last_backup_name} is missing on disk -- cannot restore."

        shutil.copy2(last_backup_path, target)
        backups.pop()
        self._save_index()
        return True, f"Restored {target.name} from checkpoint {last_backup_name} (made just before that write)."

    def has_checkpoint(self, target: Path) -> bool:
        return bool(self._history.get(str(target.resolve())))
