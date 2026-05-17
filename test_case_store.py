"""
Helpers for loading test cases.

The UI can update `test_cases.py` via the GitHub Contents API. On machines where
the daemon runs outside a git clone (or `git pull` fails), we still want the
latest dashboard edits to be runnable.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


def _gh_env() -> tuple[str, str, str] | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    path = os.environ.get("GITHUB_FILE_PATH", "test_cases.py").strip() or "test_cases.py"
    if token and repo:
        return token, repo, path
    return None


def _gh_get_file(token: str, repo: str, path: str) -> str:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API GET {path}: {e.code} {e.read().decode(errors='ignore')}") from e
    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return content


def _extract_test_cases(src: str) -> list[dict[str, Any]]:
    match = re.search(r"TEST_CASES\\s*=\\s*(\\[.*\\])", src, re.DOTALL)
    if not match:
        return []
    val = ast.literal_eval(match.group(1))
    if not isinstance(val, list):
        return []
    out: list[dict[str, Any]] = []
    for item in val:
        if isinstance(item, dict):
            out.append(item)
    return out


def load_test_cases() -> list[dict[str, Any]]:
    """
    Load test cases from GitHub if configured, otherwise from local `test_cases.py`.
    """
    gh = _gh_env()
    if gh:
        token, repo, path = gh
        src = _gh_get_file(token, repo, path)
        return _extract_test_cases(src)

    from test_cases import TEST_CASES  # local import to avoid side-effects at module import time

    return list(TEST_CASES)

