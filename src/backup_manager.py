"""
backup_manager.py — Git-based backup operations per user.

Responsibilities:
  - Maintain a local Git working directory for each user.
  - Copy eligible files from the user's directories into the working dir.
  - Commit and push changes with meaningful commit messages.
  - Prevent concurrent Git operations on the same repository.
  - Retry on transient network failures.
  - Handle the case where the remote already has content (pull before push).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Optional

from filelock import FileLock, Timeout

from . import config, security
from .user_manager import load_registry, save_registry, update_last_backup, get_user_backup_dirs

logger = logging.getLogger(__name__)

# Per-repo locks: repo_name -> threading.Lock
_repo_locks: dict[str, Lock] = {}
_repo_locks_meta: Lock = Lock()

# System files that are also present in the backup dir — never recurse into them
_VPSBACKUP_DATA_DIR = "/var/lib/vpsbackup"


def _get_repo_lock(repo_name: str) -> Lock:
    with _repo_locks_meta:
        if repo_name not in _repo_locks:
            _repo_locks[repo_name] = Lock()
        return _repo_locks[repo_name]


# ---------------------------------------------------------------------------
# Git credential helper
# ---------------------------------------------------------------------------

def _configure_git_credentials(working_dir: Path, token: str) -> None:
    """
    Configure git to use the token via a store credential helper in a
    temporary file — never embedded in the URL or environment.
    """
    # /etc is read-only under the systemd sandbox; keep the helper in the
    # service's writable state directory and restrict it to root.
    cred_dir = Path("/var/lib/vpsbackup")
    cred_dir.mkdir(parents=True, exist_ok=True)
    git_cred_file = cred_dir / "git-credentials"
    from .github_manager import get_github_username
    username = get_github_username()
    # Format: https://user:token@github.com
    git_cred_file.write_text(f"https://{username}:{token}@github.com\n")
    git_cred_file.chmod(0o600)

    # Configure git globally for the backup process (not the system)
    _run_git(
        working_dir,
        ["config", "credential.helper", f"store --file={git_cred_file}"],
        check=True,
    )


def _run_git(
    cwd: Path,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """
    Run a git command in *cwd*.
    Strips potential token leakage from error messages before logging.
    """
    git_env = os.environ.copy()
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    # Prevent git from trying to spawn SSH agent
    git_env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
    if env:
        git_env.update(env)

    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=git_env,
            capture_output=capture,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} timed out in {cwd}")

    if check and result.returncode != 0:
        stderr = _redact(result.stderr)
        stdout = _redact(result.stdout)
        raise RuntimeError(
            f"git {args[0]} failed (exit {result.returncode})\n"
            f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
        )
    return result


def _redact(text: str) -> str:
    """Remove any credential-like strings from git output before logging."""
    import re
    return re.sub(r"https://[^@]+@", "https://***@", text)


# ---------------------------------------------------------------------------
# Repository initialization
# ---------------------------------------------------------------------------

def get_working_dir(repo_name: str) -> Path:
    """Return the path to the local git working directory for *repo_name*."""
    return config.get_repos_path() / repo_name


def init_working_dir(
    repo_name: str,
    remote_url: str,
    author_name: str = "VPS Backup",
    author_email: str = "vpsbackup@localhost",
) -> Path:
    """
    Create or validate the local git working directory.

    If it already exists and is a valid git repo, re-configure it.
    If the remote already has commits, pull them.
    """
    working_dir = get_working_dir(repo_name)
    working_dir.mkdir(parents=True, exist_ok=True)

    # Write a .gitignore for the backup workspace itself
    gitignore = working_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# VPS Backup System — auto-generated\n"
            ".vpsbackup_state\n"
            "*.lock\n"
        )

    is_git_repo = (working_dir / ".git").is_dir()
    if not is_git_repo:
        _run_git(working_dir, ["init"], check=True)
        logger.info("Initialized git repo at %s", working_dir)

    # Configure identity for this repo
    _run_git(working_dir, ["config", "user.name", author_name], check=True)
    _run_git(working_dir, ["config", "user.email", author_email], check=True)
    _run_git(working_dir, ["config", "core.autocrlf", "false"], check=True)
    _run_git(working_dir, ["config", "push.default", "current"], check=True)

    # Set up credential helper
    token = config.get_github_token()
    _configure_git_credentials(working_dir, token)

    # Ensure remote is set correctly
    result = _run_git(working_dir, ["remote", "-v"], check=False)
    existing_remotes = result.stdout if result else ""
    safe_url = _redact(remote_url)

    if "origin" not in existing_remotes:
        _run_git(working_dir, ["remote", "add", "origin", remote_url], check=True)
        logger.info("Set remote origin to %s for %s", safe_url, repo_name)
    else:
        _run_git(working_dir, ["remote", "set-url", "origin", remote_url], check=True)

    # Attempt to pull existing content (repo was auto-initialized with README)
    if not is_git_repo or not (working_dir / ".git" / "FETCH_HEAD").exists():
        try:
            _run_git(working_dir, ["fetch", "origin"], check=True)
            # Check if remote has a main/master branch
            result = _run_git(
                working_dir,
                ["ls-remote", "--heads", "origin"],
                check=False,
            )
            if result and result.stdout.strip():
                # Remote has branches — check out main/master
                branches = result.stdout.strip().split("\n")
                default_branch = "main"
                for line in branches:
                    if "refs/heads/main" in line:
                        default_branch = "main"
                        break
                    if "refs/heads/master" in line:
                        default_branch = "master"
                        break
                try:
                    _run_git(
                        working_dir,
                        ["checkout", "-B", default_branch, f"origin/{default_branch}"],
                        check=True,
                    )
                except RuntimeError:
                    # No matching remote branch yet — just set up tracking
                    pass
        except RuntimeError as exc:
            logger.warning("Could not fetch from remote %s: %s (continuing)", safe_url, exc)

    return working_dir


# ---------------------------------------------------------------------------
# File state tracking (hash-based change detection)
# ---------------------------------------------------------------------------

def _file_hash(path: Path) -> str:
    """Return SHA-256 hex digest of file contents."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _load_state(repo_name: str) -> dict[str, str]:
    """Load the stored file-hash state for *repo_name*."""
    state_file = config.get_state_path() / f"{repo_name}.json"
    if not state_file.exists():
        return {}
    try:
        import json
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def _save_state(repo_name: str, state: dict[str, str]) -> None:
    import json
    state_dir = config.get_state_path()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{repo_name}.json"
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, state_file)


