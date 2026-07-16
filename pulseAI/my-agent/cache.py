"""
cache.py
--------
A small, deliberately conservative cache for the agent's *read-only* tool
calls (read_file, list_files, grep_files) within a single run_agent() task.

Why cache at all: it's common for a multi-step ReAct task to read_file() the
same file two or three times as it reasons (e.g. read it, write changes,
read it back to verify, then read it again while writing a summary) — those
repeat reads are pure waste, since the file didn't change in between.

Why scope it to ONE task, not across turns/sessions: files on disk can be
edited by the user, other processes, or a previous turn between agent runs,
so a cache that outlives a single run_agent() call could silently feed the
model stale content. A fresh cache per task is the safe default; see
ToolCache.invalidate_all() for how mutations bust it mid-task too.

Invalidation policy: intentionally blunt on purpose. Any write_file or
run_command call flushes the *entire* cache, not just the specific path
touched — because run_command can run arbitrary shell (a script, `mv`, a
build tool) that changes files we have no way to enumerate in advance.
A stale cache is a correctness bug (the agent reasons about content that no
longer exists); a slightly-too-aggressive flush just costs one extra disk
read. That tradeoff is the right one here.
"""

from __future__ import annotations

import json
import threading
from typing import Callable

# Tools that are pure reads of current on-disk state -- safe to cache.
CACHEABLE_TOOLS = frozenset({"read_file", "list_files", "grep_files"})

# Tools that can change on-disk state -- calling any of these invalidates
# the whole cache, since we can't know what they touched.
#
# Real bug found and fixed before shipping apply_edit (tools.py): this set
# was hardcoded to exactly {"write_file", "run_command"} when apply_edit
# was added as a new file-mutating tool -- if apply_edit weren't added
# here too, a prior cached read_file() result for a file would keep being
# served as "current" after apply_edit silently changed that file on disk,
# reproducing the EXACT class of stale-cache bug this module's own
# docstring warns about. Confirmed directly (before this fix) that
# "apply_edit" was NOT a member of this set even though the tool existed
# and was already registered in tools.TOOL_FUNCTIONS.
MUTATING_TOOLS = frozenset({"write_file", "apply_edit", "run_command"})


def _cache_key(name: str, args: dict) -> str:
    """Deterministic cache key regardless of argument/dict key ordering."""
    return f"{name}:{json.dumps(args, sort_keys=True)}"


class ToolCache:
    """Per-task cache for read-only tool results, with hit/miss stats.

    Thread-safe: agent.py may now execute several independent tool calls
    from the same LLM turn concurrently (see run_agent's use of a thread
    pool for batched read-only calls), so multiple get_or_call() calls can
    race on the same cache instance. A single lock around the whole
    read-check-write section is simple and correct; these calls are I/O
    bound (disk/subprocess), not CPU bound, so lock contention is a non-issue.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    def get_or_call(self, name: str, args: dict, call: Callable[[], str]) -> tuple[str, bool]:
        """
        Return (result, was_cached) for a tool call. If `name` is cacheable
        and we've already seen this exact (name, args) pair since the last
        invalidation, return the cached result without calling `call()`.
        Otherwise invoke `call()`, cache the result if cacheable, and return
        it. Mutating tools always invalidate the whole cache after running,
        regardless of success/failure (conservative: an error from a shell
        command doesn't guarantee nothing changed beforehand).
        """
        if name in CACHEABLE_TOOLS:
            key = _cache_key(name, args)
            with self._lock:
                if key in self._store:
                    self.hits += 1
                    return self._store[key], True
            # Call outside the lock -- it's the slow part (disk/subprocess)
            # and doesn't touch shared state, so no need to hold the lock
            # while it runs. Two threads racing on an identical uncached
            # call will both do the work once each; harmless, just a wasted
            # duplicate read in the rare case it happens.
            result = call()
            with self._lock:
                self.misses += 1
                self._store[key] = result
            return result, False

        # Not cacheable -> just run it.
        result = call()
        if name in MUTATING_TOOLS:
            self.invalidate_all()
        return result, False

    def invalidate_all(self) -> None:
        with self._lock:
            if self._store:
                self.invalidations += 1
            self._store.clear()

    def stats(self) -> str:
        return (
            f"cache hits={self.hits} misses={self.misses} "
            f"invalidations={self.invalidations} entries_now={len(self._store)}"
        )
