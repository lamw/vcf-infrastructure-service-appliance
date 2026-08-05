#!/bin/bash
set -euo pipefail

ARCHIVE=""
CHECKSUM_FILE=""
SIGNATURE_FILE=""
PUBLIC_KEY="${VIS_UPDATE_PUBLIC_KEY:-/etc/vis/update-signing.pub}"
WORK_DIR="${VIS_UPDATE_WORK_DIR:-/opt/vis/update}"
STATE_DIR="${VIS_UPDATE_STATE_DIR:-/opt/vis/state}"
LOG_FILE="${VIS_UPDATE_LOG_FILE:-${STATE_DIR}/vis-update.log}"
STATUS_FILE="${VIS_UPDATE_STATUS_FILE:-${STATE_DIR}/vis-update-status.json}"
LOCK_DIR="${VIS_UPDATE_LOCK_DIR:-/run/vis-update.lock}"
APPLY_SCRIPT="${VIS_APPLY_UPDATE_SCRIPT:-/usr/local/sbin/vis-apply-update}"

usage() {
  cat <<EOF
Usage: vis-offline-update --archive ZIP --sha256 SHA256_FILE --signature SIGNATURE_FILE

Verify a signed VIS release archive and apply the update without internet access.
The SHA256 file must be signed by the VIS release signing key.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --archive)
      ARCHIVE="${2:-}"
      shift 2
      ;;
    --sha256)
      CHECKSUM_FILE="${2:-}"
      shift 2
      ;;
    --signature)
      SIGNATURE_FILE="${2:-}"
      shift 2
      ;;
    --public-key)
      PUBLIC_KEY="${2:-}"
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

mkdir -p "${STATE_DIR}" "${WORK_DIR}"
touch "${LOG_FILE}"
chmod 600 "${LOG_FILE}" || true

write_status() {
  local state="$1"
  local message="$2"
  local commit="${3:-}"
  python3 - "$STATUS_FILE" "$state" "$message" "offline" "signed-archive" "$commit" <<'PY_STATUS'
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
PY_STATUS
}

fail() {
  local message="$1"
  write_status "failed" "$message"
  echo "$message" >&2
  exit 1
}

for required in "$ARCHIVE" "$CHECKSUM_FILE" "$SIGNATURE_FILE" "$PUBLIC_KEY"; do
  if [ -z "$required" ] || [ ! -f "$required" ]; then
    fail "Offline update requires a ZIP archive, SHA256 file, signature file, and trusted public key."
  fi
done

if [ ! -x "$APPLY_SCRIPT" ]; then
  fail "VIS apply script is not installed at ${APPLY_SCRIPT}."
fi
if ! command -v openssl >/dev/null 2>&1; then
  fail "openssl is required to verify signed offline updates."
fi
if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 is required to validate offline update archives."
fi

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  write_status "running" "VIS update is already running."
  echo "VIS update is already running." >&2
  exit 1
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null || true' EXIT

exec >>"${LOG_FILE}" 2>&1

echo "== VIS offline update started: $(date -u +"%Y-%m-%dT%H:%M:%SZ") =="
echo "Archive: ${ARCHIVE}"
echo "SHA256: ${CHECKSUM_FILE}"
echo "Signature: ${SIGNATURE_FILE}"
write_status "running" "Verifying signed VIS update archive."

if ! openssl pkeyutl -verify -rawin -pubin -inkey "$PUBLIC_KEY" -in "$CHECKSUM_FILE" -sigfile "$SIGNATURE_FILE"; then
  fail "Offline update signature verification failed."
fi

python3 - "$ARCHIVE" "$CHECKSUM_FILE" <<'PY_VERIFY'
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
checksum_file = pathlib.Path(sys.argv[2])
expected = None
archive_name = archive.name
for raw_line in checksum_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    parts = line.replace("*", " ").split()
    if len(parts) == 1:
        expected = parts[0]
        break
    if len(parts) >= 2 and pathlib.Path(parts[-1]).name == archive_name:
        expected = parts[0]
        break
    if expected is None:
        expected = parts[0]
if not expected or len(expected) != 64:
    raise SystemExit("SHA256 file does not contain a valid archive hash.")
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual.lower() != expected.lower():
    raise SystemExit("Offline update archive SHA256 does not match signed manifest.")
print("Archive SHA256 verified: {}".format(actual))
PY_VERIFY

STAMP="$(date -u +"%Y%m%d%H%M%S")"
STAGE_DIR="${WORK_DIR}/offline-${STAMP}"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

python3 - "$ARCHIVE" "$STAGE_DIR" <<'PY_EXTRACT'
import os
import pathlib
import shutil
import stat
import sys
import zipfile

archive = pathlib.Path(sys.argv[1])
stage = pathlib.Path(sys.argv[2]).resolve()
extract_dir = stage / "extract"
extract_dir.mkdir(parents=True, exist_ok=True)

with zipfile.ZipFile(archive) as zf:
    for info in zf.infolist():
        name = info.filename
        if not name or name.endswith("/"):
            continue
        target = (extract_dir / name).resolve()
        if not str(target).startswith(str(extract_dir) + os.sep):
            raise SystemExit("Archive contains an unsafe path: {}".format(name))
        mode = (info.external_attr >> 16) & 0o777777
        if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
            raise SystemExit("Archive contains an unsupported file type: {}".format(name))
    zf.extractall(extract_dir)

candidates = []
if (extract_dir / "vis").is_dir() and (extract_dir / "scripts" / "vis-apply-update.sh").is_file():
    candidates.append(extract_dir)
for child in extract_dir.iterdir():
    if child.is_dir() and (child / "vis").is_dir() and (child / "scripts" / "vis-apply-update.sh").is_file():
        candidates.append(child)
if not candidates:
    raise SystemExit("Archive does not contain a valid VIS release tree.")
source = candidates[0]
repo = stage / "repo"
if repo.exists():
    shutil.rmtree(repo)
shutil.copytree(source, repo, symlinks=False)
print(repo)
PY_EXTRACT

REPO_DIR="${STAGE_DIR}/repo"
VERSION="$(python3 - "$REPO_DIR" <<'PY_VERSION'
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
for path in (root / "vis-version.json", root / "vis" / "vis-version.json"):
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            print(payload.get("version") or payload.get("commit") or "offline")
            raise SystemExit(0)
        except Exception:
            pass
print("offline")
PY_VERSION
)"
write_status "running" "Applying signed VIS offline update ${VERSION}." "$VERSION"

VIS_UPDATE_SOURCE_DIR="$REPO_DIR" VIS_UPDATE_OFFLINE=true "$APPLY_SCRIPT"

write_status "complete" "Signed VIS offline update ${VERSION} applied successfully." "$VERSION"
echo "== VIS offline update complete: ${VERSION} =="
