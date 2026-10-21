#!/usr/bin/env bash
# uninstall.sh — VPS GitHub Automatic Backup System Uninstaller
#
# Removes all installed components:
#   - systemd service (stopped, disabled, removed)
#   - Application files (/opt/vpsbackup)
#   - Configuration (/etc/vpsbackup) — optional
#   - Data (/var/lib/vpsbackup) — optional
#   - Logs (/var/log/vpsbackup) — optional
#   - CLI symlink (/usr/local/bin/vpsbackup)
#
# GitHub repositories are NEVER removed without explicit flag.
# Run as root: sudo bash uninstall.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "This uninstaller must be run as root: sudo bash uninstall.sh"

SERVICE_NAME="vpsbackup"
INSTALL_DIR="/opt/vpsbackup"
CONFIG_DIR="/etc/vpsbackup"
DATA_DIR="/var/lib/vpsbackup"
LOG_DIR="/var/log/vpsbackup"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CLI_LINK="/usr/local/bin/vpsbackup"

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
KEEP_CONFIG=true
KEEP_DATA=true
KEEP_LOGS=true
PURGE_ALL=false

usage() {
    echo "Usage: sudo bash uninstall.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --purge          Remove everything including config, data, and logs"
    echo "  --remove-config  Also remove /etc/vpsbackup (credentials + config)"
    echo "  --remove-data    Also remove /var/lib/vpsbackup (git repos, registry)"
    echo "  --remove-logs    Also remove /var/log/vpsbackup"
    echo "  -h, --help       Show this help message"
    echo ""
    echo "Note: GitHub repositories are NEVER removed by this script."
    echo "      To delete repos, use the GitHub web interface or API."
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --purge)
            PURGE_ALL=true
            KEEP_CONFIG=false
            KEEP_DATA=false
            KEEP_LOGS=false
            ;;
        --remove-config)
            KEEP_CONFIG=false
            ;;
        --remove-data)
            KEEP_DATA=false
            ;;
        --remove-logs)
            KEEP_LOGS=false
            ;;
        -h|--help)
            usage
            ;;
        *)
            die "Unknown option: $arg (use --help for usage)"
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------
echo ""
echo "==================================================================="
echo "  VPS GitHub Backup System — Uninstaller"
echo "==================================================================="
echo ""
echo "  Will remove:"
echo "    - Systemd service: ${SERVICE_FILE}"
echo "    - Application:     ${INSTALL_DIR}"
echo "    - CLI symlink:     ${CLI_LINK}"

if [[ ${KEEP_CONFIG} == false ]]; then
    echo "    - Configuration:   ${CONFIG_DIR}  (INCLUDES CREDENTIALS)"
fi
if [[ ${KEEP_DATA} == false ]]; then
    echo "    - Data directory:  ${DATA_DIR}  (INCLUDES LOCAL GIT REPOS)"
fi
if [[ ${KEEP_LOGS} == false ]]; then
    echo "    - Log directory:   ${LOG_DIR}"
fi

echo ""
echo "  Will NOT remove:"
if [[ ${KEEP_CONFIG} == true ]]; then
    echo "    - ${CONFIG_DIR}  (use --remove-config or --purge to remove)"
fi
if [[ ${KEEP_DATA} == true ]]; then
    echo "    - ${DATA_DIR}  (use --remove-data or --purge to remove)"
fi
if [[ ${KEEP_LOGS} == true ]]; then
    echo "    - ${LOG_DIR}  (use --remove-logs or --purge to remove)"
fi
echo "    - GitHub repositories (never removed automatically)"
echo ""

read -rp "  Continue? [y/N] " CONFIRM
echo ""
if [[ "${CONFIRM,,}" != "y" && "${CONFIRM,,}" != "yes" ]]; then
    echo "Uninstall cancelled."
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: Stop and disable systemd service
# ---------------------------------------------------------------------------
info "Stopping ${SERVICE_NAME} service..."
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    systemctl stop "${SERVICE_NAME}" || warn "Failed to stop service (may already be stopped)"
    success "Service stopped"
