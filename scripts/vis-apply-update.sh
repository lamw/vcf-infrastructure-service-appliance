#!/bin/bash
set -euo pipefail

SOURCE_DIR="${VIS_UPDATE_SOURCE_DIR:-}"
APP_ROOT="${VIS_APP_ROOT:-/opt/vis/app}"
STATE_DIR="${VIS_STATE_DIR:-/opt/vis/state}"
BACKUP_ROOT="${VIS_UPDATE_BACKUP_ROOT:-${STATE_DIR}/update-backups}"
OFFLINE_UPDATE="${VIS_UPDATE_OFFLINE:-false}"

if [ -z "${SOURCE_DIR}" ]; then
  SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

if [ ! -d "${SOURCE_DIR}/vis" ]; then
  echo "VIS source package not found under ${SOURCE_DIR}" >&2
  exit 1
fi

echo "> Applying VIS files from ${SOURCE_DIR}"
mkdir -p "${APP_ROOT}" "${BACKUP_ROOT}"

STAMP="$(date -u +"%Y%m%d%H%M%S")"
STAGED_APP="${APP_ROOT}/vis.update-${STAMP}"
CURRENT_APP="${APP_ROOT}/vis"
BACKUP_APP="${BACKUP_ROOT}/vis-${STAMP}"

rm -rf "${STAGED_APP}"
cp -a "${SOURCE_DIR}/vis" "${STAGED_APP}"
chown -R root:root "${STAGED_APP}"

if [ -d "${CURRENT_APP}" ]; then
  mv "${CURRENT_APP}" "${BACKUP_APP}"
fi
mv "${STAGED_APP}" "${CURRENT_APP}"

echo "> Updating VIS Python dependencies"
if [ ! -d "${APP_ROOT}/venv" ]; then
  if [ "${OFFLINE_UPDATE}" = "true" ]; then
    echo "VIS Python virtual environment is missing; offline update can not install dependencies." >&2
    exit 1
  fi
  python3 -m venv "${APP_ROOT}/venv"
fi
if [ "${OFFLINE_UPDATE}" = "true" ]; then
  "${APP_ROOT}/venv/bin/pip" install --no-index -r "${CURRENT_APP}/requirements.txt"
else
  "${APP_ROOT}/venv/bin/pip" install -r "${CURRENT_APP}/requirements.txt"
fi

echo "> Installing update helper scripts"
if [ -f "${SOURCE_DIR}/scripts/vis-update.sh" ]; then
  install -o root -g root -m 0755 "${SOURCE_DIR}/scripts/vis-update.sh" /usr/local/sbin/vis-update
fi
if [ -f "${SOURCE_DIR}/scripts/vis-apply-update.sh" ]; then
  install -o root -g root -m 0755 "${SOURCE_DIR}/scripts/vis-apply-update.sh" /usr/local/sbin/vis-apply-update
fi
if [ -f "${SOURCE_DIR}/scripts/vis-offline-update.sh" ]; then
  install -o root -g root -m 0755 "${SOURCE_DIR}/scripts/vis-offline-update.sh" /usr/local/sbin/vis-offline-update
fi
if [ -f "${SOURCE_DIR}/files/vis-update-signing.pub" ]; then
  install -o root -g root -m 0755 -d /etc/vis
  install -o root -g root -m 0644 "${SOURCE_DIR}/files/vis-update-signing.pub" /etc/vis/update-signing.pub
fi

echo "> Removing default Ubuntu Chrony NTP pools"
rm -f /etc/chrony/sources.d/ubuntu-ntp-pools.sources

echo "> Refreshing VIS systemd units"
if [ -f "${SOURCE_DIR}/files/vis-redirect.service" ]; then
  install -o root -g root -m 0644 "${SOURCE_DIR}/files/vis-redirect.service" /etc/systemd/system/vis-redirect.service
fi

systemctl daemon-reload
systemctl restart vis-redirect.service || true
systemctl restart vis-web.service

echo "> VIS services restarted"
