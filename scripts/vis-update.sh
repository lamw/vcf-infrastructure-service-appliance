#!/bin/bash
set -euo pipefail

REPO_URL="${VIS_UPDATE_REPO_URL:-https://github.com/lamw/vcf-infrastructure-service-appliance.git}"
BRANCH="${VIS_UPDATE_BRANCH:-main}"
WORK_DIR="${VIS_UPDATE_WORK_DIR:-/opt/vis/update}"
STATE_DIR="${VIS_UPDATE_STATE_DIR:-/opt/vis/state}"
LOG_FILE="${VIS_UPDATE_LOG_FILE:-${STATE_DIR}/vis-update.log}"
STATUS_FILE="${VIS_UPDATE_STATUS_FILE:-${STATE_DIR}/vis-update-status.json}"
LOCK_DIR="${VIS_UPDATE_LOCK_DIR:-/run/vis-update.lock}"

usage() {
  cat <<EOF
Usage: vis-update [--repo-url URL] [--branch BRANCH]

Pull the latest VIS appliance code from GitHub and apply web application,
script, systemd, and Python dependency updates without redeploying the OVA.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo-url)
      REPO_URL="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "${REPO_URL}" ]; then
  echo "Repository URL is required." >&2
  exit 2
fi

if [ -z "${BRANCH}" ]; then
  echo "Branch is required." >&2
  exit 2
fi

mkdir -p "${STATE_DIR}" "${WORK_DIR}"
touch "${LOG_FILE}"
chmod 600 "${LOG_FILE}" || true

write_status() {
  local state="$1"
  local message="$2"
  local commit="${3:-}"
  python3 - "$STATUS_FILE" "$state" "$message" "$REPO_URL" "$BRANCH" "$commit" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, state, message, repo_url, branch, commit = sys.argv[1:7]
payload = {
    "state": state,
    "message": message,
    "repo_url": repo_url,
    "branch": branch,
    "commit": commit,
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

if ! command -v git >/dev/null 2>&1; then
  write_status "failed" "git is not installed on this appliance."
  echo "git is not installed on this appliance." >&2
  exit 1
fi

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  write_status "running" "VIS update is already running."
  echo "VIS update is already running." >&2
  exit 1
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT

exec >>"${LOG_FILE}" 2>&1

echo "== VIS update started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") =="
echo "Repository: ${REPO_URL}"
echo "Branch: ${BRANCH}"
write_status "running" "Fetching VIS updates from ${BRANCH}."

REPO_DIR="${WORK_DIR}/repo"
if [ ! -d "${REPO_DIR}/.git" ]; then
  rm -rf "${REPO_DIR}"
  git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${REPO_DIR}"
else
  git -C "${REPO_DIR}" remote set-url origin "${REPO_URL}"
  git -C "${REPO_DIR}" fetch --depth 1 origin "${BRANCH}"
  git -C "${REPO_DIR}" checkout -B "${BRANCH}" "origin/${BRANCH}"
  git -C "${REPO_DIR}" reset --hard "origin/${BRANCH}"
fi

COMMIT="$(git -C "${REPO_DIR}" rev-parse --short HEAD)"
write_status "running" "Applying VIS update ${COMMIT}." "${COMMIT}"

if [ ! -x "${REPO_DIR}/scripts/vis-apply-update.sh" ]; then
  echo "Update hook scripts/vis-apply-update.sh is missing or not executable." >&2
  write_status "failed" "Update hook is missing in repository checkout." "${COMMIT}"
  exit 1
fi

VIS_UPDATE_SOURCE_DIR="${REPO_DIR}" "${REPO_DIR}/scripts/vis-apply-update.sh"

write_status "complete" "VIS update ${COMMIT} applied successfully." "${COMMIT}"
echo "== VIS update complete: ${COMMIT} =="
