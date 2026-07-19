#!/bin/bash
set -euo pipefail

OUTPUT_PATH="../output-vmware-iso"
OUTPUT_ABS=$(cd "${OUTPUT_PATH}" && pwd -P)
OVF_PATH=$(find "${OUTPUT_PATH}" -type f -iname "${VIS_APPLIANCE_NAME}.ovf" -exec dirname "{}" \; | head -n 1)

if [ -z "${OVF_PATH}" ]; then
    echo "Unable to locate ${VIS_APPLIANCE_NAME}.ovf under ${OUTPUT_PATH}"
    exit 1
fi

# Move ovf files in to a subdirectory of OUTPUT_PATH if not already
if [ "${OUTPUT_PATH}" = "${OVF_PATH}" ]; then
    mkdir -p "${OUTPUT_PATH}/${VIS_APPLIANCE_NAME}"
    find "${OUTPUT_PATH}" -maxdepth 1 -type f -exec mv "{}" "${OUTPUT_PATH}/${VIS_APPLIANCE_NAME}/" \;
    OVF_PATH="${OUTPUT_PATH}/${VIS_APPLIANCE_NAME}"
fi

rm -f "${OVF_PATH}/${VIS_APPLIANCE_NAME}.mf"

sed "s/{{VERSION}}/${VIS_VERSION}/g" "${VIS_OVF_TEMPLATE}" > vis.xml

if [ "$(uname)" == "Darwin" ]; then
    sed -i .bak1 's/<VirtualHardwareSection>/<VirtualHardwareSection ovf:transport="com.vmware.guestInfo">/g' "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i .bak2 "/    <\/vmw:BootOrderSection>/ r vis.xml" "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i .bak3 '/^      <vmw:ExtraConfig ovf:required="false" vmw:key="nvram".*$/d' "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i .bak4 "/^    <File ovf:href=\"${VIS_APPLIANCE_NAME}-file1.nvram\".*$/d" "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i .bak5 '/vmw:ExtraConfig.*/d' "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
else
    sed -i 's/<VirtualHardwareSection>/<VirtualHardwareSection ovf:transport="com.vmware.guestInfo">/g' "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i "/    <\/vmw:BootOrderSection>/ r vis.xml" "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i '/^      <vmw:ExtraConfig ovf:required="false" vmw:key="nvram".*$/d' "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i "/^    <File ovf:href=\"${VIS_APPLIANCE_NAME}-file1.nvram\".*$/d" "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
    sed -i '/vmw:ExtraConfig.*/d' "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf"
fi

rm -f "${OUTPUT_ABS}/${FINAL_VIS_APPLIANCE_NAME}.ova"
ovftool --overwrite "${OVF_PATH}/${VIS_APPLIANCE_NAME}.ovf" "${OUTPUT_ABS}/${FINAL_VIS_APPLIANCE_NAME}.ova"
rm -rf "${OVF_PATH}"
rm -f vis.xml
