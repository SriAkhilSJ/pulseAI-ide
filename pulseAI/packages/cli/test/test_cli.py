"""
test_cli.py
-----------
TDD Unit Tests for PulseCodeAI Command Line Interface & Paid Setup Engine (`packages/cli`).
Verifies one-command setups, license verification for commercial deployments, and server management.
"""
import pytest
from pathlib import Path
from src.cli_main import pulse_cli


def test_cli_status_command(tmp_path):
    res = pulse_cli(["status"], workspace_root=str(tmp_path))
    assert res["status"] == "success"
    assert "PulseCodeAI Monorepo Engine v2.0" in res["output"]


def test_cli_setup_commercial_license(tmp_path):
    # Test valid commercial license setup
    res_valid = pulse_cli(["setup", "--commercial", "--license", "PULSE-PRO-2026-X89Z"], workspace_root=str(tmp_path))
    assert res_valid["status"] == "success"
    assert "Commercial setup initialized" in res_valid["output"]
    assert (tmp_path / ".pulsecode" / "license.json").exists()

    # Test invalid license rejection
    res_invalid = pulse_cli(["setup", "--commercial", "--license", "BAD-KEY"], workspace_root=str(tmp_path))
    assert res_invalid["status"] == "error"
    assert "Invalid commercial license format" in res_invalid["output"]
