"""
cli.py — Administrator command-line interface: `vpsbackup`

Commands:
  status   — Show service status, hostname, users, repos, last backup
  users    — List registered users and their backup status
  repos    — List mapped GitHub repositories
  now      — Trigger an immediate backup (all users or specific user)
  logs     — Display recent logs without exposing credentials
  test     — Test GitHub authentication and connectivity
  config   — Show configuration file locations
  start    — Start the backup service
  stop     — Stop the backup service
  restart  — Restart the backup service
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


CONFIG_PATH = "/etc/vpsbackup/config.yaml"
SERVICE_NAME = "vpsbackup"


def _load_config_and_registry():
    """Load config and registry, returning (config_module, registry)."""
    sys.path.insert(0, "/opt/vpsbackup")
    from src import config as cfg
    cfg.load(CONFIG_PATH)
    from src import user_manager as um
    return cfg, um


def _timestamp_to_str(ts) -> str:
    if ts is None:
        return "never"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _redact_token_from_text(text: str) -> str:
    """Strip GitHub PAT patterns from log output."""
    text = re.sub(r"ghp_[A-Za-z0-9]{36}", "ghp_***", text)
    text = re.sub(r"github_pat_[A-Za-z0-9_]{82}", "github_pat_***", text)
    text = re.sub(r"https://[^@\s]+@", "https://***@", text)
    return text


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_status(args) -> None:
    try:
        cfg, um = _load_config_and_registry()
    except Exception as exc:
        print(f"Error loading config: {exc}", file=sys.stderr)
        sys.exit(1)

    hostname = cfg.get_hostname()
    registry = um.load_registry()
    github_username = registry.get("github_username", "unknown")
    vps_repo = registry.get("vps_repo", "unknown")
    active_users = um.get_active_users(registry)

    print("=" * 60)
    print("VPS GitHub Backup System — Status")
    print("=" * 60)
    print(f"VPS Hostname    : {hostname}")
    print(f"GitHub Account  : {github_username}")
    print(f"VPS Repository  : {vps_repo}")
    print(f"Active Users    : {len(active_users)}")
    print()

    # Service status via systemctl
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True, text=True
    )
    svc_status = result.stdout.strip()
    print(f"Service Status  : {svc_status}")

    if active_users:
        print()
        print("Registered Users:")
        print(f"  {'Username':<20} {'Repository':<40} {'Last Backup'}")
        print(f"  {'-'*20} {'-'*40} {'-'*20}")
        for uname, udata in sorted(active_users.items()):
            last = _timestamp_to_str(udata.get("last_backup"))
            repo = udata.get("repo", "?")
            print(f"  {uname:<20} {repo:<40} {last}")

    print("=" * 60)


def cmd_users(args) -> None:
    try:
        cfg, um = _load_config_and_registry()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    registry = um.load_registry()
    all_users = registry.get("users", {})

    if not all_users:
        print("No users registered.")
        return

    print(f"{'Username':<20} {'Status':<12} {'UID':<8} {'Home':<30} {'Last Backup'}")
    print(f"{'-'*20} {'-'*12} {'-'*8} {'-'*30} {'-'*20}")
    for uname, udata in sorted(all_users.items()):
        status = udata.get("status", "?")
        uid = udata.get("uid", "?")
        home = udata.get("home", "?")
        last = _timestamp_to_str(udata.get("last_backup"))
        print(f"{uname:<20} {status:<12} {uid:<8} {home:<30} {last}")


def cmd_repos(args) -> None:
    try:
        cfg, um = _load_config_and_registry()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    registry = um.load_registry()
    github_username = registry.get("github_username", "?")
    vps_repo = registry.get("vps_repo", "")

    repos = []
    if vps_repo:
        repos.append(("(VPS)", vps_repo, f"https://github.com/{github_username}/{vps_repo}"))

    for uname, udata in sorted(registry.get("users", {}).items()):
        repo = udata.get("repo", "?")
        url = f"https://github.com/{github_username}/{repo}"
        repos.append((uname, repo, url))

    if not repos:
        print("No repositories configured.")
        return

    print(f"{'Owner':<20} {'Repository':<40} {'URL'}")
    print(f"{'-'*20} {'-'*40} {'-'*50}")
    for owner, repo, url in repos:
        print(f"{owner:<20} {repo:<40} {url}")


def cmd_now(args) -> None:
    """Trigger an immediate backup."""
    # Signal the service by sending SIGUSR1, or fall back to direct invocation
    pid_file = Path("/run/vpsbackup.pid")
    username = getattr(args, "user", None)

    if pid_file.exists():
        try:
            import signal as sig
            pid = int(pid_file.read_text().strip())
            os.kill(pid, sig.SIGUSR1)
            print(f"Backup triggered (PID {pid})")
            return
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    # Fall back to direct backup
    print("Service PID not found — running backup directly.")
    try:
        cfg, um = _load_config_and_registry()
        from src import backup_manager as bm, github_manager as gh

        registry = um.load_registry()
        github_username = registry.get("github_username")
        target_users = um.get_active_users(registry)

        if username:
            if username not in target_users:
                print(f"User '{username}' not found or inactive.")
                sys.exit(1)
            target_users = {username: target_users[username]}

        for uname, udata in target_users.items():
            repo_name = udata["repo"]
            backup_dirs = um.get_user_backup_dirs(registry, uname)
            working_dir = bm.get_working_dir(repo_name)
            if not working_dir.exists():
                clone_url = gh.get_repo_clone_url(github_username, repo_name)
                bm.init_working_dir(repo_name, clone_url)
            count = bm.backup_user(uname, repo_name, backup_dirs, working_dir, force=True)
            print(f"User {uname}: {count} file(s) backed up.")
    except Exception as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_logs(args) -> None:
    """Display recent logs without exposing credentials."""
    lines = getattr(args, "lines", 50)
    try:
        cfg, _ = _load_config_and_registry()
        log_file = cfg.get("logging", "log_file", "/var/log/vpsbackup/vpsbackup.log")
    except Exception:
        log_file = "/var/log/vpsbackup/vpsbackup.log"

    log_path = Path(log_file)
    if log_path.exists():
        # Read last N lines
        with log_path.open() as fh:
            all_lines = fh.readlines()
        for line in all_lines[-lines:]:
            print(_redact_token_from_text(line), end="")
    else:
        # Fall back to journalctl
        result = subprocess.run(
            ["journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True,
        )
        print(_redact_token_from_text(result.stdout))


def cmd_test(args) -> None:
    """Test GitHub authentication and connectivity."""
    print("Testing GitHub authentication...")
    try:
        sys.path.insert(0, "/opt/vpsbackup")
        from src import config as cfg
        cfg.load(CONFIG_PATH)
        from src import github_manager as gh

        ok, result = gh.validate_token()
        if ok:
            print(f"✓ Authenticated as GitHub user: {result}")
        else:
            print(f"✗ Authentication failed: {result}")
            sys.exit(1)

        # Test repo listing
        hostname = cfg.get_hostname()
        repos = gh.list_user_repos(prefix=hostname)
        print(f"✓ Found {len(repos)} repository/repositories with prefix '{hostname}'")
        for repo in repos[:5]:
            print(f"  - {repo['name']} ({'private' if repo['private'] else 'public'})")
        if len(repos) > 5:
            print(f"  ... and {len(repos) - 5} more")

        print("\n✓ All tests passed.")
    except Exception as exc:
        print(f"✗ Test failed: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_config(args) -> None:
    """Show configuration file locations."""
    paths = {
        "Main config": "/etc/vpsbackup/config.yaml",
        "Credentials": "/etc/vpsbackup/credentials",
        "Registry": "/var/lib/vpsbackup/registry.json",
        "Repositories": "/var/lib/vpsbackup/repositories/",
        "State": "/var/lib/vpsbackup/state/",
        "Log file": "/var/log/vpsbackup/vpsbackup.log",
        "Systemd service": f"/etc/systemd/system/{SERVICE_NAME}.service",
        "CLI binary": "/usr/local/bin/vpsbackup",
    }
    print("VPS Backup System — Configuration Paths")
    print("=" * 50)
    for label, path in paths.items():
        exists = "✓" if Path(path).exists() else "✗ (missing)"
        print(f"  {label:<25} {path}  {exists}")


def cmd_start(args) -> None:
    os.execvp("systemctl", ["systemctl", "start", SERVICE_NAME])


def cmd_stop(args) -> None:
    os.execvp("systemctl", ["systemctl", "stop", SERVICE_NAME])


def cmd_restart(args) -> None:
    os.execvp("systemctl", ["systemctl", "restart", SERVICE_NAME])


# ---------------------------------------------------------------------------
# Directory management
# ---------------------------------------------------------------------------

def cmd_add_dir(args) -> None:
    """Add a monitored directory for a user."""
    username = args.user
    directory = Path(args.directory).resolve()

    if not directory.exists():
        print(f"Error: Directory {directory} does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        cfg, um = _load_config_and_registry()
        registry = um.load_registry()
        user_entry = um.get_user_entry(registry, username)
        if not user_entry:
            print(f"Error: User '{username}' is not registered.", file=sys.stderr)
            sys.exit(1)

        existing = user_entry.get("backup_dirs", [])
        if str(directory) in existing:
            print(f"Directory {directory} is already monitored for user {username}.")
            return

        existing.append(str(directory))
        registry["users"][username]["backup_dirs"] = existing
        um.save_registry(registry)
        print(f"Added {directory} to monitored directories for user {username}.")
        print("Restart the service to apply: vpsbackup restart")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_remove_dir(args) -> None:
    """Remove a monitored directory for a user."""
    username = args.user
    directory = str(Path(args.directory).resolve())

    try:
        cfg, um = _load_config_and_registry()
        registry = um.load_registry()
        user_entry = um.get_user_entry(registry, username)
        if not user_entry:
            print(f"Error: User '{username}' is not registered.", file=sys.stderr)
            sys.exit(1)

        existing = user_entry.get("backup_dirs", [])
        if directory not in existing:
            print(f"Directory {directory} is not monitored for user {username}.")
            return

        existing.remove(directory)
        registry["users"][username]["backup_dirs"] = existing
        um.save_registry(registry)
        print(f"Removed {directory} from monitored directories for user {username}.")
        print("Restart the service to apply: vpsbackup restart")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vpsbackup",
        description="VPS GitHub Automatic Backup System — Administrator CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show system status")
    subparsers.add_parser("users", help="List registered users")
    subparsers.add_parser("repos", help="List GitHub repositories")

    now_p = subparsers.add_parser("now", help="Trigger immediate backup")
    now_p.add_argument("--user", help="Backup a specific user only")

    logs_p = subparsers.add_parser("logs", help="Show recent logs")
    logs_p.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to show")

    subparsers.add_parser("test", help="Test GitHub connectivity")
    subparsers.add_parser("config", help="Show configuration paths")
    subparsers.add_parser("start", help="Start backup service")
    subparsers.add_parser("stop", help="Stop backup service")
    subparsers.add_parser("restart", help="Restart backup service")

    # Directory management (admin-only)
    add_dir_p = subparsers.add_parser("add-dir", help="Add monitored directory for a user")
    add_dir_p.add_argument("user", help="Linux username")
    add_dir_p.add_argument("directory", help="Directory path to monitor")

    rm_dir_p = subparsers.add_parser("remove-dir", help="Remove monitored directory for a user")
    rm_dir_p.add_argument("user", help="Linux username")
    rm_dir_p.add_argument("directory", help="Directory path to remove")

    args = parser.parse_args()

    # Enforce root for most operations
    sensitive_cmds = {
        "start", "stop", "restart", "now", "add-dir", "remove-dir", "test"
    }
    if args.command in sensitive_cmds and os.geteuid() != 0:
        print(f"Error: '{args.command}' requires root privileges.", file=sys.stderr)
        sys.exit(1)

    dispatch = {
        "status": cmd_status,
        "users": cmd_users,
        "repos": cmd_repos,
        "now": cmd_now,
        "logs": cmd_logs,
        "test": cmd_test,
        "config": cmd_config,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "add-dir": cmd_add_dir,
        "remove-dir": cmd_remove_dir,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
