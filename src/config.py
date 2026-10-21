"""
config.py — Load, validate, and expose configuration.
All other modules import from here.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = "/etc/vpsbackup/config.yaml"
_CREDENTIALS_PATH = "/etc/vpsbackup/credentials"

_config: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def load(path: str = _DEFAULT_CONFIG_PATH) -> None:
    """Load YAML config from *path* into the module-level dict."""
    global _config
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with p.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("Config file must be a YAML mapping.")
    _config = data
    logger.debug("Config loaded from %s", path)


def get(section: str, key: str, default: Any = None) -> Any:
    """Return config[section][key], or *default* if missing."""
    return _config.get(section, {}).get(key, default)


def get_section(section: str) -> dict[str, Any]:
    return dict(_config.get(section, {}))


def get_github_token() -> str:
    """
    Read the GitHub PAT from the credentials file.
    Never returns the token as a string in logs.
    Raises RuntimeError if the file is missing or empty.
    """
    cred_path = Path(_CREDENTIALS_PATH)
    if not cred_path.exists():
        raise RuntimeError(
            f"Credentials file not found: {_CREDENTIALS_PATH}\n"
            "Run the installer to configure the GitHub token."
        )
    # Ensure only root can read it
    stat = cred_path.stat()
    if stat.st_mode & 0o077:
        logger.warning(
            "Credentials file %s has loose permissions (mode %s). "
            "Restrict with: chmod 600 %s",
            _CREDENTIALS_PATH,
            oct(stat.st_mode),
            _CREDENTIALS_PATH,
        )
    token = cred_path.read_text().strip()
    if not token:
        raise RuntimeError("Credentials file is empty.")
    return token


def get_hostname() -> str:
    """Return VPS hostname from config or detect via `hostname` command."""
    hostname = get("vps", "hostname", "").strip()
    if hostname:
        return hostname
    import subprocess
    try:
        hostname = subprocess.check_output(
            ["hostname"], text=True, timeout=5
        ).strip()
    except Exception:
        hostname = os.uname().nodename
    return hostname


def get_registry_path() -> Path:
    return Path(get("vps", "registry_path", "/var/lib/vpsbackup/registry.json"))


def get_repos_path() -> Path:
    return Path(get("vps", "repos_path", "/var/lib/vpsbackup/repositories"))


def get_state_path() -> Path:
    return Path(get("vps", "state_path", "/var/lib/vpsbackup/state"))


def get_exclusion_patterns() -> dict[str, list[str]]:
    """Return the exclusion dict (directories, files, exact_names)."""
    return {
        "directories": get("exclusions", "directories") or [],
        "files": get("exclusions", "files") or [],
        "exact_names": get("exclusions", "exact_names") or [],
    }


def get_secret_patterns() -> list[re.Pattern]:
    raw = get("security", "secret_patterns") or []
    compiled = []
    for pat in raw:
        try:
            compiled.append(re.compile(pat))
        except re.error as exc:
            logger.warning("Invalid secret pattern %r: %s", pat, exc)
    return compiled


def sanitize_repo_name(name: str) -> str:
    """
    Convert a string to a safe GitHub repository name.
    GitHub allows: letters, numbers, hyphens, underscores, dots.
    """
    # Replace illegal characters with hyphens
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "-", name)
    # Collapse multiple hyphens
    safe = re.sub(r"-{2,}", "-", safe)
    # Remove leading/trailing hyphens and dots
    safe = safe.strip("-.")
    # Truncate to GitHub's 100-char limit
    return safe[:100]
