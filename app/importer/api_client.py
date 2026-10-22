"""HTTP client for the MCSR Ranked API with process-wide pacing and retries.

Limit: 500 requests per 10 minutes per IP. We keep >= 1.25 s between calls (at most 480 per 10 minutes: 4%
headroom, because the API's window needn't line up with ours), and as a second line of defence follow the
`RateLimit` header (wait for the reset when nothing remains) and HTTP 429 `Retry-After`.
"""
import logging
import re
import threading
import time

import requests

from app.config import Config

log = logging.getLogger(__name__)

MIN_INTERVAL = 1.25   # 600 s / 1.25 s = 480 calls per 10 minutes, below the API's 500
TIMEOUT = 15
MAX_RETRIES = 3
# A call made while a visitor waits for the page (leaderboard, Load timeline): fail fast instead of 15 s x 4.
PAGE_TIMEOUT, PAGE_RETRIES = 5, 0

_lock = threading.Lock()
_next_allowed = 0.0
_RATE = re.compile(r"r=(\d+);\s*t=(\d+)")


class ApiError(Exception):
    """Request failed after retries, or the response was not usable."""


class ApiNotFound(ApiError):
    """HTTP 400 with status 'error' (the API's 'not found' / bad parameter response)."""


def _wait_turn():
    global _next_allowed
    with _lock:
        now = time.monotonic()
        if now < _next_allowed:
            time.sleep(_next_allowed - now)
        _next_allowed = time.monotonic() + MIN_INTERVAL


def _respect_headers(resp):
    global _next_allowed
    m = _RATE.search(resp.headers.get("RateLimit", ""))
    if m and int(m.group(1)) == 0:
        with _lock:
            _next_allowed = max(_next_allowed, time.monotonic() + int(m.group(2)) + 1)


class ApiClient:
    def __init__(self, base=None, session=None, timeout=TIMEOUT, retries=MAX_RETRIES):
        """timeout/retries: imports use the defaults; a page that calls the API while the visitor waits
        passes smaller ones, so a slow API can't hang the page for a minute."""
        self.base = (base or Config.MCSR_API_BASE).rstrip("/")
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "INF2003-Group18-MCSR-Tracker"
        self.timeout, self.retries = timeout, retries

    @classmethod
    def for_page(cls):
        """A client for a call made while a visitor waits (PAGE_TIMEOUT, PAGE_RETRIES)."""
        return cls(timeout=PAGE_TIMEOUT, retries=PAGE_RETRIES)

    def get(self, path, params=None):
        """Return the `data` member of a successful response."""
        last_error = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(2 ** attempt)
            _wait_turn()
            try:
                resp = self.session.get(self.base + path, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                log.warning("network error on %s (attempt %d): %s", path, attempt + 1, exc)
                continue
            _respect_headers(resp)
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = ApiError(f"HTTP {resp.status_code} on {path}")
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(int(retry_after))
                log.warning("HTTP %s on %s (attempt %d)", resp.status_code, path, attempt + 1)
                continue
            try:
                body = resp.json()
            except ValueError as exc:
                raise ApiError(f"non-JSON response from {path}") from exc
            if resp.status_code == 400:
                raise ApiNotFound(f"{path}: {body.get('data')}")
            if resp.status_code != 200 or body.get("status") != "success":
                raise ApiError(f"HTTP {resp.status_code} on {path}: {body.get('data')}")
            return body["data"]
        raise ApiError(f"giving up on {path}: {last_error}")

    def user(self, identifier):
        return self.get(f"/users/{identifier}")

    def user_seasons(self, identifier):
        return self.get(f"/users/{identifier}/seasons")

    def user_matches(self, identifier, season, before=None, count=100):
        params = {"season": season, "count": count}
        if before is not None:
            params["before"] = before
        return self.get(f"/users/{identifier}/matches", params)

    def match(self, match_id):
        return self.get(f"/matches/{int(match_id)}")

    def leaderboard(self, season=None):
        return self.get("/leaderboard", {"season": season} if season else None)
