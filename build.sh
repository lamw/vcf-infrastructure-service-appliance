#!/bin/bash -x

export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/VMware OVF Tool:/Applications/VMware Fusion.app/Contents/Library/VMware OVF Tool:${PATH}"

echo "Building VIS OVA Appliance ..."
mkdir -p output-vmware-iso
find output-vmware-iso -mindepth 1 -maxdepth 1 -exec rm -rf "{}" \;

echo "Applying packer build to vis.json ..."
packer build -var-file=vis-builder.json -var-file=vis-version.json vis.json

echo "Creating split OVA release artifacts ..."
OVA_PATH=$(find output-vmware-iso -maxdepth 1 -type f -name "*.ova" | head -n 1)

if [ -z "${OVA_PATH}" ]; then
  echo "No OVA found in output-vmware-iso"
  exit 1
fi

OVA_NAME=$(basename "${OVA_PATH}")

(
  cd output-vmware-iso || exit 1
  shasum -a 256 "${OVA_NAME}" > "${OVA_NAME}.sha256"
  split -b 1900m "${OVA_NAME}" "${OVA_NAME}.part-"
  shasum -a 256 "${OVA_NAME}".part-* > "${OVA_NAME}.parts.sha256"
)

echo "Build artifacts:"
ls -lh output-vmware-iso/*.ova output-vmware-iso/*.sha256 output-vmware-iso/*.ova.part-*
