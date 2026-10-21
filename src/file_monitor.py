"""
file_monitor.py — inotify-based file monitoring per user.

Responsibilities:
  - Watch each registered user's backup directories recursively.
  - Batch rapid file events into a single commit after a debounce window.
  - Handle atomic writes, temp-file renames, and ZIP extractions.
  - Automatically pick up newly created subdirectories.
  - Re-establish monitoring after service restart without missing changes.
  - Avoid excessive CPU/RAM by using event-driven inotify, not polling.
  - Fall back to a periodic reconciliation scan if inotify is unavailable.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import config

logger = logging.getLogger(__name__)

# inotify event flags we care about
# IN_CLOSE_WRITE  — file was opened for write and then closed (safe to read)
# IN_MOVED_TO     — file renamed into monitored dir
# IN_CREATE       — file/dir created
# IN_DELETE       — file/dir deleted
# IN_ATTRIB       — metadata changed (e.g., chmod; rare but useful)
try:
    import inotify_simple

    _INOTIFY_FLAGS = (
        inotify_simple.flags.CLOSE_WRITE
        | inotify_simple.flags.MOVED_TO
        | inotify_simple.flags.CREATE
        | inotify_simple.flags.DELETE
        | inotify_simple.flags.MOVE_FROM
        | inotify_simple.flags.ATTRIB
    )
    _INOTIFY_AVAILABLE = True
except ImportError:
    _INOTIFY_AVAILABLE = False
    logger.warning(
        "inotify_simple not available — falling back to periodic scan. "
        "Install with: pip install inotify-simple"
    )


# ---------------------------------------------------------------------------
# UserMonitor: watches one user's directories and triggers backups
# ---------------------------------------------------------------------------

class UserMonitor:
    """
    Monitors all backup directories for a single Linux user.
    Queues file events and triggers a backup callback after debouncing.
    """

    def __init__(
        self,
        username: str,
        backup_dirs: list[Path],
        on_backup: Callable[[str, list[Path]], None],
    ):
        self.username = username
        self.backup_dirs = backup_dirs
        self.on_backup = on_backup
        self._running = False
        self._event_queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        if _INOTIFY_AVAILABLE:
            t = threading.Thread(
                target=self._inotify_loop,
                daemon=True,
                name=f"Monitor-{self.username}",
            )
        else:
            t = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name=f"Monitor-{self.username}",
            )
        t.start()
        self._threads.append(t)

        # Debounce / flush thread
        dt = threading.Thread(
            target=self._debounce_loop,
            daemon=True,
            name=f"Debounce-{self.username}",
        )
        dt.start()
        self._threads.append(dt)

        logger.info(
            "Started monitoring %d dir(s) for user %s",
            len(self.backup_dirs),
            self.username,
        )

    def stop(self) -> None:
        self._running = False
        # Wake up debounce loop
        self._event_queue.put(None)
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()
        logger.info("Stopped monitoring for user %s", self.username)

    def update_dirs(self, new_dirs: list[Path]) -> None:
        """Hot-update the list of monitored directories."""
        self.backup_dirs = new_dirs
        # Restart monitoring to pick up new dirs
        self.stop()
        self.start()

    # -----------------------------------------------------------------------
    # inotify loop
    # -----------------------------------------------------------------------

    def _inotify_loop(self) -> None:
        """Watch all backup_dirs recursively using inotify."""
        if not _INOTIFY_AVAILABLE:
            return

        inotify = inotify_simple.INotify()
        watch_descriptors: dict[int, Path] = {}

        def add_watches(directory: Path) -> None:
            """Add inotify watches for *directory* and all subdirectories."""
            if not directory.exists():
                return
            for root, dirs, _ in os.walk(directory, followlinks=False):
                root_path = Path(root)
                # Don't watch excluded directories
                from . import security
                dirs[:] = [
                    d for d in dirs
                    if not security.should_exclude(root_path / d, directory)[0]
                ]
                try:
                    wd = inotify.add_watch(str(root_path), _INOTIFY_FLAGS)
                    watch_descriptors[wd] = root_path
                except OSError as exc:
                    logger.warning("Cannot watch %s: %s", root_path, exc)

        for bdir in self.backup_dirs:
            add_watches(bdir)

        logger.debug("inotify set up for user %s (%d watches)", self.username, len(watch_descriptors))

        while self._running:
            try:
                events = inotify.read(timeout=1000, read_delay=100)
            except Exception as exc:
                logger.error("inotify read error for user %s: %s", self.username, exc)
                time.sleep(1)
                continue

            for event in events:
                flags = inotify_simple.flags.from_mask(event.mask)
                name = event.name or ""
                parent_dir = watch_descriptors.get(event.wd)
                if parent_dir is None:
                    continue

                # If a new directory was created, add watches for it
                if inotify_simple.flags.CREATE in flags and inotify_simple.flags.ISDIR in flags:
                    new_dir = parent_dir / name
                    add_watches(new_dir)

                # Queue an event for the debounce loop
                if name:  # ignore events on the directory itself without a name
                    self._event_queue.put((parent_dir / name, event.mask))

        # Cleanup
        try:
            inotify.close()
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Polling fallback
    # -----------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Periodic scan fallback when inotify is unavailable."""
        interval = config.get("backup", "scan_interval", 300)
        logger.info("Polling user %s every %ds (inotify unavailable)", self.username, interval)
        while self._running:
            self._event_queue.put(("__scan__", 0))
            time.sleep(interval)

    # -----------------------------------------------------------------------
    # Debounce loop
    # -----------------------------------------------------------------------

    def _debounce_loop(self) -> None:
        """
        Collect events until a quiet period, then trigger backup.
        This prevents hundreds of commits during a ZIP extraction.
        """
        debounce = float(config.get("backup", "debounce_seconds", 10))
        pending = False
        last_event_time: float = 0.0

        while self._running or pending:
            try:
                item = self._event_queue.get(timeout=1.0)
            except queue.Empty:
                pass
            else:
                if item is None:
                    break  # stop signal
                pending = True
                last_event_time = time.monotonic()
                continue

            if pending:
                elapsed = time.monotonic() - last_event_time
                if elapsed >= debounce:
                    pending = False
                    try:
                        self.on_backup(self.username, self.backup_dirs)
                    except Exception as exc:
                        logger.exception(
                            "Backup callback error for user %s: %s", self.username, exc
                        )

        logger.debug("Debounce loop exiting for user %s", self.username)


