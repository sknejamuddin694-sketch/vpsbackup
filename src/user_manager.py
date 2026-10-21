"""
user_manager.py — Detect Linux users and manage the registry.

Responsibilities:
  - Poll /etc/passwd to detect newly created users.
  - Maintain a JSON registry of users, their directories, and repo mappings.
  - Never touch /etc/shadow or any password data.
  - Handle user deletion gracefully (mark inactive; preserve repo).
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import time
from pathlib import Path
from threading import Lock
from typing import Optional

from filelock import FileLock, Timeout

from . import config

logger = logging.getLogger(__name__)

_registry_lock = Lock()  # in-process concurrency
_IGNORED_SHELLS = {"/usr/sbin/nologin", "/bin/false", "/sbin/nologin"}


# ---------------------------------------------------------------------------
# Registry schema
# {
#   "schema_version": 2,
#   "vps_hostname": "ip-123-082-029",
#   "github_username": "myuser",
#   "vps_repo": "ip-123-082-029",
#   "users": {
#     "test": {
#       "uid": 1001,
#       "home": "/home/test",
#       "repo": "ip-123-082-029-test",
#       "backup_dirs": ["/home/test"],
#       "status": "active",      # active | inactive | pending
#       "created_at": 1700000000,
#       "last_backup": null
#     }
#   }
# }
# ---------------------------------------------------------------------------

def _get_registry_path() -> Path:
    return config.get_registry_path()


def _load_registry_raw() -> dict:
    path = _get_registry_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read registry at %s: %s", path, exc)
        return {}


def _save_registry_raw(data: dict) -> None:
    path = _get_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_registry() -> dict:
    with _registry_lock:
        return _load_registry_raw()


def save_registry(data: dict) -> None:
    lock_path = str(_get_registry_path()) + ".lock"
    try:
        with FileLock(lock_path, timeout=10), _registry_lock:
            _save_registry_raw(data)
    except Timeout:
        logger.error("Timed out acquiring registry lock — skipping save")


def init_registry(hostname: str, github_username: str, vps_repo: str) -> dict:
    """
    Create or migrate the registry file.
    Safe to call if registry already exists (idempotent).
    """
    with _registry_lock:
        existing = _load_registry_raw()
        if existing.get("schema_version") == 2:
            return existing
        data: dict = {
            "schema_version": 2,
            "vps_hostname": hostname,
            "github_username": github_username,
            "vps_repo": vps_repo,
            "users": existing.get("users", {}),
        }
        _save_registry_raw(data)
        return data


# ---------------------------------------------------------------------------
# passwd parsing
# ---------------------------------------------------------------------------

def _read_passwd_entries() -> dict[str, dict]:
    """
    Return {username: {uid, gid, home, shell}} for all accounts in /etc/passwd.
    Uses the `pwd` module (C library) for reliable parsing.
    """
    entries = {}
    try:
        for pw in pwd.getpwall():
            entries[pw.pw_name] = {
                "uid": pw.pw_uid,
                "gid": pw.pw_gid,
                "home": pw.pw_dir,
                "shell": pw.pw_shell,
            }
    except Exception as exc:
        logger.error("Failed to read passwd: %s", exc)
    return entries


def _is_regular_user(username: str, entry: dict) -> bool:
    """
    Decide whether an account should be tracked.
    """
    cfg_users = config.get_section("users")
    min_uid: int = cfg_users.get("min_uid", 1000)
    include_root: bool = cfg_users.get("include_root", True)
    ignored: list[str] = cfg_users.get("ignored_users", [])

    uid = entry["uid"]
    # Ignore explicitly listed users
    if username in ignored:
        return False
    # Root
    if uid == 0:
        return include_root
    # System accounts
    if uid < min_uid:
        return False
    # Accounts with no-login shells are typically service accounts
    if entry.get("shell") in _IGNORED_SHELLS:
        return False
    # Home directory must exist or be /home/*
    home = entry.get("home", "")
    if not home or home == "/nonexistent":
        return False
    return True


# ---------------------------------------------------------------------------
# User detection and registration
# ---------------------------------------------------------------------------

def get_known_usernames(registry: dict) -> set[str]:
    return set(registry.get("users", {}).keys())


def detect_new_users(registry: dict) -> list[dict]:
    """
    Compare current /etc/passwd against the registry.

    Returns a list of new user dicts: [{username, uid, home, shell}, …]
    """
    passwd = _read_passwd_entries()
    known = get_known_usernames(registry)
    new_users = []
    for uname, entry in passwd.items():
        if uname in known:
            continue
        if not _is_regular_user(uname, entry):
            continue
        new_users.append({"username": uname, **entry})
    return new_users


def detect_deleted_users(registry: dict) -> list[str]:
    """
    Detect users in the registry who no longer exist in /etc/passwd.

    Returns list of usernames that have been removed.
    """
    passwd = _read_passwd_entries()
    deleted = []
    for uname, udata in registry.get("users", {}).items():
        if udata.get("status") == "inactive":
            continue
        if uname not in passwd:
            deleted.append(uname)
    return deleted


def register_user(
    registry: dict,
    username: str,
    uid: int,
    home: str,
    repo_name: str,
    backup_dirs: Optional[list[str]] = None,
) -> dict:
    """
    Add a user entry to the registry.
    Returns the updated registry.
    """
    if backup_dirs is None:
        backup_dirs = [home]
    if "users" not in registry:
        registry["users"] = {}
    registry["users"][username] = {
        "uid": uid,
        "home": home,
        "repo": repo_name,
        "backup_dirs": backup_dirs,
        "status": "active",
        "created_at": int(time.time()),
        "last_backup": None,
    }
    return registry


def mark_user_inactive(registry: dict, username: str) -> dict:
    """Mark a deleted user's registry entry as inactive (never deletes their repo)."""
    users = registry.get("users", {})
    if username in users:
        users[username]["status"] = "inactive"
        users[username]["deactivated_at"] = int(time.time())
        logger.info("Marked user %s as inactive in registry", username)
    return registry


