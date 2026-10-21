"""
github_manager.py — All GitHub API interactions.

Responsibilities:
  - Validate the GitHub token.
  - Identify the authenticated GitHub username.
  - Create or locate private repositories.
  - Handle rate limits and transient network failures.
  - Never expose the token in logs or process listings.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from . import config

logger = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        # Set token via header — never in the URL
        token = config.get_github_token()
        _SESSION.headers.update(
            {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        _SESSION.timeout = 30
    return _SESSION


def _invalidate_session() -> None:
    """Called after a token change to force re-authentication."""
    global _SESSION
    _SESSION = None


def _api(
    method: str,
    path: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
    retry_on: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> requests.Response:
    """
    Execute a GitHub API call with automatic retry and rate-limit handling.

    Args:
        method: HTTP method ("GET", "POST", …)
        path:   API path, e.g. "/user" or "/user/repos"
        json:   Optional request body.
        params: Optional query parameters.
        retry_on: HTTP status codes that should trigger a retry.

    Returns:
        Response object (status is NOT checked; caller decides).
    """
    base_url = config.get("github", "api_url", "https://api.github.com")
    url = base_url.rstrip("/") + path
    max_retries: int = config.get("github", "max_retries", 5)
    backoff_base: float = float(config.get("github", "retry_backoff", 2))

    sess = _get_session()
    for attempt in range(max_retries + 1):
        try:
            resp = sess.request(method, url, json=json, params=params)
        except requests.exceptions.ConnectionError as exc:
            if attempt >= max_retries:
                raise
            wait = backoff_base ** attempt
            logger.warning("GitHub API connection error (attempt %d/%d): %s — retrying in %.1fs",
                           attempt + 1, max_retries + 1, exc, wait)
            time.sleep(wait)
            continue
        except requests.exceptions.Timeout as exc:
            if attempt >= max_retries:
                raise
            wait = backoff_base ** attempt
            logger.warning("GitHub API timeout (attempt %d/%d): %s — retrying in %.1fs",
                           attempt + 1, max_retries + 1, exc, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            # Respect Retry-After header if present
            retry_after = int(resp.headers.get("Retry-After", backoff_base ** attempt))
            logger.warning("GitHub rate limit hit — waiting %ds", retry_after)
            time.sleep(retry_after)
            continue

        # Check x-ratelimit-remaining for proactive throttling
        remaining = int(resp.headers.get("X-RateLimit-Remaining", 9999))
        if remaining < 10:
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            sleep_secs = max(0, reset_ts - time.time()) + 1
            logger.info("GitHub rate limit nearly exhausted (%d remaining) — sleeping %.0fs",
                        remaining, sleep_secs)
            time.sleep(sleep_secs)

        if resp.status_code in retry_on and attempt < max_retries:
            wait = backoff_base ** attempt
            logger.warning("GitHub API returned %d (attempt %d/%d) — retrying in %.1fs",
                           resp.status_code, attempt + 1, max_retries + 1, wait)
            time.sleep(wait)
            continue

        return resp

    # Should not be reached
    raise RuntimeError("GitHub API call failed after maximum retries.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_token() -> tuple[bool, str]:
    """
    Validate the configured GitHub token.

    Returns:
        (True, username)  on success
        (False, error_message) on failure
    """
    try:
        resp = _api("GET", "/user")
    except Exception as exc:
        return False, f"Network error: {exc}"

    if resp.status_code == 200:
        data = resp.json()
        username = data.get("login", "")
        logger.info("GitHub token validated for user: %s", username)
        return True, username
    elif resp.status_code == 401:
        return False, "Invalid or expired GitHub token."
    else:
        return False, f"Unexpected response: HTTP {resp.status_code}"


def get_github_username() -> str:
    """Return the authenticated GitHub username, raising on failure."""
    ok, result = validate_token()
    if not ok:
        raise RuntimeError(f"GitHub authentication failed: {result}")
    return result


def repo_exists(username: str, repo_name: str) -> bool:
    """Return True if the repository already exists under *username*."""
    resp = _api("GET", f"/repos/{username}/{repo_name}")
    if resp.status_code == 200:
        return True
    elif resp.status_code == 404:
        return False
    else:
        resp.raise_for_status()
        return False  # unreachable


def create_private_repo(repo_name: str, description: str = "") -> dict:
    """
    Create a private repository under the authenticated account.

    Returns the repository data dict from the GitHub API.
    Raises RuntimeError on failure.
    Never creates duplicates.
    """
    username = get_github_username()
    safe_name = config.sanitize_repo_name(repo_name)

    if repo_exists(username, safe_name):
        logger.info("Repository %s/%s already exists — reusing.", username, safe_name)
        resp = _api("GET", f"/repos/{username}/{safe_name}")
        resp.raise_for_status()
        return resp.json()

    payload = {
        "name": safe_name,
        "description": description or f"VPS backup: {safe_name}",
        "private": True,
        "auto_init": True,
    }
    resp = _api("POST", "/user/repos", json=payload)

    if resp.status_code == 422:
        # Could be a race condition — another instance already created it
        if repo_exists(username, safe_name):
            logger.warning("Repository %s/%s appeared between check and create — reusing.",
                           username, safe_name)
            resp2 = _api("GET", f"/repos/{username}/{safe_name}")
            resp2.raise_for_status()
            return resp2.json()
        error_msg = resp.json().get("message", "Unprocessable Entity")
        raise RuntimeError(f"Failed to create repository '{safe_name}': {error_msg}")

    if not resp.ok:
        raise RuntimeError(
            f"Failed to create repository '{safe_name}': "
            f"HTTP {resp.status_code} — {resp.text[:200]}"
        )

    data = resp.json()
    logger.info("Created private repository: %s/%s", username, safe_name)
    return data


def get_repo_clone_url(username: str, repo_name: str) -> str:
    """Return the HTTPS clone URL using a token credential helper, not embedded in URL."""
    return f"https://github.com/{username}/{repo_name}.git"


def list_user_repos(prefix: str = "") -> list[dict]:
    """List repositories for the authenticated account, optionally filtered by prefix."""
    results = []
    page = 1
    while True:
        resp = _api(
            "GET",
            "/user/repos",
            params={"per_page": 100, "page": page, "type": "owner"},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for repo in batch:
            name = repo.get("name", "")
            if not prefix or name.startswith(prefix):
                results.append(repo)
        page += 1
        if len(batch) < 100:
            break
    return results


def check_push_permissions(username: str, repo_name: str) -> bool:
    """Return True if the token can push to the repository."""
    resp = _api("GET", f"/repos/{username}/{repo_name}")
    if not resp.ok:
        return False
    perms = resp.json().get("permissions", {})
    return perms.get("push", False)