# ---------------------------------------------------------------------------
# Core backup operation
# ---------------------------------------------------------------------------

def backup_user(
    username: str,
    repo_name: str,
    backup_dirs: list[Path],
    working_dir: Path,
    *,
    force: bool = False,
) -> int:
    """
    Sync eligible files from *backup_dirs* into *working_dir* and push.

    Returns the number of files changed/added/deleted in this run.
    Uses a per-repo file lock to prevent concurrent Git operations.
    """
    lock_path = str(working_dir) + ".lock"
    try:
        with FileLock(lock_path, timeout=5), _get_repo_lock(repo_name):
            return _do_backup(username, repo_name, backup_dirs, working_dir, force=force)
    except Timeout:
        logger.warning("Backup for %s skipped — another operation is in progress", repo_name)
        return 0


def _do_backup(
    username: str,
    repo_name: str,
    backup_dirs: list[Path],
    working_dir: Path,
    *,
    force: bool = False,
) -> int:
    state = _load_state(repo_name)
    changed_files: list[tuple[str, str]] = []  # [(action, rel_path)]
    new_state: dict[str, str] = {}

    # Avoid accidentally backing up the backup system's own data
    protected_dirs = {
        Path(_VPSBACKUP_DATA_DIR).resolve(),
        working_dir.resolve(),
    }

    # Collect all eligible files from all backup dirs
    for bdir in backup_dirs:
        if not bdir.exists():
            logger.warning("Backup dir %s for user %s does not exist", bdir, username)
            continue

        bdir_resolved = bdir.resolve()
        # Safety: never backup backup system itself
        if any(bdir_resolved == p or _is_subpath(bdir_resolved, p) for p in protected_dirs):
            logger.error("Refusing to backup protected directory %s", bdir)
            continue

        approved_dirs = backup_dirs  # all configured dirs for this user

        for root, dirs, files in os.walk(bdir, followlinks=False):
            root_path = Path(root)

            # Filter excluded directories in-place to prevent descending
            dirs[:] = [
                d for d in dirs
                if not security.should_exclude(root_path / d, bdir)[0]
            ]

            for fname in files:
                fpath = root_path / fname

                # Validate file
                ok, reason = security.validate_file_for_backup(
                    fpath, bdir, approved_dirs
                )
                if not ok:
                    logger.debug("Skipping %s: %s", fpath, reason)
                    continue

                # Compute relative path for both state tracking and git staging
                rel_path = str(fpath.relative_to(bdir))
                dest_path = working_dir / rel_path

                current_hash = _file_hash(fpath)
                new_state[rel_path] = current_hash

                old_hash = state.get(rel_path, "")
                if current_hash == old_hash and not force:
                    continue  # No change

                # Copy file to working dir
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(fpath, dest_path)
                except (OSError, PermissionError) as exc:
                    logger.warning("Could not copy %s: %s", fpath, exc)
                    continue

                action = "update" if old_hash else "add"
                changed_files.append((action, rel_path))

    # Detect deletions (files in state but no longer present)
    deletion_policy = config.get("backup", "deletion_policy", "track")
    for rel_path, _hash in state.items():
        if rel_path not in new_state:
            dest_path = working_dir / rel_path
            if dest_path.exists() and deletion_policy == "track":
                dest_path.unlink(missing_ok=True)
                changed_files.append(("delete", rel_path))

    if not changed_files:
        logger.debug("No changes for user %s — skipping commit", username)
        _save_state(repo_name, new_state)
        return 0

    # Stage and commit
    try:
        _commit_changes(working_dir, changed_files, username)
        _push_with_retry(working_dir, repo_name)
    except Exception as exc:
        logger.error("Backup failed for user %s: %s", username, exc)
        raise

    _save_state(repo_name, new_state)

    # Update registry
    registry = load_registry()
    registry = update_last_backup(registry, username)
    save_registry(registry)

    logger.info("Backed up %d file(s) for user %s", len(changed_files), username)
    return len(changed_files)