def update_last_backup(registry: dict, username: str) -> dict:
    users = registry.get("users", {})
    if username in users:
        users[username]["last_backup"] = int(time.time())
    return registry


def get_user_entry(registry: dict, username: str) -> Optional[dict]:
    return registry.get("users", {}).get(username)


def get_active_users(registry: dict) -> dict[str, dict]:
    """Return only active user entries."""
    return {
        uname: udata
        for uname, udata in registry.get("users", {}).items()
        if udata.get("status") == "active"
    }


def get_user_backup_dirs(registry: dict, username: str) -> list[Path]:
    """Return validated backup directories for a user."""
    entry = get_user_entry(registry, username)
    if not entry:
        return []
    dirs = []
    for d in entry.get("backup_dirs", []):
        p = Path(d)
        if p.exists() and p.is_dir():
            dirs.append(p)
        else:
            logger.warning("Backup dir %s for user %s does not exist", d, username)
    return dirs


def get_repo_name_for_user(username: str) -> str:
    """
    Build the repository name for a given Linux user.
    Format: <VPS_HOSTNAME>-<username>
    """
    hostname = config.get_hostname()
    raw = f"{hostname}-{username}"
    return config.sanitize_repo_name(raw)


def get_vps_repo_name() -> str:
    """Repository name for the VPS itself (root-level)."""
    hostname = config.get_hostname()
    return config.sanitize_repo_name(hostname)


# ---------------------------------------------------------------------------
# User poll loop (runs in a background thread)
# ---------------------------------------------------------------------------

class UserWatcher:
    """
    Periodically polls /etc/passwd for new or deleted users and calls
    registered callbacks.
    """

    def __init__(
        self,
        on_new_user=None,
        on_deleted_user=None,
    ):
        self._on_new_user = on_new_user
        self._on_deleted_user = on_deleted_user
        self._running = False

    def start(self) -> None:
        import threading
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True, name="UserWatcher")
        t.start()

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        interval = config.get("users", "user_poll_interval", 30)
        logger.info("UserWatcher started — polling every %ds", interval)
        while self._running:
            try:
                registry = load_registry()

                # --- New users ---
                for user_info in detect_new_users(registry):
                    logger.info("New user detected: %s (uid=%d)", user_info["username"], user_info["uid"])
                    if self._on_new_user:
                        try:
                            self._on_new_user(user_info)
                        except Exception as exc:
                            logger.exception("Error handling new user %s: %s",
                                             user_info["username"], exc)

                # --- Deleted users ---
                for uname in detect_deleted_users(registry):
                    logger.info("User deleted from system: %s", uname)
                    if self._on_deleted_user:
                        try:
                            self._on_deleted_user(uname)
                        except Exception as exc:
                            logger.exception("Error handling deleted user %s: %s", uname, exc)

            except Exception as exc:
                logger.error("UserWatcher poll error: %s", exc)

            time.sleep(interval)
        logger.info("UserWatcher stopped")
