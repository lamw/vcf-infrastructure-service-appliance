#!/bin/bash
# Deploy VIS Appliance directly to a standalone ESX host using OVF Tool.
# Author: William Lam

set -euo pipefail

OVFTOOL="/Applications/VMware OVF Tool/ovftool"
VIS_OVA="./output-vmware-iso/vcf-infrastructure-services-appliance-1.0.2.ova"

# ESX deployment target
ESX_HOST="172.30.0.10"
ESX_USERNAME="root"
ESX_PASSWORD="VMware1!"
VM_NETWORK="VM Network"
VM_DATASTORE="local-vmfs-datastore-1"
VM_NAME="vis"

# VIS appliance networking
VIS_FQDN="vis.vcf.lab"
VIS_IP="172.30.0.9"
VIS_NETMASK="24 (255.255.255.0)"
VIS_GATEWAY="172.30.0.1"
VIS_DNS_SERVER="192.168.30.29"
VIS_DNS_DOMAIN="vcf.lab"
VIS_NTP_SERVER="pool.ntp.org"

# Appliance OS credentials
VIS_ROOT_PASSWORD="VMware1!"

# Optional root SSH public key. Leave empty to skip.
VIS_SSH_PUBLIC_KEY_FILE=""

# VIS web application administrator credentials.
# This is separate from the appliance OS/root credential.
VIS_ADMIN_USERNAME="admin"
VIS_ADMIN_PASSWORD="VMware1!"

# Advanced: Docker container address pool used by VIS services such as Harbor.
# Change this if 10.10.0.0/16 overlaps with your lab network.
VIS_POD_CIDR_NETWORK="10.10.0.0/16"

# Enable verbose first-boot logging. Debug logs may include credentials.
VIS_DEBUG="false"

### DO NOT EDIT BEYOND HERE ###

if [[ ! -x "${OVFTOOL}" ]]; then
    echo "ovftool not found or is not executable: ${OVFTOOL}"
    exit 1
fi

if [[ ! -f "${VIS_OVA}" ]]; then
    echo "VIS OVA not found: ${VIS_OVA}"
    exit 1
fi

VIS_SSH_PUBLIC_KEY=""
if [[ -n "${VIS_SSH_PUBLIC_KEY_FILE}" ]]; then
    if [[ ! -f "${VIS_SSH_PUBLIC_KEY_FILE}" ]]; then
        echo "SSH public key file not found: ${VIS_SSH_PUBLIC_KEY_FILE}"
        exit 1
    fi
    VIS_SSH_PUBLIC_KEY="$(cat "${VIS_SSH_PUBLIC_KEY_FILE}")"
fi

echo -e "\nDeploying VIS Appliance ${VM_NAME} to ${ESX_HOST} ..."

"${OVFTOOL}" \
    --acceptAllEulas \
    --noSSLVerify \
    --skipManifestCheck \
    --X:injectOvfEnv \
    --allowExtraConfig \
    --X:waitForIp \
    --sourceType=OVA \
    --powerOn \
    "--net:VM Network=${VM_NETWORK}" \
    "--datastore=${VM_DATASTORE}" \
    --diskMode=thin \
    "--name=${VM_NAME}" \
    "--prop:guestinfo.hostname=${VIS_FQDN}" \
    "--prop:guestinfo.ipaddress=${VIS_IP}" \
    "--prop:guestinfo.netmask=${VIS_NETMASK}" \
    "--prop:guestinfo.gateway=${VIS_GATEWAY}" \
    "--prop:guestinfo.dns=${VIS_DNS_SERVER}" \
    "--prop:guestinfo.domain=${VIS_DNS_DOMAIN}" \
    "--prop:guestinfo.ntp=${VIS_NTP_SERVER}" \
    "--prop:guestinfo.root_password=${VIS_ROOT_PASSWORD}" \
    "--prop:guestinfo.ssh_public_key=${VIS_SSH_PUBLIC_KEY}" \
    "--prop:guestinfo.vis_admin_username=${VIS_ADMIN_USERNAME}" \
    "--prop:guestinfo.vis_admin_password=${VIS_ADMIN_PASSWORD}" \
    "--prop:guestinfo.pod_cidr_network=${VIS_POD_CIDR_NETWORK}" \
    "--prop:guestinfo.debug=${VIS_DEBUG}" \
    "${VIS_OVA}" \
    "vi://${ESX_USERNAME}:${ESX_PASSWORD}@${ESX_HOST}/"

echo -e "\nVIS deployment submitted. After first boot customization completes, open:"
echo "  http://${VIS_FQDN}"
echo "  http://${VIS_IP}"
