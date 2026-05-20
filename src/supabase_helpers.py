import base64
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request

import jwt


class SupabaseError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    return v if v is not None else default


SUPABASE_URL = _env("SUPABASE_URL").rstrip("/")
SUPABASE_ANON_KEY = _env("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = _env("SUPABASE_SERVICE_ROLE_KEY")


def _require_supabase():
    if not SUPABASE_URL or not SUPABASE_ANON_KEY or not SUPABASE_SERVICE_ROLE_KEY:
        raise SupabaseError(
            "Missing SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY environment variables"
        )


_JWKS_CACHE = {"ts": 0.0, "jwks": None}


def _fetch_json(url: str, headers: dict | None = None, timeout: float = 10.0):
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _get_jwks() -> dict:
    _require_supabase()
    now = time.time()
    if _JWKS_CACHE["jwks"] and (now - _JWKS_CACHE["ts"]) < 3600:
        return _JWKS_CACHE["jwks"]
    jwks = _fetch_json(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json", headers={"apikey": SUPABASE_ANON_KEY})
    _JWKS_CACHE["jwks"] = jwks
    _JWKS_CACHE["ts"] = now
    return jwks


def verify_supabase_jwt(token: str) -> dict:
    """
    Verify Supabase JWT signature and return decoded claims.
    Notes:
      - Supabase issues JWTs signed by its project keys; JWKS provides verification.
      - We validate signature + exp; we do not enforce aud beyond basic presence.
    """
    if not token:
        raise SupabaseError("Missing token")
    try:
        jwks = _get_jwks()
        unverified = jwt.get_unverified_header(token)
        algorithm = unverified.get("alg", "RS256")
        kid = unverified.get("kid")
        keys = (jwks or {}).get("keys") or []
        key = next((k for k in keys if k.get("kid") == kid), None)
        if key:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
            claims = jwt.decode(
                token,
                public_key,
                algorithms=[algorithm],
                options={"verify_aud": False},
            )
        else:
            raise SupabaseError("Unknown signing key")
    except Exception:
        # Fallback for projects that still issue non-RSA access tokens.
        status, user = _sb_request(
            "GET",
            "/auth/v1/user",
            bearer=token,
            apikey=SUPABASE_ANON_KEY,
        )
        if status >= 300 or not isinstance(user, dict):
            raise SupabaseError("Invalid token")
        claims = {
            "sub": user.get("id"),
            "email": user.get("email"),
            "role": user.get("role"),
            "user_metadata": user.get("user_metadata") or {},
        }
    sub = claims.get("sub")
    if not sub:
        raise SupabaseError("Token missing sub")
    return claims


def _sb_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body=None,
    bearer: str | None = None,
    apikey: str | None = None,
    extra_headers: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict | list | str | None]:
    _require_supabase()
    url = f"{SUPABASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    data = None
    headers: dict[str, str] = {}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if apikey:
        headers["apikey"] = apikey
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items() if v is not None})
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
    txt = raw.decode("utf-8") if raw else ""
    if not txt:
        return status, None
    try:
        return status, json.loads(txt)
    except Exception:
        return status, txt


def sb_user_get(path: str, *, token: str, params: dict | None = None):
    return _sb_request("GET", path, params=params, bearer=token, apikey=SUPABASE_ANON_KEY)


def sb_user_post(path: str, *, token: str, json_body=None, params: dict | None = None):
    return _sb_request("POST", path, params=params, json_body=json_body, bearer=token, apikey=SUPABASE_ANON_KEY)


def sb_user_patch(path: str, *, token: str, json_body=None, params: dict | None = None):
    return _sb_request("PATCH", path, params=params, json_body=json_body, bearer=token, apikey=SUPABASE_ANON_KEY)


def sb_user_post_ex(path: str, *, token: str, json_body=None, params: dict | None = None, headers: dict | None = None):
    return _sb_request(
        "POST", path, params=params, json_body=json_body, bearer=token, apikey=SUPABASE_ANON_KEY, extra_headers=headers
    )


def sb_user_patch_ex(path: str, *, token: str, json_body=None, params: dict | None = None, headers: dict | None = None):
    return _sb_request(
        "PATCH", path, params=params, json_body=json_body, bearer=token, apikey=SUPABASE_ANON_KEY, extra_headers=headers
    )


def sb_service_get(path: str, *, params: dict | None = None):
    return _sb_request("GET", path, params=params, bearer=SUPABASE_SERVICE_ROLE_KEY, apikey=SUPABASE_SERVICE_ROLE_KEY)


def sb_service_post(path: str, *, json_body=None, params: dict | None = None):
    return _sb_request(
        "POST", path, params=params, json_body=json_body, bearer=SUPABASE_SERVICE_ROLE_KEY, apikey=SUPABASE_SERVICE_ROLE_KEY
    )


def sb_service_post_ex(path: str, *, json_body=None, params: dict | None = None, headers: dict | None = None):
    return _sb_request(
        "POST",
        path,
        params=params,
        json_body=json_body,
        bearer=SUPABASE_SERVICE_ROLE_KEY,
        apikey=SUPABASE_SERVICE_ROLE_KEY,
        extra_headers=headers,
    )


def sb_service_patch(path: str, *, json_body=None, params: dict | None = None):
    return _sb_request(
        "PATCH", path, params=params, json_body=json_body, bearer=SUPABASE_SERVICE_ROLE_KEY, apikey=SUPABASE_SERVICE_ROLE_KEY
    )


def storage_upload_bytes(*, bucket: str, path: str, content: bytes, content_type: str) -> None:
    """
    Upload bytes to Supabase Storage using the service role key.
    Uses POST /storage/v1/object/<bucket>/<path> with raw body.
    """
    _require_supabase()
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{urllib.parse.quote(path)}"
    req = urllib.request.Request(url, data=content, method="POST")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    req.add_header("Content-Type", content_type or "application/octet-stream")
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        raise SupabaseError(f"Storage upload failed ({e.code}): {e.read().decode('utf-8', 'ignore')}") from e


def storage_create_signed_url(*, bucket: str, path: str, expires_in: int = 600) -> str:
    _require_supabase()
    status, data = sb_service_post(
        f"/storage/v1/object/sign/{bucket}/{urllib.parse.quote(path)}",
        json_body={"expiresIn": int(expires_in)},
    )
    if status >= 300:
        raise SupabaseError(f"Failed to sign URL: {data}")
    signed = (data or {}).get("signedURL") or (data or {}).get("signedUrl")
    if not signed:
        raise SupabaseError("Missing signedURL in response")
    # signedURL is usually a path; normalize to full URL for convenience
    if signed.startswith("http"):
        return signed
    return f"{SUPABASE_URL}{signed}"


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def iso_utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def bearer_from_request_headers(headers) -> str:
    auth = headers.get("Authorization", "")
    if not auth or not auth.lower().startswith("bearer "):
        return ""
    return auth.split(" ", 1)[1].strip()
