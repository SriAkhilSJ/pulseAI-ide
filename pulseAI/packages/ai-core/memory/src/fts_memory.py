"""
fts_memory.py
-------------
PulseCodeAI SQLite FTS5 Conversation & Project Memory Engine.
Provides instant cross-session recall and structured note storage.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConversationMemory:
    """Persistent SQLite-backed full-text search memory across sessions."""

    def __init__(self, db_path: str = ".pulsecode/memory.db"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            # Enable WAL mode for high-concurrency reading/writing across agents
            conn.execute("PRAGMA journal_mode=WAL;")
            
            # Sessions table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    title TEXT,
                    summary TEXT
                );
            """)

            # FTS5 Virtual Table for Conversation Turns
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    session_id,
                    role,
                    content,
                    tags,
                    timestamp UNINDEXED
                );
            """)

            # Project architectural notes table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    category TEXT,
                    note TEXT NOT NULL
                );
            """)
            conn.commit()

    def record_turn(self, session_id: str, role: str, content: str, tags: Optional[List[str]] = None) -> None:
        """Record a single conversation turn into the FTS5 search index."""
        tag_str = ",".join(tags) if tags else ""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            # Ensure session entry exists
            conn.execute("INSERT OR IGNORE INTO sessions (session_id) VALUES (?);", (session_id,))
            conn.execute(
                "INSERT INTO turns_fts (session_id, role, content, tags, timestamp) VALUES (?, ?, ?, ?, ?);",
                (session_id, role, content, tag_str, timestamp)
            )
            conn.commit()

    def search_history(self, query_text: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Perform full-text search query across all historical turns."""
        with self._get_connection() as conn:
            # FTS5 match query
            cursor = conn.execute("""
                SELECT session_id, role, content, tags, timestamp
                FROM turns_fts
                WHERE turns_fts MATCH ?
                ORDER BY rank
                LIMIT ?;
            """, (query_text, limit))
            
            results = []
            for row in cursor.fetchall():
                results.append({
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "content": row["content"],
                    "tags": row["tags"].split(",") if row["tags"] else [],
                    "timestamp": row["timestamp"]
                })
            return results

    def add_project_note(self, note: str, category: str = "general") -> int:
        """Store a high-level architectural decision or project note."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "INSERT INTO project_notes (category, note) VALUES (?, ?);",
                (category, note)
            )
            conn.commit()
            return cursor.lastrowid

    def get_project_notes(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve stored architectural project notes, optionally filtered by category."""
        with self._get_connection() as conn:
            if category:
                cursor = conn.execute("SELECT id, created_at, category, note FROM project_notes WHERE category = ? ORDER BY id ASC;", (category,))
            else:
                cursor = conn.execute("SELECT id, created_at, category, note FROM project_notes ORDER BY id ASC;")
                
            return [dict(row) for row in cursor.fetchall()]
