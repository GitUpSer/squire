"""Shared, scoped resource findings for sibling tasks (acceptance item 6).

Scope = the ORIGIN task (session key of the user request). Findings never cross scopes, so
one user's auth state or evidence is never visible to another chat. Only three kinds are
accepted, all safe to share and none purchase- or identity-specific:

* ``auth_required``     — the site demanded a login (siblings stop probing / ask the user once)
* ``site_unreachable``  — DNS/connection failure for the host (siblings skip it)
* ``readonly_evidence`` — a handle (workspace file path) to already-captured read-only
  evidence (page text / screenshot) a sibling may reuse instead of reloading

Anything else (promo codes, cookies, prices, tokens) is refused: those are task-specific or
private and merging them is exactly the bug the acceptance criteria forbid.

``classify_result`` is deliberately NARROW. A finding blocks every sibling's calls to that host
for the TTL, so a false positive costs 30 min of browser access to a whole chat. Earlier it
grepped the whole tool output for ``/signin`` / ``captcha`` — any ordinary page whose text contains
a "Sign in" link (amazon.com, every retailer header) marked the host ``auth_required``. Now only
three signals count, and only for the SAME site as the host being classified:

* a **location** line — the page's current URL as reported by the harness (``page_info()`` prints
  ``{'url': …, 'title': …}``; "redirected to <url>", "URL: <url>") — whose PATH is a login /
  signin / sso / captcha-interstitial path;
* a **title** line (``'title': 'Amazon Sign-In'``, "Title: Robot Check") that names a sign-in,
  verification or captcha page;
* an explicit **error** field (not page text) saying "login required" / "sign in to continue" /
  "captcha".

Body text, link hrefs, menu items ("Hello, sign in") and other hosts' URLs never count.

Automatic browser-call dedupe: NOT provided (consciously deferred). An earlier AST allowlist
treated ``new_tab(<literal>)`` as read-only, which is false (logout/approve/unsubscribe/magic-login/
signed URLs are state-changing; two sessions carry different cookies). Deciding read-only-ness
needs explicit semantics from the browser tool itself, which upstream does not expose. Until then
every browser call runs exactly once and no result is ever replayed to another caller
(``NO_AUTOMATIC_DEDUPE`` is asserted by tests and the policy never consults any probe table).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from .lease import state_dir

SAFE_KINDS = ("auth_required", "site_unreachable", "readonly_evidence")
DEFAULT_TTL_S = 1800.0
NO_AUTOMATIC_DEDUPE = True   # see module docstring; flipping this requires explicit read-only tool semantics

# URL path that IS a login / sign-in / SSO / captcha-interstitial page (anchored at the path start,
# one or more leading segments allowed: /ap/signin, /account/login, /errors/validateCaptcha).
_AUTH_PATH_RX = re.compile(r"^/(?:[A-Za-z0-9_.-]+/){0,3}(?:signin|sign-in|login|log-in|logon|auth|authenticate|sso|"
                           r"validatecaptcha|captcha)(?:[/?#.]|$)", re.I)
# "location" lines: a url key as the harness prints it (dict/JSON repr or `URL: …`), or an explicit
# redirect statement. Anchored to a key / phrase so a bare href in page text does not match.
_LOCATION_RX = re.compile(r"(?:(?:^|[{,\s])['\"]?(?:url|current_url|final_url|location)['\"]?\s*[:=]\s*|"
                          r"\bredirected to\s+|\bnavigated to\s+)['\"]?(https?://[^\s'\"<>)}\],]+)", re.I | re.M)
_TITLE_RX = re.compile(r"(?:^|[{,\s])['\"]?title['\"]?\s*[:=]\s*['\"]?([^'\"\n}]{1,120})", re.I | re.M)
_TITLE_AUTH_RX = re.compile(r"\b(?:sign[ -]?in|log[ -]?in|login|authentication|two[- ]step verification|"
                            r"verification code|robot check|captcha|are you a human)\b", re.I)
_ERROR_AUTH_RX = re.compile(r"login required|sign in to continue|please (?:log|sign) in|captcha", re.I)
_UNREACH_RX = re.compile(r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_(REFUSED|TIMED_OUT|RESET)|ERR_INTERNET_DISCONNECTED|"
                         r"net::ERR_|getaddrinfo failed|Could not resolve host|ECONNREFUSED (?!127\.0\.0\.1)|"
                         r"HTTP 5\d\d for https?://", re.I)


def _same_site(a: str, b: str) -> bool:
    a, b = (a or "").lower().strip("."), (b or "").lower().strip(".")
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _split_result(result) -> tuple[str, str, str]:
    """(output, error, whole_text) — browser_exec returns a JSON object; plain strings are output."""
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    obj = None
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
    if isinstance(obj, dict):
        return str(obj.get("output") or ""), str(obj.get("error") or ""), text
    return text, "", text


def classify_result(url: str, result: str) -> Optional[str]:
    """Finding kind for ``url``'s host given a tool result, or None. See the module docstring for why
    the auth signal is restricted to location / title / error lines on the same site."""
    host = host_of(url)
    output, error, text = _split_result(result)
    if _UNREACH_RX.search(text):
        return "site_unreachable"
    for m in _LOCATION_RX.finditer(output):
        loc = m.group(1)
        if _same_site(host_of(loc), host):
            path = re.sub(r"^[a-z]+://[^/]+", "", loc, flags=re.I) or "/"
            if _AUTH_PATH_RX.search(path):
                return "auth_required"
    for m in _TITLE_RX.finditer(output):
        if _TITLE_AUTH_RX.search(m.group(1)):
            return "auth_required"
    if error and _ERROR_AUTH_RX.search(error):
        return "auth_required"
    return None


def host_of(url: str) -> str:
    m = re.match(r"^\s*(?:[a-z]+://)?([^/\s:?#]+)", url or "", re.I)
    return (m.group(1).lower() if m else (url or "").lower())[:120]


def probe_key(tool_name: str, args: dict) -> None:
    """Always None: no browser call is ever dedupable automatically (see module docstring)."""
    return None


class Findings:
    def __init__(self, scope: str, *, now: Callable[[], float] = time.time, ttl_s: float = DEFAULT_TTL_S):
        self.scope = re.sub(r"[^A-Za-z0-9_.@-]", "_", scope or "unscoped")[:120]
        self.now, self.ttl = now, float(ttl_s)
        d = state_dir() / "findings"
        d.mkdir(parents=True, exist_ok=True)
        self.path: Path = d / f"{self.scope}.json"

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = b""
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                raw += chunk
            try:
                data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except Exception:
                data = {}
            data.setdefault("findings", [])
            data.pop("probes", None)   # legacy in-flight dedupe table: dropped, never consulted
            t = self.now()
            data["findings"] = [f for f in data["findings"] if t - float(f.get("at", 0)) <= self.ttl]
            yield data
            payload = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- findings -------------------------------------------------------------------------
    def record(self, *, host: str, kind: str, detail: str, task_id: str, handle: str = "") -> bool:
        if kind not in SAFE_KINDS or not host:
            return False
        if kind == "readonly_evidence" and not handle:
            return False
        with self._locked() as d:
            d["findings"] = [f for f in d["findings"] if not (f["host"] == host and f["kind"] == kind)]
            d["findings"].append({"host": host, "kind": kind, "detail": str(detail)[:200], "task_id": task_id,
                                  "handle": handle, "at": self.now()})
        return True

    def lookup(self, host: str) -> list[dict]:
        with self._locked() as d:
            return [dict(f) for f in d["findings"] if f["host"] == host]

    def all(self) -> list[dict]:
        with self._locked() as d:
            return [dict(f) for f in d["findings"]]
