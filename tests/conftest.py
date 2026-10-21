"""
conftest.py — Shared pytest fixtures for vpsbackup test suite.

Provides:
  - Temporary directory tree for isolated tests
  - Mock GitHub API responses
  - Mock config and registry
  - Mock credential file
  - Mock git repository workspace
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Directory fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    """Root of the isolated test filesystem."""
    return tmp_path


@pytest.fixture
def user_home(tmp_root: Path) -> Path:
    """Fake user home directory."""
    home = tmp_root / "home" / "testuser"
    home.mkdir(parents=True)
    return home


@pytest.fixture
def vpsbackup_dirs(tmp_root: Path):
    """Create the vpsbackup data/config/log directory structure."""
    dirs = {
        "config": tmp_root / "etc" / "vpsbackup",
        "data":   tmp_root / "var" / "lib" / "vpsbackup",
        "repos":  tmp_root / "var" / "lib" / "vpsbackup" / "repositories",
        "state":  tmp_root / "var" / "lib" / "vpsbackup" / "state",
        "logs":   tmp_root / "var" / "log" / "vpsbackup",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


@pytest.fixture
def cred_file(vpsbackup_dirs: dict, tmp_root: Path) -> Path:
    """Write a fake GitHub token to a temp credentials file."""
    cred = vpsbackup_dirs["config"] / "credentials"
    cred.write_text("ghp_" + "A" * 36 + "\n")
    cred.chmod(0o600)
    return cred


@pytest.fixture
def config_file(vpsbackup_dirs: dict) -> Path:
    """Write a minimal config.yaml for tests."""
    cfg_path = vpsbackup_dirs["config"] / "config.yaml"
    cfg_path.write_text(
        """
github:
  api_url: https://api.github.com
  private_repos: true
  max_retries: 3
vps:
  hostname: test-vps
  data_dir: /var/lib/vpsbackup
  log_dir: /var/log/vpsbackup
backup:
  debounce_seconds: 1
  scan_interval: 300
  max_file_size_mb: 50
  deletion_policy: keep
  backup_existing_files: true
users:
  min_uid: 1000
  include_root: false
  user_poll_interval: 5
exclusions:
  extensions:
    - .sqlite
    - .db
    - .log
    - .env
    - .pem
    - .key
  directories:
    - node_modules
    - __pycache__
    - .git
  filenames:
    - .env
    - id_rsa
    - id_ed25519
security:
  scan_for_secrets: true
  secret_action: skip
  secret_patterns:
    - 'AKIA[0-9A-Z]{16}'
    - 'ghp_[A-Za-z0-9]{36}'
    - 'github_pat_[A-Za-z0-9_]{82}'
    - 'sk-[A-Za-z0-9]{48}'
logging:
  level: DEBUG
  log_file: /var/log/vpsbackup/vpsbackup.log
"""
    )
    return cfg_path


@pytest.fixture
def registry_path(vpsbackup_dirs: dict) -> Path:
    return vpsbackup_dirs["data"] / "registry.json"


@pytest.fixture
def base_registry(registry_path: Path) -> dict:
    """Write and return a minimal registry."""
    reg = {
        "schema_version": 2,
        "hostname": "test-vps",
        "github_username": "testgithubuser",
        "vps_repo": "test-vps",
        "users": {},
        "created_at": 1700000000,
        "updated_at": 1700000000,
    }
    registry_path.write_text(json.dumps(reg, indent=2))
    return reg


@pytest.fixture
def registered_user_registry(base_registry: dict, registry_path: Path, user_home: Path) -> dict:
    """Registry with one active user registered."""
    reg = dict(base_registry)
    reg["users"] = {
        "testuser": {
            "uid": 1001,
            "home": str(user_home),
            "repo": "test-vps-testuser",
            "status": "active",
            "backup_dirs": [str(user_home)],
            "registered_at": 1700000000,
            "last_backup": None,
        }
    }
    registry_path.write_text(json.dumps(reg, indent=2))
    return reg


# ---------------------------------------------------------------------------
# Mock GitHub API
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_github_api():
    """Patch requests.Session to return controlled GitHub API responses."""
    with patch("requests.Session") as mock_session_cls:
        session = MagicMock()
        mock_session_cls.return_value = session

        def make_response(status_code: int, body: Any) -> MagicMock:
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = body
            resp.raise_for_status.return_value = None
            if status_code >= 400:
                import requests
                resp.raise_for_status.side_effect = requests.HTTPError(
                    f"HTTP {status_code}", response=resp
                )
            return resp

        session.get.return_value = make_response(200, {"login": "testgithubuser", "id": 12345})
        session.post.return_value = make_response(201, {
            "name": "test-vps-testuser",
            "private": True,
            "full_name": "testgithubuser/test-vps-testuser",
            "clone_url": "https://github.com/testgithubuser/test-vps-testuser.git",
        })
        session._make_response = make_response
        yield session


# ---------------------------------------------------------------------------
# Mock git operations
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_git_repo(tmp_root: Path):
    """Create a real bare git repo in tmp for clone operations."""
    import subprocess
    repo_dir = tmp_root / "git_remote" / "test-vps-testuser.git"
    repo_dir.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(repo_dir)], check=True, capture_output=True)
    return repo_dir


@pytest.fixture
def working_dir(tmp_root: Path) -> Path:
    """A working directory for git operations."""
    d = tmp_root / "working" / "test-vps-testuser"
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# Patch helper: override config paths
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_config(config_file: Path, cred_file: Path, vpsbackup_dirs: dict):
    """Patch config module to use test paths."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src import config as cfg

    with patch.multiple(
        "src.config",
        _CONFIG_PATH=str(config_file),
        _CRED_PATH=str(cred_file),
        _DATA_DIR=str(vpsbackup_dirs["data"]),
        _REPOS_DIR=str(vpsbackup_dirs["repos"]),
        _STATE_DIR=str(vpsbackup_dirs["state"]),
    ):
        cfg.load(str(config_file))
        yield cfg
