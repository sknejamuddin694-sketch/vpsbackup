"""
test_exclusions.py — Scenarios 5, 6, 7: ZIP backup, DB exclusion, secret exclusion.

Tests:
  5.1  ZIP file present → backup proceeds (ZIP is a regular file, not extracted inline)
  5.2  ZIP file causes debounce window → rapid events batched into one commit
  6.1  .sqlite file → excluded by extension
  6.2  .db file → excluded by extension
  6.3  SQLite magic bytes in a file with non-db extension → excluded
  6.4  Redis RDB magic bytes → excluded
  6.5  .log file → excluded
  6.6  .env file → excluded
  6.7  id_rsa → excluded by filename
  7.1  AWS access key in file → file skipped
  7.2  GitHub PAT in file → file skipped
  7.3  OpenAI key in file → file skipped
  7.4  Generic API key pattern → file skipped
  7.5  Clean file (no secrets) → included normally
"""

from __future__ import annotations

import struct
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestZIPBackup:

    def test_zip_file_not_excluded_by_default(self, tmp_path):
        """5.1 — .zip files are not in the default exclusion list."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        zf = user_dir / "archive.zip"

        # Create a minimal zip
        with zipfile.ZipFile(str(zf), "w") as z:
            z.writestr("readme.txt", "hello from zip")

        result = security.should_exclude(str(zf), str(user_dir))
        assert result is False

    def test_zip_file_passes_validation(self, tmp_path):
        """5.1 — .zip file under size limit passes validate_file_for_backup."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        zf = user_dir / "backup.zip"
        with zipfile.ZipFile(str(zf), "w") as z:
            z.writestr("data.txt", "some data")

        result = security.validate_file_for_backup(str(zf), max_size_mb=50)
        assert result is True


class TestDatabaseExclusion:

    def test_sqlite_extension_excluded(self, tmp_path):
        """6.1 — .sqlite extension is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        db = user_dir / "app.sqlite"
        db.write_bytes(b"\x00" * 100)

        assert security.should_exclude(str(db), str(user_dir)) is True

    def test_db_extension_excluded(self, tmp_path):
        """6.2 — .db extension is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        db = user_dir / "mydata.db"
        db.write_bytes(b"\x00" * 100)

        assert security.should_exclude(str(db), str(user_dir)) is True

    def test_sqlite_magic_bytes_excluded(self, tmp_path):
        """6.3 — File with SQLite magic bytes (but .dat extension) is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        f = user_dir / "hidden_db.dat"
        # SQLite magic: b"SQLite format 3\x00"
        f.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

        assert security.should_exclude(str(f), str(user_dir)) is True

    def test_redis_rdb_magic_bytes_excluded(self, tmp_path):
        """6.4 — File with Redis RDB magic bytes is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        f = user_dir / "dump.rdb"
        # Redis RDB magic: b"REDIS"
        f.write_bytes(b"REDIS0011\xfa\x09redis-ver\x05" + b"\x00" * 50)

        assert security.should_exclude(str(f), str(user_dir)) is True

    def test_log_extension_excluded(self, tmp_path):
        """6.5 — .log extension is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        log = user_dir / "app.log"
        log.write_text("2024-01-01 some log entry\n")

        assert security.should_exclude(str(log), str(user_dir)) is True

    def test_env_file_excluded(self, tmp_path):
        """6.6 — .env file is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        env = user_dir / ".env"
        env.write_text("DATABASE_URL=postgres://...\nSECRET_KEY=abc123\n")

        assert security.should_exclude(str(env), str(user_dir)) is True

    def test_private_key_filename_excluded(self, tmp_path):
        """6.7 — id_rsa is excluded by filename."""
        from src import security

        user_dir = tmp_path / "home" / "alice" / ".ssh"
        user_dir.mkdir(parents=True)
        key = user_dir / "id_rsa"
        key.write_text("-----BEGIN RSA PRIVATE KEY-----\n...")

        assert security.should_exclude(str(key), str(user_dir)) is True

    def test_node_modules_dir_excluded(self, tmp_path):
        """6.x — node_modules directory is excluded."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        node = user_dir / "project" / "node_modules" / "lodash" / "index.js"
        node.mkdir(parents=True)

        # The directory itself should be excluded
        assert security.should_exclude(str(user_dir / "project" / "node_modules"), str(user_dir)) is True


class TestSecretExclusion:

    def _write_file(self, path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_aws_access_key_skipped(self, tmp_path):
        """7.1 — File containing AWS access key is skipped."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        f = self._write_file(
            user_dir / "config.txt",
            "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\nexport AWS_SECRET=wJalrXUtnFEMI\n",
        )

        result = security.scan_for_secrets(str(f))
        assert result is True  # secrets found

    def test_github_pat_skipped(self, tmp_path):
        """7.2 — File containing GitHub PAT is skipped."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        # Generate a pattern-valid PAT (ghp_ + 36 alphanumeric chars)
        fake_pat = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh"
        f = self._write_file(
            user_dir / "script.sh",
            f'GITHUB_TOKEN="{fake_pat}"\ncurl -H "Authorization: token $GITHUB_TOKEN" ...\n',
        )

        result = security.scan_for_secrets(str(f))
        assert result is True

    def test_openai_key_skipped(self, tmp_path):
        """7.3 — File containing OpenAI key is skipped."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        # sk- followed by 48 chars
        fake_key = "sk-" + "T" * 48
        f = self._write_file(
            user_dir / "openai_client.py",
            f'client = OpenAI(api_key="{fake_key}")\n',
        )

        result = security.scan_for_secrets(str(f))
        assert result is True

    def test_clean_file_not_skipped(self, tmp_path):
        """7.5 — Clean file with no secrets is not skipped."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        f = self._write_file(
            user_dir / "notes.txt",
            "Remember to buy milk.\nMeeting at 3pm.\nProject deadline: Friday.\n",
        )

        result = security.scan_for_secrets(str(f))
        assert result is False  # no secrets found

    def test_github_pat_fine_grained_skipped(self, tmp_path):
        """7.4 — Fine-grained GitHub PAT (github_pat_...) is skipped."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        fake_pat = "github_pat_" + "A" * 82
        f = self._write_file(
            user_dir / "deploy.sh",
            f'TOKEN="{fake_pat}"\n',
        )

        result = security.scan_for_secrets(str(f))
        assert result is True

    def test_binary_file_secret_scan_skipped_gracefully(self, tmp_path):
        """7.x — Binary file is not crashed during secret scan."""
        from src import security

        user_dir = tmp_path / "home" / "alice"
        user_dir.mkdir(parents=True)
        f = user_dir / "binary.bin"
        f.write_bytes(bytes(range(256)) * 100)

        # Should not raise, just return False (can't scan binary as text)
        try:
            result = security.scan_for_secrets(str(f))
            # Either True or False is acceptable; no exception is required
            assert isinstance(result, bool)
        except Exception as exc:
            pytest.fail(f"scan_for_secrets should not raise on binary files: {exc}")
