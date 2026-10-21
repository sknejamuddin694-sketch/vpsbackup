"""
test_credential_security.py — Scenario 13: Credential security.

Tests:
  13.1 GitHub token never appears in log output
  13.2 GitHub token never embedded in git remote URL
  13.3 Redaction strips ghp_ tokens from arbitrary log lines
  13.4 Redaction strips github_pat_ tokens
  13.5 Redaction strips HTTPS credential URLs (https://user:token@github.com)
  13.6 Token never appears in any git command's argv
  13.7 Credentials file is mode 600 (root-only read)
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from io import StringIO

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


FAKE_PAT = "ghp_" + "S" * 36
FAKE_FINE_GRAINED = "github_pat_" + "A" * 82
FAKE_CREDENTIAL_URL = f"https://myuser:{FAKE_PAT}@github.com/user/repo.git"


class TestCredentialSecurity:

    # ------------------------------------------------------------------
    # 13.3 — Redaction of ghp_ tokens
    # ------------------------------------------------------------------

    def test_redact_ghp_token_from_log_line(self):
        """13.3 — _redact_token_from_text strips ghp_ tokens."""
        from src.cli import _redact_token_from_text

        line = f'Using token {FAKE_PAT} for authentication'
        redacted = _redact_token_from_text(line)

        assert FAKE_PAT not in redacted
        assert "ghp_" not in redacted or "***" in redacted or "<redacted>" in redacted.lower()

    # ------------------------------------------------------------------
    # 13.4 — Redaction of fine-grained tokens
    # ------------------------------------------------------------------

    def test_redact_fine_grained_pat(self):
        """13.4 — _redact_token_from_text strips github_pat_ tokens."""
        from src.cli import _redact_token_from_text

        line = f'Token: {FAKE_FINE_GRAINED}'
        redacted = _redact_token_from_text(line)

        assert FAKE_FINE_GRAINED not in redacted

    # ------------------------------------------------------------------
    # 13.5 — Redaction of HTTPS credential URLs
    # ------------------------------------------------------------------

    def test_redact_https_credential_url(self):
        """13.5 — HTTPS URLs with embedded tokens are redacted."""
        from src.cli import _redact_token_from_text

        line = f'Cloning from {FAKE_CREDENTIAL_URL}'
        redacted = _redact_token_from_text(line)

        assert FAKE_PAT not in redacted
        assert "myuser:" not in redacted or "@" not in redacted.split("https://")[-1]

    # ------------------------------------------------------------------
    # 13.2 — Token never in git remote URL
    # ------------------------------------------------------------------

    def test_token_not_embedded_in_remote_url(self, tmp_path):
        """13.2 — get_repo_clone_url returns HTTPS URL without token."""
        from src import github_manager as gh

        with patch("src.config.get_github_token", return_value=FAKE_PAT):
            url = gh.get_repo_clone_url("someuser", "test-vps-alice")

        assert FAKE_PAT not in url
        assert "ghp_" not in url
        assert "@" not in url  # no embedded user:pass@host

    # ------------------------------------------------------------------
    # 13.6 — Token never in git command argv
    # ------------------------------------------------------------------

    def test_token_never_in_git_argv(self):
        """13.6 — _run_git never receives the token as a command argument."""
        from src import backup_manager as bm

        captured_args = []

        def record_cmd(cmd, **kwargs):
            captured_args.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=record_cmd):
            with patch("src.config.get_github_token", return_value=FAKE_PAT):
                # Simulate a typical git push call
                try:
                    bm._run_git(
                        ["git", "push", "origin", "main"],
                        cwd="/fake/dir",
                    )
                except Exception:
                    pass  # git might not exist; we only check args

        for args in captured_args:
            for arg in args:
                assert FAKE_PAT not in str(arg), f"Token found in git argv: {arg}"
                assert "ghp_" not in str(arg)

    # ------------------------------------------------------------------
    # 13.1 — Token never in log output
    # ------------------------------------------------------------------

    def test_token_never_logged(self):
        """13.1 — Token is never written to log records."""
        # Set up an in-memory log handler
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger("vpsbackup")
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)

        try:
            from src import github_manager as gh

            # Trigger a function that might inadvertently log the token
            with patch("src.config.get_github_token", return_value=FAKE_PAT):
                with patch("requests.Session") as mock_session:
                    session = MagicMock()
                    mock_session.return_value = session
                    resp = MagicMock()
                    resp.status_code = 200
                    resp.json.return_value = {"login": "testuser"}
                    session.get.return_value = resp

                    try:
                        gh.validate_token()
                    except Exception:
                        pass  # we care about logs, not return value

            log_output = stream.getvalue()
            assert FAKE_PAT not in log_output, "Token leaked into log output"

        finally:
            root_logger.removeHandler(handler)

    # ------------------------------------------------------------------
    # 13.7 — Credentials file mode 600
    # ------------------------------------------------------------------

    def test_credentials_file_mode_600(self, cred_file):
        """13.7 — Credentials file has mode 600 (rw for owner only)."""
        mode = oct(os.stat(str(cred_file)).st_mode)
        # Mode should end in 600
        file_perm = os.stat(str(cred_file)).st_mode & 0o777
        assert file_perm == 0o600, f"Expected 600, got {oct(file_perm)}"

    # ------------------------------------------------------------------
    # 13.x — Token read from file, not from env or argv
    # ------------------------------------------------------------------

    def test_token_read_from_credentials_file(self, cred_file):
        """13.x — get_github_token reads from credentials file, not environment."""
        from src import config as cfg

        with patch("src.config._CRED_PATH", str(cred_file)):
            token = cfg.get_github_token()

        expected = "ghp_" + "A" * 36
        assert token.strip() == expected

    def test_token_not_from_environment_variable(self, cred_file):
        """13.x — GITHUB_TOKEN env var is not used (credentials file is authoritative)."""
        from src import config as cfg

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_envtoken" + "X" * 30}):
            with patch("src.config._CRED_PATH", str(cred_file)):
                token = cfg.get_github_token()

        # Should NOT return the env var value
        assert "envtoken" not in token
        assert "ghp_" + "A" * 36 == token.strip()
