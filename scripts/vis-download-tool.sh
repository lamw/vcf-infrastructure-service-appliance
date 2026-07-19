#!/bin/bash -eux

echo '> Installing optional VCF Download Tool...'

if [ "${VIS_INSTALL_VCF_DOWNLOAD_TOOL:-false}" != "true" ]; then
  echo '> VCF Download Tool build-time install is disabled; users can install VCFDT from the VIS Software Depot UI.'
  exit 0
fi

ARTIFACT_DIR="${VIS_OPTIONAL_ARTIFACT_DIR:-/tmp/vis-optional-files}"
INSTALL_ROOT="${VCF_DOWNLOAD_TOOL_INSTALL_ROOT:-/usr/local/lib/vcf-download-tool}"
BIN_DIR="${VCF_DOWNLOAD_TOOL_BIN_DIR:-/usr/local/bin}"

shopt -s nullglob
archives=("${ARTIFACT_DIR}"/vcf-download-tool-*.tar.gz /root/vcf-download-tool-*.tar.gz)
valid_archives=()
for candidate in "${archives[@]}"; do
  if [ -s "${candidate}" ]; then
    valid_archives+=("${candidate}")
  fi
done
archives=("${valid_archives[@]}")

if [ "${#archives[@]}" -eq 0 ]; then
  echo '> VCF Download Tool archive was not provided; skipping.'
  exit 0
fi

archive="${archives[0]}"
archive_name="$(basename "${archive}")"
tmpdir="$(mktemp -d)"

cleanup() {
  rm -rf "${tmpdir}"
}
trap cleanup EXIT

sudo install -d -o root -g root -m 755 "${INSTALL_ROOT}" "${BIN_DIR}"
if [ "${archive}" != "/root/${archive_name}" ]; then
  sudo cp "${archive}" "/root/${archive_name}"
fi
sudo rm -rf "${INSTALL_ROOT}"
sudo install -d -o root -g root -m 755 "${INSTALL_ROOT}"

tar -xzf "${archive}" -C "${tmpdir}"

tool_path="$(find "${tmpdir}" -type f -path '*/bin/vcf-download-tool' | head -n 1)"
if [ -z "${tool_path}" ]; then
  tool_path="$(find "${tmpdir}" -type f -name 'vcf-download-tool' | head -n 1)"
fi

if [ -z "${tool_path}" ]; then
  echo 'Unable to find vcf-download-tool executable in archive.' >&2
  find "${tmpdir}" -maxdepth 3 -type f | sort >&2
  exit 1
fi

relative_tool_path="${tool_path#${tmpdir}/}"
sudo cp -a "${tmpdir}/." "${INSTALL_ROOT}/"
sudo chmod +x "${INSTALL_ROOT}/${relative_tool_path}"
sudo ln -sfn "${INSTALL_ROOT}/${relative_tool_path}" "${BIN_DIR}/vcf-download-tool"

sudo tee /etc/profile.d/vis-download-tool.sh >/dev/null <<'EOF'
export PATH="/usr/local/bin:$PATH"
EOF
sudo chmod 644 /etc/profile.d/vis-download-tool.sh

if [ -f "${INSTALL_ROOT}/conf/obtu_telemetry/obtu-telemetry.properties" ]; then
  sudo sed -i 's/^obtu\.telemetry\.ceip=.*/obtu.telemetry.ceip=ENABLE/' "${INSTALL_ROOT}/conf/obtu_telemetry/obtu-telemetry.properties"
fi
sudo install -d -o root -g root -m 755 "${INSTALL_ROOT}/conf/telemetry"
printf 'obtu.telemetry.config=ENABLE\n' | sudo tee "${INSTALL_ROOT}/conf/telemetry/telemetry.flag" >/dev/null
sudo chmod 644 "${INSTALL_ROOT}/conf/telemetry/telemetry.flag"

sudo install -d -o root -g root -m 750 /opt/vis/config/depot
system_id_output="$(vcf-download-tool configuration generate --software-depot-id --force)"
system_id="$(printf '%s\n' "${system_id_output}" | grep -Eo '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' | tail -n 1)"
if [ -z "${system_id}" ]; then
  echo 'Unable to parse VCFDT System ID from vcf-download-tool output.' >&2
  printf '%s\n' "${system_id_output}" >&2
  exit 1
fi
printf '%s\n' "${system_id}" | sudo tee /opt/vis/config/depot/vcfdt-system-id >/dev/null
sudo chmod 600 /opt/vis/config/depot/vcfdt-system-id

echo "> VCF Download Tool installed at ${BIN_DIR}/vcf-download-tool"
