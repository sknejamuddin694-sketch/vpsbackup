"""
test_repo_management.py — Scenarios 2 & 3: Repository creation and reuse.

Tests:
  2.1  New user → private repo created on GitHub with correct name
  2.2  Repo name format: <hostname>-<username>
  2.3  Existing repo → not created again (idempotent)
  2.4  422 on creation (race condition) → treated as success
  2.5  Repo access failure → logged, user skipped gracefully
  3.1  Correct per-user repo mapping (no cross-user contamination)
  3.2  VPS-level repo name matches hostname
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import requests

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRepoCreation:

    def test_new_user_repo_name_format(self):
        """2.2 — Repo name is <hostname>-<username>."""
        from src import user_manager as um

        with patch("src.config.get_hostname", return_value="prod-vps"):
            repo_name = um.get_repo_name_for_user("alice")

        assert repo_name == "prod-vps-alice"

    def test_vps_repo_name_is_hostname(self):
        """3.2 — VPS-level repo name equals the sanitized hostname."""
        from src import user_manager as um

        with patch("src.config.get_hostname", return_value="my-server"):
            vps_repo = um.get_vps_repo_name()

        assert vps_repo == "my-server"

    def test_create_private_repo_success(self, mock_github_api):
        """2.1 — create_private_repo sends POST with private=true."""
        from src import github_manager as gh

        with patch("src.github_manager._get_session", return_value=mock_github_api):
            result = gh.create_private_repo("test-vps-alice", description="test")

        call_kwargs = mock_github_api.post.call_args
        body = call_kwargs[1].get("json", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})
        assert body.get("private") is True
        assert body.get("name") == "test-vps-alice"

    def test_create_repo_idempotent_existing(self, mock_github_api):
        """2.3 — If repo exists (GET returns 200), no POST is made."""
        from src import github_manager as gh

        # Make GET /repos/... return 200 (repo already exists)
        exist_resp = MagicMock()
        exist_resp.status_code = 200
        exist_resp.json.return_value = {"name": "test-vps-alice"}

        def fake_get(url, **kwargs):
            if "/repos/" in url:
                return exist_resp
            return mock_github_api.get.return_value

        mock_github_api.get.side_effect = fake_get

        with patch("src.github_manager._get_session", return_value=mock_github_api):
            with patch("src.github_manager.repo_exists", return_value=True):
                result = gh.create_private_repo("test-vps-alice")

        # POST should not be called
        mock_github_api.post.assert_not_called()

    def test_create_repo_422_race_condition(self, mock_github_api):
        """2.4 — 422 on repo creation (race condition) → treated as success."""
        from src import github_manager as gh

        # GET says no repo, POST returns 422
        no_repo_resp = MagicMock()
        no_repo_resp.status_code = 404
        mock_github_api.get.return_value = no_repo_resp

        conflict_resp = MagicMock()
        conflict_resp.status_code = 422
        conflict_resp.json.return_value = {"message": "Repository creation failed: already exists"}
        mock_github_api.post.return_value = conflict_resp

        with patch("src.github_manager._get_session", return_value=mock_github_api):
            with patch("src.github_manager.repo_exists", return_value=False):
                # Should not raise
                try:
                    gh.create_private_repo("test-vps-alice")
                except Exception as exc:
                    pytest.fail(f"422 should be handled gracefully, got: {exc}")

    def test_repo_mapping_no_cross_contamination(self, registered_user_registry):
        """3.1 — Each user maps to exactly their own repo, not another user's."""
        from src import user_manager as um

        reg = registered_user_registry
        # Add a second user
        reg["users"]["alice"] = {
            "uid": 1002, "home": "/home/alice",
            "repo": "test-vps-alice", "status": "active",
            "backup_dirs": ["/home/alice"],
            "registered_at": 1700000000, "last_backup": None,
        }

        for username, udata in reg["users"].items():
            entry = um.get_user_entry(reg, username)
            assert entry["repo"] == udata["repo"]
            # Repo name contains the username
            assert username in entry["repo"]

    def test_token_never_in_clone_url(self):
        """2.5 — Clone URL returned by get_repo_clone_url never contains token."""
        from src import github_manager as gh

        with patch("src.config.get_github_token", return_value="ghp_" + "S" * 36):
            url = gh.get_repo_clone_url("testgithubuser", "test-vps-alice")

        assert "ghp_" not in url
        # HTTPS clone URL without embedded credentials
        assert url.startswith("https://github.com/")
        assert "@" not in url
