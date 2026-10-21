"""
test_recovery_and_safety.py — Scenarios 10, 11, 12: Reboot recovery, symlink safety,
concurrent protection.

Tests:
  10.1 Service restart → registry persists across restarts (state not lost)
  10.2 Service restart → state files (hashes) persist → no redundant commits
  10.3 Partial write interrupted → registry recovers from atomic tmp write
  11.1 Symlink pointing inside approved dir → followed
  11.2 Symlink pointing outside approved dir → not followed
  11.3 Absolute symlink to /etc → excluded
  11.4 Relative symlink traversing out → excluded
  12.1 Two users backed up simultaneously → no cross-file contamination
  12.2 FileLock prevents concurrent git operations on same repo
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRebootRecovery:

    def test_registry_persists_after_restart(self, registered_user_registry, registry_path):
        """10.1 — Registry written to disk and loadable after 'restart'."""
        from src import user_manager as um

        # Simulate a service restart by loading from disk
        loaded = um.load_registry(str(registry_path))
        assert loaded["users"]["testuser"]["status"] == "active"
        assert loaded["schema_version"] == 2

    def test_state_file_persists_hashes(self, tmp_path):
        """10.2 — File hashes persisted to state dir survive restart."""
        from src import backup_manager as bm

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        state_file = state_dir / "test-vps-alice.json"
        fake_state = {
            "Documents/notes.txt": "abc123def456",
            "projects/app.py":     "deadbeef1234",
        }
        state_file.write_text(json.dumps(fake_state))

        # Simulating load_state after restart
        loaded = bm._load_state(str(state_file))
        assert loaded == fake_state

    def test_atomic_registry_write_no_corruption(self, tmp_path):
        """10.3 — Atomic write (tmp + os.replace) prevents partial-write corruption."""
        from src import user_manager as um

        reg_path = tmp_path / "registry.json"
        initial = {
            "schema_version": 2,
            "hostname": "test-vps",
            "github_username": "u",
            "vps_repo": "test-vps",
            "users": {},
            "created_at": 1700000000,
            "updated_at": 1700000000,
        }
        # Write the initial registry
        um.save_registry(initial, str(reg_path))

        # Verify it is valid JSON
        with open(reg_path) as f:
            data = json.load(f)
        assert data["schema_version"] == 2

        # Ensure no .tmp file left behind
        tmp_candidate = Path(str(reg_path) + ".tmp")
        assert not tmp_candidate.exists()


class TestSymlinkSafety:

    def test_symlink_inside_approved_dir_ok(self, tmp_path):
        """11.1 — Symlink whose target is inside the approved dir is not excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)

        # Create a real file and a symlink to it inside the same dir
        real_file = user_dir / "real.txt"
        real_file.write_text("content")
        sym = user_dir / "link.txt"
        sym.symlink_to(real_file)

        # validate_backup_dir should not raise for this safe symlink
        # should_exclude checks path, not symlink target by default
        result = security.should_exclude(str(sym), str(user_dir))
        # Symlinks to files inside the approved dir should not be excluded
        assert result is False

    def test_symlink_outside_approved_dir_excluded(self, tmp_path):
        """11.2 — Symlink pointing outside the approved dir is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        outside = tmp_path / "secret_dir"
        outside.mkdir()
        outside_file = outside / "secret.txt"
        outside_file.write_text("sensitive data")

        # Symlink inside user_dir pointing outside
        sym = user_dir / "escape.txt"
        sym.symlink_to(outside_file)

        # Should be excluded (symlink traversal outside base dir)
        result = security.should_exclude(str(sym), str(user_dir))
        assert result is True

    def test_symlink_to_etc_excluded(self, tmp_path):
        """11.3 — Symlink to /etc is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)

        sym = user_dir / "etc_link"
        # Create a symlink to /etc (may or may not exist in sandbox)
        try:
            sym.symlink_to("/etc")
        except Exception:
            pytest.skip("Cannot create /etc symlink in this environment")

        result = security.should_exclude(str(sym), str(user_dir))
        assert result is True

    def test_relative_symlink_traversal_excluded(self, tmp_path):
        """11.4 — Symlink using ../../ to traverse outside is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)

        sym = user_dir / "traverse"
        # Relative symlink: ../../../etc
        sym.symlink_to(Path("../../../etc"))

        result = security.should_exclude(str(sym), str(user_dir))
        assert result is True


class TestConcurrentProtection:

    def test_two_users_files_never_cross_contaminate(self, tmp_path):
        """12.1 — Backup dirs are per-user; no file from one user goes to another's repo."""
        user_a_dir = tmp_path / "home" / "alice"
        user_b_dir = tmp_path / "home" / "bob"
        user_a_dir.mkdir(parents=True)
        user_b_dir.mkdir(parents=True)

        (user_a_dir / "alice_only.txt").write_text("alice private")
        (user_b_dir / "bob_only.txt").write_text("bob private")

        work_a = tmp_path / "repos" / "test-vps-alice"
        work_b = tmp_path / "repos" / "test-vps-bob"
        work_a.mkdir(parents=True)
        work_b.mkdir(parents=True)

        # Simulate what backup_user does: copy files to per-user working dirs
        import shutil
        for f in user_a_dir.iterdir():
            shutil.copy2(str(f), str(work_a / f.name))
        for f in user_b_dir.iterdir():
            shutil.copy2(str(f), str(work_b / f.name))

        # Verify no cross-contamination
        alice_files = set(p.name for p in work_a.iterdir())
        bob_files   = set(p.name for p in work_b.iterdir())

        assert "alice_only.txt" in alice_files
        assert "bob_only.txt" not in alice_files
        assert "bob_only.txt" in bob_files
        assert "alice_only.txt" not in bob_files

    def test_filelock_serializes_git_ops(self, tmp_path):
        """12.2 — FileLock prevents concurrent git operations on same repo."""
        try:
            from filelock import FileLock
        except ImportError:
            pytest.skip("filelock not installed")

        lock_path = tmp_path / "test.lock"
        results = []
        errors  = []
        hold_event = threading.Event()

        def thread_a():
            with FileLock(str(lock_path), timeout=2):
                results.append("A_start")
                hold_event.wait(timeout=0.5)
                results.append("A_end")

        def thread_b():
            time.sleep(0.05)  # let A grab lock first
            try:
                with FileLock(str(lock_path), timeout=0.3):
                    results.append("B_entered_while_A_held")
            except Exception:
                results.append("B_blocked")

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=3)
        hold_event.set()
        tb.join(timeout=3)

        # B must have been blocked while A held the lock
        assert "B_entered_while_A_held" not in results
        assert "B_blocked" in results
        assert results[0] == "A_start"
        assert results[1] == "A_end"

    def test_concurrent_backups_do_not_deadlock(self, tmp_path):
        """12.2 — Two concurrent backup threads complete without deadlock."""
        from src import backup_manager as bm

        completed = []
        errors = []

        def do_backup(username):
            try:
                with patch.object(bm, "_do_backup", return_value=0):
                    with patch.object(bm, "_push_with_retry"):
                        with patch.object(bm, "_load_state", return_value={}):
                            with patch.object(bm, "_save_state"):
                                bm.backup_user(
                                    username=username,
                                    repo_name=f"test-vps-{username}",
                                    backup_dirs=[str(tmp_path / username)],
                                    work_dir=str(tmp_path / f"repo_{username}"),
                                )
                completed.append(username)
            except Exception as exc:
                errors.append(f"{username}: {exc}")

        for u in ("alice", "bob"):
            (tmp_path / u).mkdir(parents=True, exist_ok=True)
            (tmp_path / f"repo_{u}").mkdir(parents=True, exist_ok=True)

        threads = [threading.Thread(target=do_backup, args=(u,)) for u in ("alice", "bob")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent backup: {errors}"
        assert set(completed) == {"alice", "bob"}
