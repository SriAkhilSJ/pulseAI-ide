"""
test_completions.py
-------------------
TDD Unit Tests for PulseCodeAI GhostText Inline Completion Engine (`packages/ai-core/completions`).
Verifies FIM prompt construction, completion prediction, and LRU keystroke caching.
"""
from unittest.mock import MagicMock
import pytest
from src.completions import FimCompletionEngine


def test_fim_prompt_construction():
    engine = FimCompletionEngine()
    code = "def add(a, b):\n    "
    cursor = len(code)
    prompt = engine.build_prompt(code, cursor_offset=cursor)
    assert "<|fim_prefix|>def add(a, b):\n    <|fim_suffix|><|fim_middle|>" == prompt


def test_fim_prediction_and_caching():
    mock_model_mgr = MagicMock()
    mock_model_mgr.complete.return_value = {
        "status": "success",
        "content": "return a + b"
    }

    engine = FimCompletionEngine(model_manager=mock_model_mgr)
    code = "def add(a, b):\n    "
    
    # First call: hits ModelManager
    res_1 = engine.predict(file_path="math.py", content=code, cursor_offset=len(code))
    assert res_1["status"] == "success"
    assert res_1["completion"] == "return a + b"
    assert mock_model_mgr.complete.call_count == 1

    # Second call within cache window: returns cached completion cleanly without re-calling API
    res_2 = engine.predict(file_path="math.py", content=code, cursor_offset=len(code))
    assert res_2["status"] == "success"
    assert res_2["completion"] == "return a + b"
    assert res_2["cached"] is True
    assert mock_model_mgr.complete.call_count == 1  # Still 1!