else
    info "Service was not running"
fi

if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    systemctl disable "${SERVICE_NAME}" || warn "Failed to disable service"
    success "Service disabled"
fi

# Remove PID file if leftover
rm -f /run/vpsbackup.pid

# ---------------------------------------------------------------------------
# Step 2: Remove systemd unit file
# ---------------------------------------------------------------------------
if [[ -f "${SERVICE_FILE}" ]]; then
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    success "Removed ${SERVICE_FILE}"
fi

# ---------------------------------------------------------------------------
# Step 3: Remove application files
# ---------------------------------------------------------------------------
if [[ -d "${INSTALL_DIR}" ]]; then
    info "Removing application files: ${INSTALL_DIR}"
    rm -rf "${INSTALL_DIR}"
    success "Removed ${INSTALL_DIR}"
else
    info "Application directory not found: ${INSTALL_DIR}"
fi

# ---------------------------------------------------------------------------
# Step 4: Remove CLI symlink
# ---------------------------------------------------------------------------
if [[ -f "${CLI_LINK}" ]] || [[ -L "${CLI_LINK}" ]]; then
    rm -f "${CLI_LINK}"
    success "Removed CLI: ${CLI_LINK}"
fi

# ---------------------------------------------------------------------------
# Step 5: Remove config (optional)
# ---------------------------------------------------------------------------
if [[ ${KEEP_CONFIG} == false ]]; then
    if [[ -d "${CONFIG_DIR}" ]]; then
        info "Removing configuration: ${CONFIG_DIR}"
        # Shred credentials before removal
        if [[ -f "${CONFIG_DIR}/credentials" ]]; then
            shred -u "${CONFIG_DIR}/credentials" 2>/dev/null || rm -f "${CONFIG_DIR}/credentials"
            success "Credentials securely removed"
        fi
        rm -rf "${CONFIG_DIR}"
        success "Removed ${CONFIG_DIR}"
    fi
else
    warn "Configuration preserved at ${CONFIG_DIR}"
    warn "Re-install will reuse existing config. Delete manually if needed."
fi

# ---------------------------------------------------------------------------
# Step 6: Remove data (optional)
# ---------------------------------------------------------------------------
if [[ ${KEEP_DATA} == false ]]; then
    if [[ -d "${DATA_DIR}" ]]; then
        info "Removing data directory: ${DATA_DIR}"
        rm -rf "${DATA_DIR}"
        success "Removed ${DATA_DIR}"
    fi
else
    warn "Data preserved at ${DATA_DIR}"
    warn "Registry and local git clones retained (GitHub repos unaffected)."
fi

# ---------------------------------------------------------------------------
# Step 7: Remove logs (optional)
# ---------------------------------------------------------------------------
if [[ ${KEEP_LOGS} == false ]]; then
    if [[ -d "${LOG_DIR}" ]]; then
        info "Removing log directory: ${LOG_DIR}"
        rm -rf "${LOG_DIR}"
        success "Removed ${LOG_DIR}"
    fi
else
    info "Logs preserved at ${LOG_DIR}"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "==================================================================="
echo -e "  ${GREEN}VPS GitHub Backup System uninstalled.${NC}"
echo "==================================================================="
echo ""
if [[ ${KEEP_CONFIG} == true ]]; then
    echo "  Config retained : ${CONFIG_DIR}"
fi
if [[ ${KEEP_DATA} == true ]]; then
    echo "  Data retained   : ${DATA_DIR}"
fi
if [[ ${KEEP_LOGS} == true ]]; then
    echo "  Logs retained   : ${LOG_DIR}"
fi
echo ""
echo "  GitHub repositories were NOT deleted."
echo "  To remove them, visit: https://github.com/settings/repositories"
echo ""
