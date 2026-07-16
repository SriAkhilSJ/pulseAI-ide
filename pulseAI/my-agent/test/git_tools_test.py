"""
Direct, live test of git_tools.py -- run against a THROWAWAY repo under
test/git_tools_sandbox_workdir (never the real project's .git state).

Run with: PYTHONPATH=/home/user/my-agent python3 test/git_tools_test.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import git_tools  # noqa: E402

SANDBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git_tools_sandbox_workdir")


def _reset_sandbox():
    if os.path.exists(SANDBOX):
        shutil.rmtree(SANDBOX)
    os.makedirs(SANDBOX)


def test_git_init_and_status_on_empty_repo():
    _reset_sandbox()
    msg = git_tools.git_init(SANDBOX)
    print("git_init:", msg)
    assert "Initialized" in msg
    # Re-running should be a no-op, not an error.
    msg2 = git_tools.git_init(SANDBOX)
    assert "already" in msg2
    print("PASS: git_init works and is idempotent")


def test_status_before_repo_exists_raises_clear_error():
    empty_dir = os.path.join(SANDBOX, "not_a_repo_yet")
    os.makedirs(empty_dir)
    try:
        git_tools.git_status(empty_dir)
        print("FAIL: expected GitError")
        sys.exit(1)
    except git_tools.GitError as e:
        print("PASS: git_status on a non-repo raises a clear GitError:", e)


def test_commit_normal_file_works():
    import subprocess
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=SANDBOX, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=SANDBOX, check=True)

    with open(os.path.join(SANDBOX, "app.py"), "w") as f:
        f.write("print('hello')\n")

    status = git_tools.git_status(SANDBOX)
    print("status before commit:\n", status)
    assert "app.py" in status
    assert "SENSITIVE" not in status

    result = git_tools.git_commit("initial commit", repo_path=SANDBOX)
    print("git_commit:", result)
    assert "Committed" in result

    log = git_tools.git_log(repo_path=SANDBOX)
    print("git_log:\n", log)
    assert "initial commit" in log
    print("PASS: normal commit works end-to-end")


def test_real_bug_repro_modified_tracked_env_file_is_still_caught():
    """
    THE bug found in the original proposal: a .env file that's already
    TRACKED (committed earlier by mistake) and then MODIFIED does not show
    up in `repo.untracked_files` at all. A safety check that only scans
    untracked_files would miss this entirely. Confirm git_tools catches it
    anyway via the is_sensitive_path check on the FULL changed-paths union.
    """
    import subprocess
    # Simulate a repo that had .env committed by mistake in the past.
    env_path = os.path.join(SANDBOX, ".env")
    with open(env_path, "w") as f:
        f.write("SECRET=old\n")
    subprocess.run(["git", "add", "-f", ".env"], cwd=SANDBOX, check=True)  # -f: bypass gitignore for this repro
    subprocess.run(["git", "commit", "-m", "oops committed env (simulating a past mistake)"], cwd=SANDBOX, check=True)

    # Now modify it -- this is the exact case that showed as INVISIBLE to
    # untracked_files-only detection in the live repro during design.
    with open(env_path, "w") as f:
        f.write("SECRET=new\n")

    import git
    repo = git.Repo(SANDBOX)
    assert ".env" not in repo.untracked_files, "sanity check: .env should NOT be untracked (it's already committed)"

    status = git_tools.git_status(SANDBOX)
    print("status with modified tracked .env:\n", status)
    assert "SENSITIVE" in status, "git_status should flag the modified tracked .env as sensitive"

    result = git_tools.git_commit("try to sneak in the env change", repo_path=SANDBOX)
    print("git_commit result:", result)
    assert "ERROR" in result and "sensitive" in result.lower(), \
        "git_commit MUST refuse when a modified TRACKED file is sensitive, not just untracked ones"
    print("PASS: modified tracked .env is caught (the exact gap in the original proposal)")


def test_real_bug_repro_untracked_secret_file_shown_in_diff_and_blocked():
    """
    The second half of the original bug: untracked files don't appear in
    `git diff` / `index.diff(None)` output at all. Confirm git_diff surfaces
    them explicitly instead of silently omitting them, AND git_commit
    refuses to include a brand-new untracked secrets.json.
    """
    secrets_path = os.path.join(SANDBOX, "secrets.json")
    with open(secrets_path, "w") as f:
        f.write('{"api_key": "abc123"}\n')

    import git
    repo = git.Repo(SANDBOX)
    assert "secrets.json" in repo.untracked_files

    diff_output = git_tools.git_diff(repo_path=SANDBOX)
    print("git_diff output:\n", diff_output)
    # The critical assertion: the actual SECRET CONTENT must never appear,
    # not even redacted-looking-but-actually-present. This is the exact bug
    # this module's own test suite caught in an earlier draft: a note saying
    # "excluded" was printed ALONGSIDE the full real diff text underneath it.
    assert "abc123" not in diff_output, "the real secret VALUE must never appear in the diff output"
    assert "SECRET=old" not in diff_output and "SECRET=new" not in diff_output, \
        "the .env secret's real content must never appear in the diff output"
    assert "secrets.json" in diff_output, "the untracked secret file's PATH should still be visible (just not its content)"
    assert "excluded" in diff_output.lower()

    result = git_tools.git_commit("add secrets", repo_path=SANDBOX)
    print("git_commit result:", result)
    assert "ERROR" in result and "secrets.json" in result
    print("PASS: untracked secrets.json is caught before commit, and its content never leaked via git_diff")

    os.remove(secrets_path)  # clean up so later tests have a clean tree
    # Also clean up the .env left dirty by the previous test, and commit
    # that cleanup, so subsequent branch tests start from a genuinely clean
    # tree (matching what git_create_branch actually requires).
    import subprocess
    env_path = os.path.join(SANDBOX, ".env")
    if os.path.exists(env_path):
        subprocess.run(["git", "checkout", "--", ".env"], cwd=SANDBOX, check=True)



def test_branch_refuses_with_dirty_tree():
    with open(os.path.join(SANDBOX, "wip.txt"), "w") as f:
        f.write("work in progress\n")
    result = git_tools.git_create_branch("feature/test", repo_path=SANDBOX)
    print("git_create_branch (dirty tree):", result)
    assert "ERROR" in result
    os.remove(os.path.join(SANDBOX, "wip.txt"))
    print("PASS: branch creation refuses on a dirty tree")


def test_branch_succeeds_on_clean_tree():
    result = git_tools.git_create_branch("feature/clean", repo_path=SANDBOX)
    print("git_create_branch (clean tree):", result)
    assert "Created and switched" in result
    print("PASS: branch creation succeeds on a clean tree")


if __name__ == "__main__":
    test_git_init_and_status_on_empty_repo()
    test_status_before_repo_exists_raises_clear_error()
    test_commit_normal_file_works()
    test_real_bug_repro_modified_tracked_env_file_is_still_caught()
    test_real_bug_repro_untracked_secret_file_shown_in_diff_and_blocked()
    test_branch_refuses_with_dirty_tree()
    test_branch_succeeds_on_clean_tree()
    print("\nALL TESTS PASSED")
