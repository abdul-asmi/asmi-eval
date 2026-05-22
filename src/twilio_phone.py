"""
Twilio call control helpers for call_eval hard caps.

This module is intentionally lightweight and uses direct REST calls so it
works without adding a new SDK dependency.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests


def _sid() -> str:
    return os.environ.get("TWILIO_ACCOUNT_SID", "").strip()


def _token() -> str:
    return os.environ.get("TWILIO_AUTH_TOKEN", "").strip()


def twilio_configured() -> bool:
    return bool(_sid() and _token())


def _base_url() -> str:
    sid = _sid()
    if not sid:
        raise RuntimeError("TWILIO_ACCOUNT_SID is not set")
    return f"https://api.twilio.com/2010-04-01/Accounts/{sid}"


def _normalize_e164(number: str) -> str:
    raw = (number or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"+{digits}" if digits else ""


def _to_dt(value) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        # Twilio timestamps are often RFC2822, e.g. "Thu, 22 May 2026 08:12:34 +0000"
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def list_calls(status: str, to_number: str = "", page_size: int = 50) -> list[dict]:
    """
    List calls by status, optionally filtered by destination number.
    """
    url = f"{_base_url()}/Calls.json"
    params = {
        "Status": status,
        "PageSize": max(1, min(int(page_size or 50), 100)),
    }
    to_e164 = _normalize_e164(to_number)
    if to_e164:
        params["To"] = to_e164
    resp = requests.get(url, params=params, auth=(_sid(), _token()), timeout=15)
    resp.raise_for_status()
    data = resp.json() or {}
    calls = data.get("calls") or []
    return calls if isinstance(calls, list) else []


def newest_active_call_sid(to_number: str, started_after: datetime) -> str:
    """
    Return the newest active call SID for a Twilio destination number.
    Searches in-progress first, then ringing/queued as fallback.
    """
    started_after = _to_dt(started_after).astimezone(timezone.utc)
    candidates = []
    for st in ("in-progress", "ringing", "queued"):
        for c in list_calls(st, to_number=to_number, page_size=50):
            created = _to_dt(c.get("date_created") or c.get("start_time") or c.get("date_updated")).astimezone(timezone.utc)
            if created >= started_after:
                candidates.append((created, c))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]
    return str(best.get("sid") or "").strip()


def end_call(call_sid: str) -> bool:
    """
    End a call by updating status=completed.
    Returns True if Twilio accepted the update.
    """
    sid = (call_sid or "").strip()
    if not sid:
        return False
    url = f"{_base_url()}/Calls/{sid}.json"
    resp = requests.post(url, data={"Status": "completed"}, auth=(_sid(), _token()), timeout=15)
    if resp.status_code >= 300:
        return False
    data = resp.json() or {}
    return str(data.get("status") or "").lower() in {"completed", "canceled", "in-progress", "ringing", "queued"}
