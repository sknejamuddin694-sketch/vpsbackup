"""
security.py — File-level security checks before staging.

Responsibilities:
  - Apply exclusion patterns to file paths.
  - Scan file content for embedded secrets.
  - Validate backup directories (no symlink traversal, no path traversal).
  - Detect binary/database files that should not be committed.
"""

from __future__ import annotations

import fnmatch
import logging
import mimetypes
import os
import re
import stat
from pathlib import Path
from typing import Sequence

from . import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known database magic bytes (file-header signatures)
# ---------------------------------------------------------------------------
_DB_SIGNATURES: list[tuple[bytes, str]] = [
    (b"SQLite format 3", "SQLite database"),
    (b"\x00\x06\x15\x61\x00\x00", "MongoDB BSON"),
    (b"REDIS", "Redis RDB dump"),
    (b"\x1f\x8b", "gzip compressed (possibly DB dump)"),
]

# Maximum bytes to read for magic-byte detection
_MAGIC_BYTES_READ = 16

# Mime-types we NEVER want to commit
_BLOCKED_MIME_PREFIXES = (
    "application/x-executable",
    "application/x-sharedlib",
    "application/x-object",
)

# Extension-based hard blocks
_BLOCKED_EXTENSIONS = frozenset({
    ".rdb", ".aof",       # Redis
    ".frm", ".ibd",       # MySQL InnoDB
    ".myd", ".myi",       # MySQL MyISAM
    ".bak",               # Generic backup
    ".dump",              # DB dumps
    ".sql",               # SQL dumps
    ".sql.gz",            # Compressed SQL
})


# ---------------------------------------------------------------------------
# Exclusion matching
# ---------------------------------------------------------------------------

def _compile_patterns(raw: Sequence[str]) -> list[re.Pattern]:
    """Convert gitignore-style glob patterns to regex patterns."""
    regexes = []
    for pat in raw:
        # Translate glob to regex; fnmatch.translate handles basic patterns
        try:
            regexes.append(re.compile(fnmatch.translate(pat), re.IGNORECASE))
        except re.error:
            logger.warning("Invalid exclusion pattern: %r", pat)
    return regexes


def _load_exclusions() -> dict[str, list[re.Pattern]]:
    exc = config.get_exclusion_patterns()
    return {
        "directories": _compile_patterns(exc.get("directories", [])),
        "files": _compile_patterns(exc.get("files", [])),
        "exact_names": _compile_patterns(
            [re.escape(n) for n in exc.get("exact_names", [])]
        ),
    }


def should_exclude(path: Path, base_dir: Path) -> tuple[bool, str]:
    """
    Determine whether *path* should be excluded from backup.

    Returns:
        (True, reason) if the file should be excluded.
        (False, "")    if the file is eligible.
    """
    exc = _load_exclusions()
    relative = None
    try:
        relative = path.relative_to(base_dir)
    except ValueError:
        return True, "Path is outside monitored base directory"

    name = path.name
    parts = relative.parts

    # 1. Check exact name matches (highest priority — security-critical)
    for pattern in exc["exact_names"]:
        if pattern.match(name):
            return True, f"Excluded by exact name: {name}"

    # 2. Check if any directory component is excluded
    for part in parts[:-1]:  # all but the filename
        for pattern in exc["directories"]:
            if pattern.match(part) or pattern.match(part + "/"):
                return True, f"Excluded directory component: {part}"

    # 3. Check file name patterns
    for pattern in exc["files"]:
        if pattern.match(name):
            return True, f"Excluded by file pattern: {name}"

    # 4. Check extension hard-blocks
    ext = path.suffix.lower()
    # Handle .sql.gz style double extension
    stem_ext = Path(path.stem).suffix.lower() + ext
    if ext in _BLOCKED_EXTENSIONS or stem_ext in _BLOCKED_EXTENSIONS:
        return True, f"Blocked extension: {ext}"

    # 5. Check magic bytes for database files
    if path.is_file():
        try:
            magic_result = _check_magic_bytes(path)
            if magic_result:
                return True, f"Database/binary signature detected: {magic_result}"
        except (PermissionError, OSError):
            pass  # Can't read — let the backup attempt and fail naturally

    return False, ""


def _check_magic_bytes(path: Path) -> str:
    """Return a description if the file matches a known DB/binary signature, else ''."""
    try:
        with path.open("rb") as fh:
            header = fh.read(_MAGIC_BYTES_READ)
    except OSError:
        return ""
    for signature, description in _DB_SIGNATURES:
        if header.startswith(signature):
            return description
    return ""


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------

