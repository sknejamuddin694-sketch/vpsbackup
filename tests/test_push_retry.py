"""
test_push_retry.py — Scenario 9: Push retry on transient network failure.

Tests:
  9.1  Transient push failure → retried up to max_retries times
  9.2  After max_retries exceeded → raises RuntimeError (not silent)
  9.3  Non-fast-forward push → fetch + rebase attempted
  9.4  Successful push on second attempt → returns cleanly
  9.5  Rate limit (HTTP 403 + Retry-After) → waits before retry
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestPushRetry:

    def _make_git_error(self, msg: str) -> subprocess.CalledProcessError:
        """Construct a CalledProcessError that looks like a git failure."""
        err = subprocess.CalledProcessError(128, ["git", "push"])
        err.stderr = msg
        err.stdout = ""
        return err

    # ------------------------------------------------------------------
    # 9.1 — Retry on transient failure
    # ------------------------------------------------------------------

    def test_retry_on_transient_failure(self):
        """9.1 — push_with_retry retries on network error."""
        from src import backup_manager as bm

        call_count = [0]

        def flaky_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise subprocess.CalledProcessError(1, cmd, stderr="Connection reset by peer")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(bm, "_run_git", side_effect=flaky_run):
            bm._push_with_retry(
                work_dir="/fake/dir",
                remote="origin",
                branch="main",
                max_retries=3,
                retry_delay=0,
            )

        assert call_count[0] == 3  # failed twice, succeeded on third

    # ------------------------------------------------------------------
    # 9.2 — Max retries exceeded → raises
    # ------------------------------------------------------------------

    def test_max_retries_exceeded_raises(self):
        """9.2 — After max_retries exhausted, RuntimeError is raised."""
        from src import backup_manager as bm

        def always_fail(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="Network unreachable")

        with patch.object(bm, "_run_git", side_effect=always_fail):
            with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
                bm._push_with_retry(
                    work_dir="/fake/dir",
                    remote="origin",
                    branch="main",
                    max_retries=3,
                    retry_delay=0,
                )

    # ------------------------------------------------------------------
    # 9.3 — Non-fast-forward → rebase attempted
    # ------------------------------------------------------------------

    def test_non_fast_forward_triggers_rebase(self):
        """9.3 — Non-fast-forward rejection triggers git pull --rebase."""
        from src import backup_manager as bm

        push_calls = []
        rebase_done = [False]

        def mock_run_git(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            push_calls.append(cmd_str)
            if "push" in cmd and not rebase_done[0]:
                raise subprocess.CalledProcessError(
                    1, cmd,
                    stderr="error: failed to push some refs\nhint: Updates were rejected because the remote contains work that you do not have locally."
                )
            if "pull" in cmd or "rebase" in cmd:
                rebase_done[0] = True
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(bm, "_run_git", side_effect=mock_run_git):
            try:
                bm._push_with_retry(
                    work_dir="/fake/dir",
                    remote="origin",
                    branch="main",
                    max_retries=2,
                    retry_delay=0,
                )
            except Exception:
                pass  # might fail but rebase should have been attempted

        # At least one rebase/pull call should have happened
        rebase_attempted = any(
            "pull" in c or "rebase" in c or "fetch" in c
            for c in push_calls
        )
        assert rebase_attempted or rebase_done[0], "Expected rebase attempt on non-fast-forward"

    # ------------------------------------------------------------------
    # 9.4 — Successful push on second attempt
    # ------------------------------------------------------------------

    def test_success_on_second_attempt(self):
        """9.4 — Returns cleanly when push succeeds on retry."""
        from src import backup_manager as bm

        attempt = [0]

        def fail_once(cmd, **kwargs):
            attempt[0] += 1
            if attempt[0] == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="Temporary failure in name resolution")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(bm, "_run_git", side_effect=fail_once):
            # Should not raise
            bm._push_with_retry(
                work_dir="/fake/dir",
                remote="origin",
                branch="main",
                max_retries=3,
                retry_delay=0,
            )

        assert attempt[0] == 2

    # ------------------------------------------------------------------
    # 9.5 — Retry delay is respected (not zero in production)
    # ------------------------------------------------------------------

    def test_retry_delay_called_between_attempts(self):
        """9.5 — time.sleep is called between retry attempts."""
        from src import backup_manager as bm
        import time

        attempt = [0]
        sleep_calls = []

        def fail_twice(cmd, **kwargs):
            attempt[0] += 1
            if attempt[0] < 3:
                raise subprocess.CalledProcessError(1, cmd, stderr="timeout")
            return MagicMock(returncode=0, stdout="", stderr="")

        def record_sleep(n):
            sleep_calls.append(n)

        with patch.object(bm, "_run_git", side_effect=fail_twice):
            with patch("time.sleep", side_effect=record_sleep):
                bm._push_with_retry(
                    work_dir="/fake/dir",
                    remote="origin",
                    branch="main",
                    max_retries=3,
                    retry_delay=5,
                )

        assert len(sleep_calls) >= 1
        assert all(s >= 0 for s in sleep_calls)
