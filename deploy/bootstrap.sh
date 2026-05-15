#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 24.04 VM for the Sportbet live monitor.
# Run as root or with sudo. Idempotent — safe to re-run.
#
#   curl -fsSL https://raw.githubusercontent.com/lukasbecker36-dot/Sportbet/HEAD/deploy/bootstrap.sh | sudo bash
#
# After this finishes:
#   1. scp your config.py to /home/sportbet/Sportbet/config.py
#   2. sudo systemctl start sportbet
#   3. sudo journalctl -u sportbet -f
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/lukasbecker36-dot/Sportbet.git}"
# Empty BRANCH = use the repo's default branch (set on GitHub).
BRANCH="${BRANCH:-}"
TARGET_USER=sportbet
HOME_DIR="/home/${TARGET_USER}"
APP_DIR="${HOME_DIR}/Sportbet"

echo "[1/6] apt update + install base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git ca-certificates curl tzdata unattended-upgrades

echo "[2/6] timezone -> UTC"
timedatectl set-timezone UTC

echo "[3/6] non-root user '${TARGET_USER}'"
if ! id "${TARGET_USER}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${TARGET_USER}"
fi

echo "[4/6] clone / update repo at ${APP_DIR} (branch=${BRANCH:-<default>})"
sudo -u "${TARGET_USER}" BRANCH="${BRANCH}" REPO_URL="${REPO_URL}" bash <<'INNER'
set -euo pipefail
HOME_DIR="$HOME"
APP_DIR="$HOME_DIR/Sportbet"
cd "$HOME_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
    if [ -n "${BRANCH}" ]; then
        git clone --branch "${BRANCH}" "${REPO_URL}" Sportbet
    else
        git clone "${REPO_URL}" Sportbet
    fi
else
    cd Sportbet
    git fetch --all --prune
    if [ -n "${BRANCH}" ]; then
        git checkout "${BRANCH}"
        git pull --ff-only origin "${BRANCH}"
    else
        git pull --ff-only
    fi
fi
INNER

echo "[5/6] python venv + deps"
sudo -u "${TARGET_USER}" bash <<INNER
set -euo pipefail
cd "${APP_DIR}"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -U pip wheel
.venv/bin/pip install -q -r requirements.txt
INNER

echo "[6/6] systemd unit"
install -m 0644 "${APP_DIR}/deploy/sportbet.service" /etc/systemd/system/sportbet.service
systemctl daemon-reload
systemctl enable sportbet.service

cat <<'NEXT'

Bootstrap complete.

Next steps (from your laptop):

    # Push your config.py to the VM (replace <IP> with the VM IP)
    scp sportbet/config.py root@<IP>:/home/sportbet/Sportbet/config.py
    ssh root@<IP> 'chown sportbet:sportbet /home/sportbet/Sportbet/config.py && chmod 600 /home/sportbet/Sportbet/config.py'

Then start + tail:

    sudo systemctl start sportbet
    sudo journalctl -u sportbet -f

To deploy code updates later:

    sudo systemctl stop sportbet
    sudo -u sportbet bash -c 'cd /home/sportbet/Sportbet && git pull && .venv/bin/pip install -q -r requirements.txt'
    sudo systemctl start sportbet
NEXT
