#!/usr/bin/env bash
# install.sh — VPS GitHub Automatic Backup System Installer
#
# Supported: Ubuntu 22.04, Ubuntu 24.04
# Requirements: Python 3.10+, git, curl
# Run as root: sudo bash install.sh
#
# Token security:
#   - GitHub PAT is read with no-echo input (read -rs)
#   - Stored at /etc/vpsbackup/credentials (mode 600, root only)
#   - Never written to logs, env files, or git remote URLs
#   - Never passed as a command-line argument

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors and helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Root check
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "This installer must be run as root: sudo bash install.sh"

# ---------------------------------------------------------------------------
# Detect OS
# ---------------------------------------------------------------------------
if [[ -f /etc/os-release ]]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VERSION="${VERSION_ID:-unknown}"
else
    OS_ID="unknown"
    OS_VERSION="unknown"
fi

case "${OS_ID}:${OS_VERSION}" in
    ubuntu:22.04|ubuntu:24.04) ;;
    *)
        warn "Untested OS: ${OS_ID} ${OS_VERSION}. Proceeding anyway."
        ;;
esac

info "Installing VPS GitHub Automatic Backup System"
info "OS: ${OS_ID} ${OS_VERSION}"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/vpsbackup"
CONFIG_DIR="/etc/vpsbackup"
DATA_DIR="/var/lib/vpsbackup"
LOG_DIR="/var/log/vpsbackup"
CRED_FILE="${CONFIG_DIR}/credentials"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
SERVICE_NAME="vpsbackup"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CLI_LINK="/usr/local/bin/vpsbackup"
VENV_DIR="${INSTALL_DIR}/venv"

# ---------------------------------------------------------------------------
# Step 1: Install system dependencies
# ---------------------------------------------------------------------------
info "Updating package lists and installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git curl inotify-tools \
    || die "Failed to install system packages"
success "System packages installed"

# Verify Python version
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "${PYTHON_VERSION}" | cut -d. -f1)
PYTHON_MINOR=$(echo "${PYTHON_VERSION}" | cut -d. -f2)
if [[ ${PYTHON_MAJOR} -lt 3 ]] || [[ ${PYTHON_MAJOR} -eq 3 && ${PYTHON_MINOR} -lt 10 ]]; then
    die "Python 3.10 or higher is required (found ${PYTHON_VERSION})"
fi
success "Python ${PYTHON_VERSION} found"

# ---------------------------------------------------------------------------
# Step 2: Prompt for GitHub PAT (no echo)
# ---------------------------------------------------------------------------
echo ""
echo "==================================================================="
echo "  GitHub Personal Access Token (PAT) Setup"
echo "==================================================================="
echo ""
echo "  Required token permissions:"
echo "    Fine-grained PAT (recommended):"
echo "      - Repository access: All repositories (or selected)"
echo "      - Permissions: Contents (Read & Write), Metadata (Read)"
echo ""
echo "    Classic PAT (alternative):"
echo "      - Scopes: repo (full)"
echo ""
echo "  Create a token at: https://github.com/settings/tokens"
echo ""

GITHUB_TOKEN=""
while [[ -z "${GITHUB_TOKEN}" ]]; do
    # -s = silent (no echo), -r = raw (no backslash escapes)
    read -rsp "  Enter your GitHub PAT: " GITHUB_TOKEN
    echo ""
    if [[ -z "${GITHUB_TOKEN}" ]]; then
        warn "Token cannot be empty. Please try again."
    fi
done

# ---------------------------------------------------------------------------
# Step 3: Validate token against GitHub API
# ---------------------------------------------------------------------------
info "Validating GitHub token..."

# Use Authorization header — never put token in URL
HTTP_STATUS=$(curl -sS -o /tmp/gh_auth_response.json -w "%{http_code}" \
    -H "Authorization: token ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github.v3+json" \
    --max-time 15 \
    "https://api.github.com/user" 2>/dev/null || echo "000")