def _commit_changes(
    working_dir: Path,
    changed_files: list[tuple[str, str]],
    username: str,
) -> None:
    """Stage all changed files and create a commit."""
    # Stage all changes
    _run_git(working_dir, ["add", "--all"], check=True)

    # Check if there's actually anything to commit
    result = _run_git(
        working_dir,
        ["diff", "--cached", "--name-only"],
        check=True,
    )
    if not result.stdout.strip():
        logger.debug("Nothing staged for %s — no commit needed", username)
        return

    message = _build_commit_message(changed_files)
    _run_git(
        working_dir,
        ["commit", "-m", message],
        check=True,
    )
    logger.debug("Committed for %s: %s", username, message)


def _build_commit_message(changed_files: list[tuple[str, str]]) -> str:
    """Generate a meaningful commit message from the list of changes."""
    adds = [p for a, p in changed_files if a == "add"]
    updates = [p for a, p in changed_files if a == "update"]
    deletes = [p for a, p in changed_files if a == "delete"]

    parts = []
    if len(changed_files) == 1:
        action, path = changed_files[0]
        name = Path(path).name
        if action == "add":
            return f"Backup: add {name}"
        elif action == "update":
            return f"Backup: update {name}"
        elif action == "delete":
            return f"Backup: delete {name}"

    summary_parts = []
    if adds:
        summary_parts.append(f"add {len(adds)}" if len(adds) > 1 else f"add {Path(adds[0]).name}")
    if updates:
        summary_parts.append(f"update {len(updates)}" if len(updates) > 1 else f"update {Path(updates[0]).name}")
    if deletes:
        summary_parts.append(f"delete {len(deletes)}" if len(deletes) > 1 else f"delete {Path(deletes[0]).name}")

    body_lines = []
    for action, path in sorted(changed_files)[:20]:
        body_lines.append(f"  {action}: {path}")
    if len(changed_files) > 20:
        body_lines.append(f"  ...and {len(changed_files) - 20} more")

    summary = "Backup: " + ", ".join(summary_parts)
    body = "\n".join(body_lines)
    return f"{summary}\n\n{body}"


def _push_with_retry(working_dir: Path, repo_name: str, max_retries: int = 5) -> None:
    """
    Push to remote with automatic retry and conflict resolution.
    """
    backoff_base = float(config.get("github", "retry_backoff", 2))
    for attempt in range(max_retries):
        try:
            result = _run_git(
                working_dir,
                ["push", "origin", "HEAD"],
                check=False,
            )
            if result.returncode == 0:
                logger.debug("Push succeeded for %s", repo_name)
                return

            stderr = result.stderr.lower()

            # Handle non-fast-forward (someone else pushed, or initial setup)
            if "non-fast-forward" in stderr or "rejected" in stderr or "[rejected]" in stderr:
                logger.info("Push rejected for %s — fetching and rebasing", repo_name)
                try:
                    _run_git(working_dir, ["fetch", "origin"], check=True)
                    _run_git(
                        working_dir,
                        ["rebase", "origin/HEAD", "--strategy-option=theirs"],
                        check=True,
                    )
                    continue  # Retry push after rebase
                except RuntimeError as exc:
                    logger.error("Rebase failed for %s: %s", repo_name, exc)
                    # Abort rebase if in progress
                    _run_git(working_dir, ["rebase", "--abort"], check=False)
                    raise

            # Generic error
            raise RuntimeError(
                f"git push failed for {repo_name} "
                f"(exit {result.returncode}): {_redact(result.stderr)[:300]}"
            )

        except (ConnectionError, TimeoutError) as exc:
            if attempt >= max_retries - 1:
                raise
            wait = backoff_base ** attempt
            logger.warning("Network error pushing %s (attempt %d): %s — retrying in %.0fs",
                           repo_name, attempt + 1, exc, wait)
            time.sleep(wait)

    raise RuntimeError(f"Push failed after {max_retries} attempts for {repo_name}")


# ---------------------------------------------------------------------------
# Convenience: full onboarding for a new user
# ---------------------------------------------------------------------------

def onboard_user(
    username: str,
    uid: int,
    home: str,
    repo_name: str,
    clone_url: str,
) -> bool:
    """
    Set up the working directory for a newly detected user and run initial backup.

    Returns True on success, False on failure.
    """
    try:
        working_dir = init_working_dir(repo_name, clone_url)
        backup_dirs = [Path(home)]

        if config.get("backup", "backup_existing_files", True):
            backup_user(username, repo_name, backup_dirs, working_dir, force=True)
        return True
    except Exception as exc:
        logger.error("Onboarding failed for user %s: %s", username, exc)
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
