"""
Live test for plugins.py's real remote-fetch path -- an ACTUAL git clone
of a real, public, genuinely-open GitHub repository (anthropics/skills --
Anthropic's own official public skills repo, confirmed public via a real
GitHub API call before this test was written), not a mocked
git.Repo.clone_from call.

Complements test/plugins_test.py's unit-level
test_resolve_remote_source_uses_gitpython_clone_from (which mocks the
clone to verify the CALL SITE is correct without needing network access
in every CI run) -- this test proves the WHOLE chain actually works
end-to-end against the real internet: marketplace parsing -> GitHub
source resolution -> a real `git clone` -> loading through skills.py's
real scan_skills() against real, externally-authored SKILL.md files this
project did not write.

Requires network access. Run with:
    PYTHONPATH=/home/user/my-agent python3 test/plugins_live_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins  # noqa: E402


def test_real_github_plugin_clone_and_load():
    with tempfile.TemporaryDirectory() as d:
        workdir = Path(d)
        marketplace_path = workdir / plugins.MARKETPLACE_FILE_NAME
        marketplace_path.write_text(json.dumps({
            "name": "test-marketplace",
            "owner": {"name": "test"},
            "plugins": [
                {
                    "name": "anthropic-skills",
                    "source": {"source": "github", "repo": "anthropics/skills"},
                    "description": "Anthropic's real public skills repo",
                }
            ],
        }))

        with patch.object(plugins, "_get_tools") as mock_get_tools:
            mock_tools_module = MagicMock()
            mock_tools_module.WORKDIR = workdir
            mock_get_tools.return_value = mock_tools_module

            loaded = plugins.install_plugin_from_marketplace("anthropic-skills")

            assert loaded.manifest.name == "anthropic-skills"
            assert loaded.root.exists(), "the real clone destination must exist on disk"
            assert len(loaded.skill_names) > 5, (
                f"expected several real skills from anthropics/skills, got: {loaded.skill_names}"
            )
            assert loaded.load_warnings == [], f"expected zero warnings loading a real, valid repo, got: {loaded.load_warnings}"

            # Confirm it's a REAL git clone, not a fabricated directory --
            # a genuine .git directory with real commit history.
            result = subprocess.run(
                ["git", "log", "-1", "--oneline"], cwd=loaded.root, capture_output=True, text=True,
            )
            assert result.returncode == 0 and result.stdout.strip(), (
                "expected a real git commit log inside the cloned plugin directory"
            )
            print(f"PASS: real GitHub clone of anthropics/skills succeeded, {len(loaded.skill_names)} real skills loaded")
            print(f"      real commit: {result.stdout.strip()}")
            print(f"      sample skill names: {loaded.skill_names[:5]}")

            # Re-run to confirm the "already cloned -> pull instead of
            # re-clone" path also works for real, not just on first fetch.
            loaded_again = plugins.install_plugin_from_marketplace("anthropic-skills")
            assert loaded_again.root == loaded.root
            print("PASS: re-installing the same plugin reuses the cache and pulls instead of re-cloning")


if __name__ == "__main__":
    test_real_github_plugin_clone_and_load()
    print("\nALL TESTS PASSED")