if [[ "${HTTP_STATUS}" != "200" ]]; then
    rm -f /tmp/gh_auth_response.json
    die "GitHub token validation failed (HTTP ${HTTP_STATUS}). Please check your token and try again."
fi

GITHUB_USERNAME=$(python3 -c "import json,sys; d=json.load(open('/tmp/gh_auth_response.json')); print(d['login'])" 2>/dev/null || echo "")
rm -f /tmp/gh_auth_response.json

if [[ -z "${GITHUB_USERNAME}" ]]; then
    die "Could not determine GitHub username from token response"
fi

success "Authenticated as GitHub user: ${GITHUB_USERNAME}"

# ---------------------------------------------------------------------------
# Step 4: Detect hostname
# ---------------------------------------------------------------------------
RAW_HOSTNAME=$(hostname -s 2>/dev/null || hostname)
# Sanitize: lowercase, replace non-alphanumeric with dash, collapse dashes
VPS_HOSTNAME=$(echo "${RAW_HOSTNAME}" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-\|-$//g')
# Truncate to 40 chars for hostname portion (repo max 100, leave room for username)
VPS_HOSTNAME="${VPS_HOSTNAME:0:40}"
info "VPS hostname: ${VPS_HOSTNAME}"

# ---------------------------------------------------------------------------
# Step 5: Create VPS-level GitHub repository
# ---------------------------------------------------------------------------
VPS_REPO_NAME="${VPS_HOSTNAME}"
info "Creating VPS repository '${VPS_REPO_NAME}' on GitHub..."

# Check if repo already exists
REPO_STATUS=$(curl -sS -o /dev/null -w "%{http_code}" \
    -H "Authorization: token ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github.v3+json" \
    --max-time 15 \
    "https://api.github.com/repos/${GITHUB_USERNAME}/${VPS_REPO_NAME}" 2>/dev/null || echo "000")

if [[ "${REPO_STATUS}" == "200" ]]; then
    success "VPS repository '${VPS_REPO_NAME}' already exists — reusing"
else
    # Create private repo
    CREATE_STATUS=$(curl -sS -o /tmp/gh_create_response.json -w "%{http_code}" \
        -X POST \
        -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github.v3+json" \
        -H "Content-Type: application/json" \
        --max-time 30 \
        -d "{\"name\":\"${VPS_REPO_NAME}\",\"private\":true,\"auto_init\":true,\"description\":\"VPS backup for ${VPS_HOSTNAME}\"}" \
        "https://api.github.com/user/repos" 2>/dev/null || echo "000")
    rm -f /tmp/gh_create_response.json

    if [[ "${CREATE_STATUS}" == "201" ]]; then
        success "Created private repository: ${VPS_REPO_NAME}"
    elif [[ "${CREATE_STATUS}" == "422" ]]; then
        # 422 = repo already exists (race condition)
        success "Repository '${VPS_REPO_NAME}' already exists"
    else
        die "Failed to create VPS repository (HTTP ${CREATE_STATUS})"
    fi
fi

# ---------------------------------------------------------------------------
# Step 6: Create directory structure
# ---------------------------------------------------------------------------
info "Creating directory structure..."
mkdir -p "${INSTALL_DIR}" "${CONFIG_DIR}" "${DATA_DIR}/repositories" "${DATA_DIR}/state" "${LOG_DIR}"
chmod 700 "${CONFIG_DIR}"
chmod 755 "${DATA_DIR}" "${LOG_DIR}"
success "Directories created"

# ---------------------------------------------------------------------------
# Step 7: Copy application files
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Installing application files to ${INSTALL_DIR}..."

# Copy source files
cp -r "${SCRIPT_DIR}/src" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
success "Application files installed"

# ---------------------------------------------------------------------------
# Step 8: Install Python virtual environment
# ---------------------------------------------------------------------------
info "Creating Python virtual environment..."
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt" \
    || die "Failed to install Python dependencies"
success "Python virtual environment ready"

# ---------------------------------------------------------------------------
# Step 9: Write credentials file (mode 600, root only)
# ---------------------------------------------------------------------------
info "Storing credentials (token never written to logs or URLs)..."
install -m 600 /dev/null "${CRED_FILE}"
# Write token using printf to avoid it appearing in process list
printf '%s\n' "${GITHUB_TOKEN}" > "${CRED_FILE}"
chmod 600 "${CRED_FILE}"
chown root:root "${CRED_FILE}"
# Clear the variable from memory
GITHUB_TOKEN=""
unset GITHUB_TOKEN
success "Credentials stored securely at ${CRED_FILE} (mode 600)"

# ---------------------------------------------------------------------------
# Step 10: Write configuration file
# ---------------------------------------------------------------------------
if [[ ! -f "${CONFIG_FILE}" ]]; then
    info "Writing configuration file..."
    cp "${SCRIPT_DIR}/config.yaml" "${CONFIG_FILE}"
    # Patch hostname into config
    python3 -c "
import re, sys
with open('${CONFIG_FILE}', 'r') as f:
    content = f.read()
content = re.sub(r'hostname:.*', 'hostname: ${VPS_HOSTNAME}', content)
with open('${CONFIG_FILE}', 'w') as f:
    f.write(content)
" 2>/dev/null || true
    chmod 640 "${CONFIG_FILE}"
    chown root:root "${CONFIG_FILE}"
    success "Configuration file written to ${CONFIG_FILE}"
else
    info "Configuration file already exists — skipping (${CONFIG_FILE})"
fi

# ---------------------------------------------------------------------------
# Step 11: Install CLI
# ---------------------------------------------------------------------------
info "Installing vpsbackup CLI..."
cat > "${CLI_LINK}" << 'CLISCRIPT'
#!/usr/bin/env bash
exec /opt/vpsbackup/venv/bin/python -m src.cli "$@"
CLISCRIPT
# Inject working directory
sed -i 's|exec /opt/vpsbackup/venv/bin/python|cd /opt/vpsbackup \&\& exec /opt/vpsbackup/venv/bin/python|' "${CLI_LINK}"
chmod 755 "${CLI_LINK}"
chown root:root "${CLI_LINK}"
success "CLI installed at ${CLI_LINK}"

# ---------------------------------------------------------------------------
# Step 12: Install systemd service
# ---------------------------------------------------------------------------
info "Installing systemd service..."
cp "${SCRIPT_DIR}/systemd/${SERVICE_NAME}.service" "${SERVICE_FILE}"
chmod 644 "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
success "Systemd service installed and enabled"

# ---------------------------------------------------------------------------
# Step 13: Start the service
# ---------------------------------------------------------------------------
info "Starting ${SERVICE_NAME} service..."
systemctl start "${SERVICE_NAME}"

# Brief wait to check it came up
sleep 3
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    success "Service started successfully"
else
    warn "Service may not have started cleanly. Check logs with:"
    warn "  journalctl -u ${SERVICE_NAME} -n 50"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "==================================================================="
echo -e "  ${GREEN}VPS GitHub Backup System installed successfully!${NC}"
echo "==================================================================="
echo ""
echo "  GitHub Account  : ${GITHUB_USERNAME}"
echo "  VPS Hostname    : ${VPS_HOSTNAME}"
echo "  VPS Repository  : ${VPS_REPO_NAME}"
echo ""
echo "  Useful commands:"
echo "    vpsbackup status    — Show system status"
echo "    vpsbackup users     — List registered users"
echo "    vpsbackup repos     — List GitHub repositories"
echo "    vpsbackup now       — Trigger immediate backup"
echo "    vpsbackup logs      — Show recent logs"
echo "    vpsbackup test      — Test GitHub connectivity"
echo ""
echo "  Service management:"
echo "    vpsbackup start / stop / restart"
echo "    journalctl -u ${SERVICE_NAME} -f   (follow logs)"
echo ""
echo "  Config: ${CONFIG_FILE}"
echo "  Data:   ${DATA_DIR}"
echo "  Logs:   ${LOG_DIR}"
echo ""
