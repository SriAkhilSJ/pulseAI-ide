"""
test_fts_memory.py
------------------
TDD Unit Tests for PulseCodeAI SQLite FTS5 Conversation & Project Memory Engine.
Verifies full-text search query accuracy and historical note extraction across sessions.
"""
import os
import pytest
from pathlib import Path
from src.fts_memory import ConversationMemory


def test_fts_init_and_search(tmp_path):
    db_file = tmp_path / "test_memory.db"
    mem = ConversationMemory(db_path=str(db_file))
    
    # Record turns across sessions
    mem.record_turn("session-1", "user", "How do we handle user authentication?")
    mem.record_turn("session-1", "assistant", "We decided to use JWT tokens with a 15-minute expiry.")
    mem.record_turn("session-2", "user", "What is the database connection pool limit?")
    mem.record_turn("session-2", "assistant", "The database connection pool limit is set to 20 in config.py.")
    
    # Search FTS5
    jwt_results = mem.search_history("JWT tokens")
    assert len(jwt_results) == 1
    assert "JWT tokens with a 15-minute expiry" in jwt_results[0]["content"]
    assert jwt_results[0]["session_id"] == "session-1"
    
    # Both turns in session-2 mention "connection pool"
    pool_results = mem.search_history("connection pool")
    assert len(pool_results) == 2
    assert all(r["session_id"] == "session-2" for r in pool_results)
    assert any("set to 20 in config.py" in r["content"] for r in pool_results)


def test_project_notes(tmp_path):
    db_file = tmp_path / "test_notes.db"
    mem = ConversationMemory(db_path=str(db_file))
    
    mem.add_project_note("Auth: Migrated from OAuth2 to JWT token verification", category="auth")
    mem.add_project_note("DB: Set SQLite journal_mode=WAL for concurrency", category="database")
    
    notes = mem.get_project_notes()
    assert len(notes) == 2
    assert notes[0]["category"] in ["auth", "database"]
    
    auth_notes = mem.get_project_notes(category="auth")
    assert len(auth_notes) == 1
    assert "JWT token verification" in auth_notes[0]["note"]
