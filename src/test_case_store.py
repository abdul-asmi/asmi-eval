"""
Helpers for loading test cases.

The hosted UI stores current test definitions in Supabase. Local Mac runs can
also receive an exact snapshot from the UI run queue, fetch the hosted UI's
current definitions through the daemon endpoint, or fall back to GitHub/local
defaults for development.
"""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _load_dotenv() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env.local")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except Exception:
        pass


_load_dotenv()


def _gh_env() -> tuple[str, str, str] | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    path = os.environ.get("GITHUB_FILE_PATH", "test_cases.py").strip() or "test_cases.py"
    if token and repo:
        return token, repo, path
    return None


def _remote_ui_env() -> tuple[str, str, str] | None:
    url = (
        os.environ.get("REMOTE_UI_URL", "").strip()
        or os.environ.get("RAILWAY_URL", "").strip()
    ).rstrip("/")
    token = os.environ.get("DAEMON_TOKEN", "").strip()
    owner = (
        os.environ.get("DAEMON_OWNER_USER_ID", "").strip()
        or os.environ.get("ASMI_OWNER_USER_ID", "").strip()
    )
    if url and token:
        return url, token, owner
    return None


def _load_remote_ui_test_cases() -> list[dict[str, Any]]:
    env = _remote_ui_env()
    if not env:
        return []
    url, token, owner = env
    req = urllib.request.Request(f"{url}/api/daemon/tests", method="GET")
    req.add_header("X-Daemon-Token", token)
    if owner:
        req.add_header("X-Owner-User-Id", owner)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []
    cases = data.get("test_cases") if isinstance(data, dict) else data
    if not isinstance(cases, list):
        return []
    return [item for item in cases if isinstance(item, dict)]


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
    match = re.search(r"TEST_CASES\s*=\s*(\[.*\])", src, re.DOTALL)
    if not match:
        return []
    val = _literal_eval_test_list(match.group(1))
    if not isinstance(val, list):
        return []
    out: list[dict[str, Any]] = []
    for item in val:
        if isinstance(item, dict):
            out.append(item)
    return out


def _literal_eval_test_list(list_src: str):
    try:
        return ast.literal_eval(list_src)
    except (SyntaxError, ValueError):
        return ast.literal_eval(_escape_raw_newlines_in_strings(list_src))


def _escape_raw_newlines_in_strings(src: str) -> str:
    out = []
    quote = None
    escaped = False

    for ch in src:
        if quote:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == quote:
                out.append(ch)
                quote = None
            elif ch == "\n":
                out.append("\\n")
            else:
                out.append(ch)
            continue

        out.append(ch)
        if ch in {"'", '"'}:
            quote = ch

    return "".join(out)


def _supabase_env() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if url and key:
        return url, key
    return None


def _sb_get(path: str, *, params: dict | None = None) -> tuple[int, Any]:
    env = _supabase_env()
    if not env:
        return 0, None
    url, key = env
    qs = urllib.parse.urlencode(params or {}, doseq=True)
    full_url = f"{url}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(full_url, method="GET")
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    text = raw.decode("utf-8", errors="replace") if raw else ""
    if not text:
        return status, None
    try:
        return status, json.loads(text)
    except Exception:
        return status, text


def _supabase_owner_id() -> str:
    owner = (
        os.environ.get("ASMI_OWNER_USER_ID", "").strip()
        or os.environ.get("DAEMON_OWNER_USER_ID", "").strip()
    )
    if owner:
        return owner
    status, rows = _sb_get(
        "/rest/v1/test_cases",
        params={"select": "owner_user_id", "order": "updated_at.desc", "limit": 1},
    )
    if status < 300 and isinstance(rows, list) and rows and rows[0].get("owner_user_id"):
        return str(rows[0]["owner_user_id"])
    return ""


def _load_supabase_test_cases() -> list[dict[str, Any]]:
    if not _supabase_env():
        return []
    owner = _supabase_owner_id()
    params = {
        "select": "external_id,definition,enabled,category,name,type,updated_at,owner_user_id",
        "order": "external_id.asc",
    }
    if owner:
        params["owner_user_id"] = f"eq.{owner}"
    status, rows = _sb_get("/rest/v1/test_cases", params=params)
    if status >= 300 or not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        definition = row.get("definition")
        tc = dict(definition) if isinstance(definition, dict) else {}
        tc["id"] = row.get("external_id") or tc.get("id")
        for key in ("category", "name", "type"):
            if row.get(key) is not None:
                tc[key] = row.get(key)
        if row.get("enabled") is not None:
            tc["enabled"] = bool(row.get("enabled"))
        if tc.get("id"):
            out.append(tc)

    def sort_key(tc: dict[str, Any]) -> tuple[int, str]:
        try:
            order = int(tc.get("_ui_order"))
        except Exception:
            order = 1_000_000
        return order, str(tc.get("id") or "")

    return sorted(out, key=sort_key)


def load_test_cases() -> list[dict[str, Any]]:
    """
    Load current UI test cases when configured, then GitHub or local defaults.
    """
    snapshot = os.environ.get("ASMI_TEST_CASES_JSON", "").strip()
    if snapshot:
        try:
            data = json.loads(snapshot)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        except Exception:
            pass

    remote_cases = _load_remote_ui_test_cases()
    if remote_cases:
        return remote_cases

    supabase_cases = _load_supabase_test_cases()
    if supabase_cases:
        return supabase_cases

    gh = _gh_env()
    if gh:
        token, repo, path = gh
        src = _gh_get_file(token, repo, path)
        return _extract_test_cases(src)

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_cases.py")
    with open(path, encoding="utf-8") as f:
        return _extract_test_cases(f.read())