def scan_for_secrets(path: Path) -> list[str]:
    """
    Scan a file for common secret patterns.

    Returns a list of descriptions of matches found, empty if clean.
    Only reads text-like files; binary files are skipped.
    Limits read size to avoid reading multi-GB files.
    """
    if not config.get("security", "scan_for_secrets", True):
        return []

    max_scan_bytes = 1 * 1024 * 1024  # 1 MB
    patterns = config.get_secret_patterns()
    if not patterns:
        return []

    try:
        size = path.stat().st_size
        if size == 0 or size > max_scan_bytes:
            return []
        content = path.read_text(errors="replace")
    except (UnicodeDecodeError, OSError, PermissionError):
        return []  # Binary or unreadable — skip secret scan

    hits = []
    for pat in patterns:
        if pat.search(content):
            # Do NOT include the matched value — just the pattern description
            hits.append(f"Potential secret matching pattern: {pat.pattern[:60]}")
    return hits


# ---------------------------------------------------------------------------
# Path safety validation
# ---------------------------------------------------------------------------

def validate_backup_dir(
    target: Path,
    approved_dirs: list[Path],
    follow_symlinks: bool = False,
) -> tuple[bool, str]:
    """
    Validate that *target* is safe to monitor.

    Checks:
      - The resolved path is under one of *approved_dirs*.
      - The path is not a symlink pointing outside approved dirs (unless follow_symlinks).
      - The path does not contain traversal sequences.

    Returns:
        (True, "")            if safe.
        (False, error_reason) if unsafe.
    """
    # Reject obvious traversal attempts
    raw = str(target)
    if ".." in raw.split(os.sep):
        return False, "Directory path contains '..' component"

    # Resolve symlinks to real path
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        return False, f"Cannot resolve path: {exc}"

    # Check symlink safety
    if target.is_symlink() and not follow_symlinks:
        link_target = Path(os.readlink(target))
        if not link_target.is_absolute():
            link_target = target.parent / link_target
        try:
            link_resolved = link_target.resolve(strict=False)
        except OSError as exc:
            return False, f"Cannot resolve symlink target: {exc}"
        # Check if the resolved symlink target is inside any approved dir
        approved = any(
            _is_subpath(link_resolved, a.resolve(strict=False))
            for a in approved_dirs
        )
        if not approved:
            return False, f"Symlink points outside approved directories: {link_resolved}"

    # Check the resolved path is under an approved dir
    approved = any(
        _is_subpath(resolved, a.resolve(strict=False))
        for a in approved_dirs
    )
    if not approved:
        return False, f"Path {resolved} is outside approved directories"

    return True, ""


def _is_subpath(child: Path, parent: Path) -> bool:
    """Return True if *child* is *parent* or a descendant of *parent*."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_file_for_backup(
    path: Path,
    base_dir: Path,
    approved_dirs: list[Path],
) -> tuple[bool, str]:
    """
    Full validation pipeline for a single file before staging.

    Returns:
        (True, "")       — file is safe and eligible.
        (False, reason)  — file should be skipped (with reason logged).
    """
    # 1. Must be a regular file
    try:
        st = path.stat()
    except OSError as exc:
        return False, f"Cannot stat file: {exc}"

    if not stat.S_ISREG(st.st_mode):
        return False, "Not a regular file"

    # 2. Size limit
    max_size = config.get("backup", "max_file_size", 52428800)
    if st.st_size > max_size:
        logger.warning(
            "File %s exceeds max_file_size (%d > %d bytes) — skipping",
            path, st.st_size, max_size,
        )
        return False, f"File too large ({st.st_size} > {max_size} bytes)"

    # Warn-but-continue for large files
    warn_size = config.get("backup", "warn_file_size", 26214400)
    if st.st_size > warn_size:
        logger.warning("Large file: %s (%d bytes)", path, st.st_size)

    # 3. Path safety
    ok, reason = validate_backup_dir(path, approved_dirs)
    if not ok:
        return False, reason

    # 4. Exclusion patterns
    excluded, reason = should_exclude(path, base_dir)
    if excluded:
        logger.debug("Excluding %s: %s", path, reason)
        return False, reason

    # 5. Secret scan
    action = config.get("security", "secret_action", "skip")
    hits = scan_for_secrets(path)
    if hits:
        logger.warning("Potential secrets in %s: %s", path, "; ".join(hits))
        if action == "skip":
            return False, f"Secret scan triggered: {'; '.join(hits)}"
        # action == "block" — caller handles this

    return True, ""
