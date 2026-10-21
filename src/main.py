"""
main.py — VPS Backup Service entry point.

Responsibilities:
  - Bootstrap all subsystems (config, GitHub, registry, monitors).
  - Handle new-user events from UserWatcher.
  - Handle deleted-user events.
  - Run the main event loop.
  - Graceful shutdown on SIGTERM / SIGINT.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging must be configured before any other imports
# ---------------------------------------------------------------------------

def _setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_dir = Path(log_file).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
            )
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


# Pre-configure with defaults; reconfigure after config load
_setup_logging()
logger = logging.getLogger(__name__)


def main(config_path: str = "/etc/vpsbackup/config.yaml") -> None:
    """Main entry point for the VPS backup service."""

    # --- Load configuration ---
    from . import config as cfg
    try:
        cfg.load(config_path)
    except FileNotFoundError as exc:
        logger.error("Configuration file not found: %s", exc)
        sys.exit(1)

    # Reconfigure logging with settings from config
    log_level = cfg.get("logging", "level", "INFO")
    log_file = cfg.get("logging", "log_file", "/var/log/vpsbackup/vpsbackup.log")
    _setup_logging(log_level, log_file)
    logger.info("VPS Backup Service starting up")

    # Ensure data directories exist
    for d in [cfg.get_repos_path(), cfg.get_state_path(), cfg.get_registry_path().parent]:
        d.mkdir(parents=True, exist_ok=True)

    # --- Validate GitHub credentials ---
    from . import github_manager as gh
    try:
        ok, result = gh.validate_token()
        if not ok:
            logger.error("GitHub authentication failed: %s", result)
            sys.exit(1)
        github_username = result
        logger.info("Authenticated as GitHub user: %s", github_username)
    except Exception as exc:
        logger.error("Cannot connect to GitHub: %s", exc)
        sys.exit(1)

    # --- Load or initialize registry ---
    from . import user_manager as um
    hostname = cfg.get_hostname()
    vps_repo_name = um.get_vps_repo_name()
    registry = um.init_registry(hostname, github_username, vps_repo_name)
    um.save_registry(registry)
    logger.info("VPS hostname: %s | VPS repo: %s", hostname, vps_repo_name)

    # --- Set up VPS repo ---
    _ensure_vps_repo(vps_repo_name, hostname, github_username, cfg)

    # --- Backup manager reference ---
    from . import backup_manager as bm
    from . import file_monitor as fm

    # --- Define backup callback ---
    def do_backup(username: str, backup_dirs: list[Path]) -> None:
        reg = um.load_registry()
        user_entry = um.get_user_entry(reg, username)
        if not user_entry:
            logger.warning("Backup triggered for unknown user %s", username)
            return
        repo_name = user_entry["repo"]
        working_dir = bm.get_working_dir(repo_name)
        if not working_dir.exists():
            logger.warning("Working dir missing for %s — re-initializing", username)
            clone_url = gh.get_repo_clone_url(github_username, repo_name)
            working_dir = bm.init_working_dir(repo_name, clone_url)
        bm.backup_user(username, repo_name, backup_dirs, working_dir)

    # --- Initialize MonitorManager ---
    monitor_manager = fm.MonitorManager(on_backup=do_backup)

    # --- Onboard existing users ---
    _onboard_existing_users(registry, github_username, bm, gh, um, monitor_manager, cfg)

    # --- Set up UserWatcher for new/deleted users ---
    def on_new_user(user_info: dict) -> None:
        _handle_new_user(user_info, github_username, bm, gh, um, monitor_manager, cfg)

    def on_deleted_user(username: str) -> None:
        _handle_deleted_user(username, monitor_manager, um)

    user_watcher = um.UserWatcher(
        on_new_user=on_new_user,
        on_deleted_user=on_deleted_user,
    )
    user_watcher.start()

    # --- Periodic reconciliation scan ---
    def reconciliation_scan() -> None:
        """Periodically trigger backups to catch missed inotify events."""
        interval = int(cfg.get("backup", "scan_interval", 300))
        while _running:
            time.sleep(interval)
            if not _running:
                break
            logger.debug("Periodic reconciliation scan starting")
            monitor_manager.trigger_backup()

    import threading
    recon_thread = threading.Thread(
        target=reconciliation_scan, daemon=True, name="ReconciliationScan"
    )
    recon_thread.start()

    # --- Graceful shutdown ---
    _running = True

    def _shutdown(signum, frame):
        nonlocal _running
        logger.info("Received signal %d — shutting down", signum)
        _running = False
        monitor_manager.stop_all()
        user_watcher.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("VPS Backup Service running (PID %d)", os.getpid())

    # Write PID file
    pid_file = Path("/run/vpsbackup.pid")
    try:
        pid_file.write_text(str(os.getpid()))
    except OSError:
        pass

    # Main loop
    try:
        while _running:
            time.sleep(5)
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)
    finally:
        pid_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_vps_repo(vps_repo_name: str, hostname: str, github_username: str, cfg) -> None:
    from . import github_manager as gh, backup_manager as bm
    try:
        repo_data = gh.create_private_repo(
            vps_repo_name,
            description=f"VPS backup for {hostname}",
        )
        clone_url = gh.get_repo_clone_url(github_username, vps_repo_name)
        bm.init_working_dir(vps_repo_name, clone_url)
        logger.info("VPS repo ready: %s", vps_repo_name)
    except Exception as exc:
        logger.error("Failed to set up VPS repo %s: %s", vps_repo_name, exc)


def _onboard_existing_users(registry, github_username, bm, gh, um, monitor_manager, cfg) -> None:
    """Onboard all active users already in the registry."""
    active_users = um.get_active_users(registry)
    logger.info("Onboarding %d existing registered user(s)", len(active_users))
    for username, user_data in active_users.items():
        repo_name = user_data["repo"]
        backup_dirs = um.get_user_backup_dirs(registry, username)
        if not backup_dirs:
            logger.warning("No valid backup dirs for user %s", username)
            continue
        working_dir = bm.get_working_dir(repo_name)
        if not working_dir.exists():
            clone_url = gh.get_repo_clone_url(github_username, repo_name)
            try:
                bm.init_working_dir(repo_name, clone_url)
            except Exception as exc:
                logger.error("Failed to init working dir for %s: %s", username, exc)
                continue
        monitor_manager.add_user(username, backup_dirs)
        logger.info("Resumed monitoring for user %s -> %s", username, repo_name)


def _handle_new_user(user_info: dict, github_username: str, bm, gh, um, monitor_manager, cfg) -> None:
    """Called by UserWatcher when a new Linux user is detected."""
    username = user_info["username"]
    uid = user_info["uid"]
    home = user_info.get("home", f"/home/{username}")

    logger.info("Onboarding new user: %s (uid=%d, home=%s)", username, uid, home)

    # Determine repo name
    repo_name = um.get_repo_name_for_user(username)

    # Create GitHub repository
    try:
        repo_data = gh.create_private_repo(
            repo_name,
            description=f"Backup for user {username} on {cfg.get_hostname()}",
        )
    except Exception as exc:
        logger.error("Failed to create repo for user %s: %s", username, exc)
        return

    # Update registry
    registry = um.load_registry()
    registry = um.register_user(registry, username, uid, home, repo_name)
    um.save_registry(registry)

    # Initialize git working directory
    clone_url = gh.get_repo_clone_url(github_username, repo_name)
    try:
        bm.init_working_dir(repo_name, clone_url)
    except Exception as exc:
        logger.error("Failed to init working dir for %s: %s", username, exc)
        return

    # Initial backup of existing files
    if cfg.get("backup", "backup_existing_files", True):
        backup_dirs = [Path(home)]
        working_dir = bm.get_working_dir(repo_name)
        try:
            bm.backup_user(username, repo_name, backup_dirs, working_dir, force=True)
        except Exception as exc:
            logger.warning("Initial backup for %s encountered an error: %s", username, exc)

    # Start monitoring
    backup_dirs = um.get_user_backup_dirs(um.load_registry(), username)
    if backup_dirs:
        monitor_manager.add_user(username, backup_dirs)

    logger.info("User %s fully onboarded -> repo: %s", username, repo_name)


def _handle_deleted_user(username: str, monitor_manager, um) -> None:
    """Called by UserWatcher when a Linux user is removed from the system."""
    monitor_manager.remove_user(username)
    registry = um.load_registry()
    registry = um.mark_user_inactive(registry, username)
    um.save_registry(registry)
    logger.info("User %s deactivated — repository preserved, monitoring stopped", username)


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/vpsbackup/config.yaml"
    main(cfg_path)