# ---------------------------------------------------------------------------
# MonitorManager: orchestrates all per-user monitors
# ---------------------------------------------------------------------------

class MonitorManager:
    """
    Manages a pool of UserMonitor instances, one per registered user.
    """

    def __init__(self, on_backup: Callable[[str, list[Path]], None]):
        self._on_backup = on_backup
        self._monitors: dict[str, UserMonitor] = {}
        self._lock = threading.Lock()

    def add_user(self, username: str, backup_dirs: list[Path]) -> None:
        """Start monitoring directories for *username*."""
        with self._lock:
            if username in self._monitors:
                logger.debug("Updating monitor for existing user %s", username)
                self._monitors[username].update_dirs(backup_dirs)
                return
            monitor = UserMonitor(username, backup_dirs, self._on_backup)
            monitor.start()
            self._monitors[username] = monitor
            logger.info("Added monitor for user %s", username)

    def remove_user(self, username: str) -> None:
        """Stop monitoring for *username* (user was deleted)."""
        with self._lock:
            monitor = self._monitors.pop(username, None)
            if monitor:
                monitor.stop()
                logger.info("Removed monitor for user %s", username)

    def stop_all(self) -> None:
        """Stop all monitors (service shutdown)."""
        with self._lock:
            for username, monitor in list(self._monitors.items()):
                monitor.stop()
            self._monitors.clear()
        logger.info("All monitors stopped")

    def list_monitored_users(self) -> list[str]:
        with self._lock:
            return list(self._monitors.keys())

    def trigger_backup(self, username: Optional[str] = None) -> None:
        """
        Manually trigger a backup for *username* (or all users if None).
        """
        with self._lock:
            targets = (
                {username: self._monitors[username]}
                if username and username in self._monitors
                else dict(self._monitors)
            )
        for uname, monitor in targets.items():
            try:
                self._on_backup(uname, monitor.backup_dirs)
            except Exception as exc:
                logger.error("Manual backup trigger failed for %s: %s", uname, exc)
