"""
test_user_detection.py — Scenario 1: New Linux user detection via /etc/passwd polling.

Tests:
  1.1  New user appears in /etc/passwd → detected within poll interval
  1.2  System users (uid < 1000) are NOT detected
  1.3  Existing users (already in registry) are NOT re-onboarded
  1.4  Deleted users (removed from /etc/passwd) are detected
  1.5  /etc/passwd read is reliable (uses pwd module, not text parsing)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class FakePwdEntry:
    """Mimics a struct_passwd tuple from pwd.getpwent()."""
    def __init__(self, pw_name, pw_uid, pw_gid, pw_dir, pw_shell):
        self.pw_name = pw_name
        self.pw_uid = pw_uid
        self.pw_gid = pw_gid
        self.pw_dir = pw_dir
        self.pw_shell = pw_shell


SYSTEM_ENTRIES = [
    FakePwdEntry("root", 0, 0, "/root", "/bin/bash"),
    FakePwdEntry("daemon", 1, 1, "/usr/sbin", "/usr/sbin/nologin"),
    FakePwdEntry("www-data", 33, 33, "/var/www", "/usr/sbin/nologin"),
    FakePwdEntry("nobody", 65534, 65534, "/nonexistent", "/usr/sbin/nologin"),
]

REGULAR_ENTRIES = [
    FakePwdEntry("alice", 1000, 1000, "/home/alice", "/bin/bash"),
    FakePwdEntry("bob", 1001, 1001, "/home/bob", "/bin/bash"),
]


class TestUserDetection:

    def test_new_regular_user_detected(self, base_registry, registry_path):
        """1.1 — A new regular user (uid >= 1000) is detected on the next poll."""
        from src import user_manager as um

        with patch("src.user_manager.pwd") as mock_pwd:
            mock_pwd.getpwall.return_value = SYSTEM_ENTRIES + REGULAR_ENTRIES

            registry = json.loads(registry_path.read_text())
            new_users = um.detect_new_users(registry)

        names = [u["username"] for u in new_users]
        assert "alice" in names
        assert "bob" in names

    def test_system_users_excluded(self, base_registry, registry_path):
        """1.2 — System users (uid < 1000) are never detected as new users."""
        from src import user_manager as um

        with patch("src.user_manager.pwd") as mock_pwd:
            mock_pwd.getpwall.return_value = SYSTEM_ENTRIES

            registry = json.loads(registry_path.read_text())
            new_users = um.detect_new_users(registry)

        assert new_users == []

    def test_existing_users_not_re_onboarded(self, registered_user_registry, registry_path):
        """1.3 — Users already in the registry are not returned as new."""
        from src import user_manager as um

        existing_entries = [
            FakePwdEntry("testuser", 1001, 1001, "/home/testuser", "/bin/bash"),
            FakePwdEntry("newuser", 1002, 1002, "/home/newuser", "/bin/bash"),
        ]
        with patch("src.user_manager.pwd") as mock_pwd:
            mock_pwd.getpwall.return_value = SYSTEM_ENTRIES + existing_entries

            new_users = um.detect_new_users(registered_user_registry)

        names = [u["username"] for u in new_users]
        assert "testuser" not in names  # already registered
        assert "newuser" in names        # genuinely new

    def test_deleted_user_detected(self, registered_user_registry, registry_path):
        """1.4 — A user removed from /etc/passwd is detected as deleted."""
        from src import user_manager as um

        # testuser was registered; /etc/passwd now has no regular users
        with patch("src.user_manager.pwd") as mock_pwd:
            mock_pwd.getpwall.return_value = SYSTEM_ENTRIES

            deleted = um.detect_deleted_users(registered_user_registry)

        assert "testuser" in deleted

    def test_nologin_shell_excluded(self, base_registry, registry_path):
        """1.5 — Users with nologin or false shells are excluded."""
        from src import user_manager as um

        nologin_entries = [
            FakePwdEntry("svcuser", 1005, 1005, "/home/svcuser", "/usr/sbin/nologin"),
            FakePwdEntry("falseuser", 1006, 1006, "/home/falseuser", "/bin/false"),
            FakePwdEntry("realuser", 1007, 1007, "/home/realuser", "/bin/bash"),
        ]
        with patch("src.user_manager.pwd") as mock_pwd:
            mock_pwd.getpwall.return_value = SYSTEM_ENTRIES + nologin_entries

            new_users = um.detect_new_users(base_registry)

        names = [u["username"] for u in new_users]
        assert "svcuser" not in names
        assert "falseuser" not in names
        assert "realuser" in names

    def test_uid_boundary_min_1000(self, base_registry):
        """1.6 — uid=999 is excluded; uid=1000 is included."""
        from src import user_manager as um

        boundary_entries = [
            FakePwdEntry("almost", 999, 999, "/home/almost", "/bin/bash"),
            FakePwdEntry("exactly", 1000, 1000, "/home/exactly", "/bin/bash"),
        ]
        with patch("src.user_manager.pwd") as mock_pwd:
            mock_pwd.getpwall.return_value = boundary_entries

            new_users = um.detect_new_users(base_registry)

        names = [u["username"] for u in new_users]
        assert "almost" not in names
        assert "exactly" in names
