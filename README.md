# VPS GitHub Automatic Backup System

Automatic, per-user GitHub backup for Ubuntu 22.04/24.04 VPS servers. Every Linux user gets their own private GitHub repository. File changes are detected in real time and committed automatically.

---

## Table of Contents

1. [Requirements](#requirements)
2. [GitHub Token Setup](#github-token-setup)
3. [Installation](#installation)
4. [What Gets Installed](#what-gets-installed)
5. [How It Works](#how-it-works)
6. [CLI Reference](#cli-reference)
7. [Configuration](#configuration)
8. [Exclusions and Security](#exclusions-and-security)
9. [Restoring Files](#restoring-files)
10. [Acceptance Test Walkthrough](#acceptance-test-walkthrough)
11. [Running the Test Suite](#running-the-test-suite)
12. [Uninstalling](#uninstalling)
13. [Troubleshooting](#troubleshooting)

---

## Requirements

- Ubuntu 22.04 or 24.04 (other Debian-based distributions may work)
- Root access
- Python 3.10 or newer
- A GitHub account
- A GitHub Personal Access Token (PAT) — see below

---

## GitHub Token Setup

The system needs a GitHub PAT with permission to create and push to private repositories.

### Fine-Grained PAT (recommended)

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**
2. Click **Generate new token**
3. Set **Token name** (e.g. `vpsbackup-myhostname`)
4. Set **Expiration** to your preference (no expiration is fine for a server)
5. Under **Repository access**, select **All repositories** (or **Only select repositories** if you prefer)
6. Under **Permissions → Repository permissions**, grant:
   - **Contents**: Read and write
   - **Metadata**: Read-only (required automatically)
7. Under **Account permissions**, grant:
   - **Administration**: Read and write (needed to create repositories)
8. Click **Generate token** and copy it immediately — it won't be shown again

### Classic PAT (alternative)

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Click **Generate new token (classic)**
3. Set a note and expiration
4. Check the **repo** scope (full control of private repositories)
5. Click **Generate token** and copy it

---

## Installation

```bash
# Download and run the installer as root
sudo bash install.sh
```

The installer will:

1. Check the OS and install system dependencies (`git`, `python3-pip`, `python3-venv`, `inotify-tools`)
2. Prompt for your GitHub PAT (input is hidden — nothing is echoed to the terminal)
3. Validate the token against the GitHub API
4. Detect the VPS hostname and create a private `<hostname>` repository on GitHub
5. Install the Python application to `/opt/vpsbackup/`
6. Store credentials at `/etc/vpsbackup/credentials` (mode 600, readable only by root)
7. Create the `/usr/local/bin/vpsbackup` CLI symlink
8. Enable and start the `vpsbackup` systemd service

### Installation is idempotent

Running `install.sh` again on an already-installed system is safe. It will re-validate the token, skip repository creation if the repo already exists, and restart the service.

---

## What Gets Installed

| Path | Purpose |
|---|---|
| `/opt/vpsbackup/` | Python application and virtualenv |
| `/etc/vpsbackup/config.yaml` | Main configuration file |
| `/etc/vpsbackup/credentials` | GitHub token (mode 600, root only) |
| `/var/lib/vpsbackup/` | Registry, working git repos, file-hash state |
| `/var/log/vpsbackup/` | Log files |
| `/usr/local/bin/vpsbackup` | CLI wrapper |
| `/etc/systemd/system/vpsbackup.service` | Systemd unit |

---

## How It Works

### User detection

The service polls `/etc/passwd` every 30 seconds (configurable). When a new regular user appears (UID ≥ 1000 by default, shell not `/bin/false` or `/sbin/nologin`), the service:

1. Creates a private GitHub repository named `<hostname>-<username>`
2. Clones it to `/var/lib/vpsbackup/repositories/<hostname>-<username>/`
3. Begins monitoring the user's home directory for file changes

### File monitoring

The service uses Linux `inotify` to watch for file events in real time. When files are created, modified, or deleted:

1. A 10-second debounce window opens (batches rapid changes into one commit)
2. Each changed file is checked against the exclusion list
3. Files that pass are scanned for secrets (AWS keys, GitHub tokens, OpenAI keys, etc.)
4. Qualifying files are copied to the working git repository, staged, committed, and pushed

### Change detection

Files are fingerprinted with SHA-256 before staging. A file is only committed if its hash has changed since the last backup. This prevents redundant commits when the inotify monitor fires on a metadata-only event.

### Repository structure

Each user's GitHub repository mirrors their home directory layout:

```
<hostname>-<username>/
├── Documents/
│   ├── notes.txt
│   └── report.pdf
├── projects/
│   └── myapp/
│       ├── main.py
│       └── config.json
└── .bashrc
```

Files are committed with messages like:

```
backup: 3 file(s) changed [2024-01-15 14:32:07 UTC]

Changed files:
  M Documents/notes.txt
  A projects/myapp/main.py
  D old_script.sh
```

---

## CLI Reference

All commands that modify service state require root.

### `vpsbackup status`

Show service status, uptime, monitored user count, and last backup times.

```
Service:     running (pid 12345)
Uptime:      2 days, 3 hours
Users:       3 active, 0 inactive
Last backup: alice  → 5 minutes ago
             bob    → 1 hour ago
             carol  → 12 minutes ago
```

### `vpsbackup users`

List all registered users with their status and last backup time.

### `vpsbackup repos`

List all GitHub repositories managed by this service.

### `vpsbackup now [USERNAME]`

Trigger an immediate backup. Without a username, backs up all active users.

```bash
sudo vpsbackup now           # all users
sudo vpsbackup now alice     # alice only
```

### `vpsbackup logs [--lines N]`

Show recent log output. Tokens are automatically redacted.

```bash
vpsbackup logs
vpsbackup logs --lines 200
```

### `vpsbackup test`

Run a connectivity and configuration test:

- Validates the GitHub token
- Checks repository access
- Verifies working directory integrity
- Tests inotify availability

### `vpsbackup config`

Display the current configuration (token is redacted).

### `vpsbackup start | stop | restart`

Control the systemd service.

```bash
sudo vpsbackup start
sudo vpsbackup stop
sudo vpsbackup restart
```

### `vpsbackup add-dir USERNAME PATH`

Add an additional directory to a user's backup scope.

```bash
sudo vpsbackup add-dir alice /data/alice-projects
```

### `vpsbackup remove-dir USERNAME PATH`

Remove a directory from a user's backup scope.

```bash
sudo vpsbackup remove-dir alice /data/alice-projects
```

---

## Configuration

The configuration file is at `/etc/vpsbackup/config.yaml`. Edit it and run `sudo vpsbackup restart` to apply changes.

```yaml
github:
  api_url: "https://api.github.com"
  private_repos: true
  retries: 3

vps:
  hostname: ""          # auto-detected from system hostname if empty

backup:
  debounce_seconds: 10  # wait this long after last event before committing
  scan_interval: 300    # full reconciliation scan interval (seconds)
  max_file_size_mb: 50  # files larger than this are skipped

users:
  min_uid: 1000         # UIDs below this are considered system users
  include_root: false   # whether to back up root's home directory
  poll_interval: 30     # /etc/passwd polling interval (seconds)

exclusions:
  extensions:
    - ".db"
    - ".sqlite"
    - ".sqlite3"
    - ".log"
    - ".env"
    - ".pyc"
    # ... (see full list in config.yaml)
  directories:
    - ".git"
    - "node_modules"
    - "__pycache__"
    - ".cache"
    # ...
  filenames:
    - "id_rsa"
    - "id_ed25519"
    - ".netrc"
    # ...

security:
  scan_for_secrets: true
  secret_action: "skip"    # "skip" or "warn"
```

---

## Exclusions and Security

### What is never backed up

The system has several layers of exclusion:

**By file extension**: `.db`, `.sqlite`, `.sqlite3`, `.log`, `.pid`, `.sock`, `.env`, `.pyc`, `.pyo`, `.class`, `.lock`

**By directory name**: `.git`, `node_modules`, `__pycache__`, `.cache`, `.npm`, `.cargo`, `venv`, `.venv`, `vendor`

**By filename**: `id_rsa`, `id_ed25519`, `id_ecdsa`, `id_dsa`, `.netrc`, `authorized_keys`, `known_hosts`, `*.pem`, `*.p12`, `*.pfx`, `*.key`

**By magic bytes** (content-based): SQLite databases (header `SQLite format 3\x00`), Redis RDB files (header `REDIS`), MongoDB BSON files

**By symlink safety**: Symlinks pointing outside the approved backup directory are never followed

**By size**: Files larger than `max_file_size_mb` (default 50 MB) are skipped

### Secret scanning

Before staging any file, the system scans its content for secrets:

- AWS access keys (`AKIA[A-Z0-9]{16}`)
- GitHub PATs (`ghp_[A-Za-z0-9]{36}`, `github_pat_[A-Za-z0-9_]{82}`)
- OpenAI API keys (`sk-[A-Za-z0-9]{48}`)
- Generic API key patterns (`api[_-]?key\s*=\s*[A-Za-z0-9+/]{20,}`)
- Generic password patterns (`password\s*=\s*[^\s]{8,}`)

Files with detected secrets are skipped (default) or logged with a warning, depending on `secret_action` in the configuration.

### Credential protection

- The GitHub token is stored only at `/etc/vpsbackup/credentials` (mode 600)
- The token is never placed in git remote URLs — `git credential store` is used instead
- The token is never passed as a command-line argument
- The token is never logged; all log output passes through a redaction filter
- On uninstall with `--remove-config`, the credentials file is shredded with `shred -u`

---

## Restoring Files

### Restore a single file

```bash
# Clone or pull the user's repository
git clone https://github.com/<YOUR_GITHUB_USERNAME>/<HOSTNAME>-<USERNAME>.git
cd <HOSTNAME>-<USERNAME>

# See the commit history
git log --oneline

# Restore a specific file from a specific commit
git checkout <COMMIT_HASH> -- Documents/notes.txt
```

### Restore a directory at a point in time

```bash
git checkout <COMMIT_HASH> -- projects/myapp/
```

### Restore everything to a specific date

```bash
# Find the last commit before the target date
git log --before="2024-01-15 00:00:00" --oneline -1

# Check out that commit as a new branch
git checkout -b restore-2024-01-14 <COMMIT_HASH>
```

### View what changed on a specific date

```bash
git log --after="2024-01-14" --before="2024-01-16" --stat
```

### Restore files from the service's working directory

The service keeps a live working git repository at `/var/lib/vpsbackup/repositories/<hostname>-<username>/`. This is a regular git repository and can be used for recovery even without internet access:

```bash
cd /var/lib/vpsbackup/repositories/myvps-alice
git log --oneline -20
git show HEAD:Documents/notes.txt
```

---

## Acceptance Test Walkthrough

This walkthrough verifies the complete system end-to-end after installation.

### Step 1 — Verify the service is running

```bash
vpsbackup status
# Expected: "Service: running"

vpsbackup test
# Expected: all checks pass
```

### Step 2 — Verify user detection

```bash
sudo useradd -m -s /bin/bash testbackupuser
sleep 60  # wait for polling interval

vpsbackup users
# Expected: testbackupuser appears with status "active"

vpsbackup repos
# Expected: <hostname>-testbackupuser listed
```

### Step 3 — Verify file backup

```bash
sudo -u testbackupuser bash -c 'echo "hello world" > /home/testbackupuser/test_file.txt'
sleep 30  # wait for debounce + commit

vpsbackup status
# Expected: testbackupuser last_backup updated to <recent time>
```

### Step 4 — Verify GitHub push

```bash
# On GitHub: check https://github.com/<YOUR_USERNAME>/<hostname>-testbackupuser
# Expected: a commit containing test_file.txt
```

### Step 5 — Verify exclusions

```bash
sudo -u testbackupuser bash -c 'echo "test" > /home/testbackupuser/test.log'
sudo -u testbackupuser bash -c 'echo "SECRET=sk-test123456789012345678901234567890123456789012" > /home/testbackupuser/secrets.txt'
sleep 30

# Expected: neither test.log nor secrets.txt appears in the GitHub repository
```

### Step 6 — Verify immediate backup

```bash
sudo -u testbackupuser bash -c 'echo "urgent" > /home/testbackupuser/urgent.txt'
sudo vpsbackup now testbackupuser
# Expected: immediate push without waiting for debounce
```

### Step 7 — Clean up

```bash
sudo userdel -r testbackupuser
# Note: the GitHub repository is NOT deleted automatically (by design)
```

---

## Running the Test Suite

```bash
cd /home/user/vpsbackup

# Install test dependencies (in a virtualenv)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest

# Run all tests
pytest tests/ -v

# Run a specific scenario
pytest tests/test_exclusions.py -v
pytest tests/test_credential_security.py -v

# Run with coverage
pip install pytest-cov
pytest tests/ --cov=src --cov-report=term-missing
```

### Test scenarios

| File | Scenarios covered |
|---|---|
| `test_user_detection.py` | 1 — User detection (regular vs system users, UID boundary, deleted users) |
| `test_repo_management.py` | 2, 3 — Repo naming, 422 race condition, token not in URL |
| `test_file_backup.py` | 4 — File CRUD, oversized file skip, change detection |
| `test_exclusions.py` | 5, 6, 7 — ZIP backup, DB exclusion, secret scanning |
| `test_push_retry.py` | 9 — Push retry, max retries, non-fast-forward rebase |
| `test_recovery_and_safety.py` | 10, 11, 12 — Reboot recovery, symlink safety, concurrent protection |
| `test_credential_security.py` | 13 — Token redaction, file mode, credential isolation |
| `test_integration.py` | 14 — Full end-to-end: detect → repo create → backup → no redundant commit |

---

## Uninstalling

```bash
# Stop and remove service, keep data and config
sudo bash uninstall.sh

# Remove config (credentials shredded with shred -u)
sudo bash uninstall.sh --remove-config

# Remove all data (working repos, registry, state files)
sudo bash uninstall.sh --remove-data

# Remove log files
sudo bash uninstall.sh --remove-logs

# Full removal (everything except GitHub repos, which are never auto-deleted)
sudo bash uninstall.sh --purge
```

GitHub repositories are **never** removed automatically. Delete them manually from GitHub if needed.

---

## Troubleshooting

### Service won't start

```bash
# Check logs
journalctl -u vpsbackup -n 50

# Check Python is available
/opt/vpsbackup/venv/bin/python --version

# Validate config
vpsbackup test
```

### Token validation fails

- Confirm the token has `repo` scope (classic PAT) or Contents + Administration permissions (fine-grained PAT)
- Confirm the token is not expired
- Run `vpsbackup test` — it reports the specific API error

### New user not detected

- Wait for the polling interval (default 30 seconds)
- Check the user's shell is not `/bin/false` or `/sbin/nologin`
- Check the user's UID is ≥ `min_uid` (default 1000)
- Check `vpsbackup logs` for errors

### Files not appearing in GitHub

- Check `vpsbackup logs` for secret-scan or exclusion hits
- Check the file is not in an excluded directory (`node_modules`, `.git`, etc.)
- Check the file extension is not excluded (`.log`, `.db`, `.env`, etc.)
- Check the file size is under `max_file_size_mb`
- Run `sudo vpsbackup now <username>` to trigger an immediate backup and watch the logs

### inotify limit reached

```bash
# Increase the inotify watch limit
echo "fs.inotify.max_user_watches=524288" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### Push fails with "non-fast-forward"

This is handled automatically — the service fetches and rebases before retrying. If it persists, check `vpsbackup logs` for details. Manual resolution:

```bash
cd /var/lib/vpsbackup/repositories/<hostname>-<username>
sudo git pull --rebase origin main
```

### Credentials file permissions wrong

```bash
sudo chmod 600 /etc/vpsbackup/credentials
sudo chown root:root /etc/vpsbackup/credentials
```
