"""
Direct test of a real false-positive bug found while running git_status
against this project's OWN real files: .env.example (a deliberately blank,
safe-to-commit template) was flagged as sensitive purely because ".env" is
a substring of ".env.example".

Run with: PYTHONPATH=/home/user/my-agent python3 test/sensitive_path_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


def test_env_example_is_not_sensitive():
    assert not tools.is_sensitive_path(".env.example"), ".env.example should be safe to commit"
    assert not tools.is_sensitive_path("path/to/.env.example")
    assert not tools.is_sensitive_path(".env.sample")
    assert not tools.is_sensitive_path(".env.template")
    print("PASS: .env.example/.sample/.template are correctly NOT flagged as sensitive")


def test_real_env_variants_still_caught():
    assert tools.is_sensitive_path(".env")
    assert tools.is_sensitive_path("test/.env")
    assert tools.is_sensitive_path(".env.local")
    assert tools.is_sensitive_path(".env.production")
    print("PASS: real .env / .env.local / .env.production are still correctly flagged")


def test_other_sensitive_tokens_unaffected():
    assert tools.is_sensitive_path("id_rsa")
    assert tools.is_sensitive_path(".ssh/id_rsa")
    assert tools.is_sensitive_path("secrets.json")
    assert tools.is_sensitive_path("key.pem")
    assert not tools.is_sensitive_path("app.py")
    assert not tools.is_sensitive_path("README.md")
    print("PASS: all other sensitive-path checks unaffected by this fix")


def test_edge_case_something_else_env_example_looking():
    # A file that both starts with .env AND matches another real sensitive
    # token should STILL be caught -- the exemption is narrow, not blanket.
    assert tools.is_sensitive_path(".env.example.pem"), \
        "a file ending in a real sensitive suffix should still be caught even if it starts with .env"
    print("PASS: narrow exemption doesn't accidentally widen to other sensitive suffixes")


if __name__ == "__main__":
    test_env_example_is_not_sensitive()
    test_real_env_variants_still_caught()
    test_other_sensitive_tokens_unaffected()
    test_edge_case_something_else_env_example_looking()
    print("\nALL TESTS PASSED")
