#!/bin/bash

set -euo pipefail

# Extract all OVF Properties
DEBUG=$(/root/setup/getOvfProperty.py "guestinfo.debug")
HOSTNAME=$(/root/setup/getOvfProperty.py "guestinfo.hostname")
IP_ADDRESS=$(/root/setup/getOvfProperty.py "guestinfo.ipaddress")
NETMASK=$(/root/setup/getOvfProperty.py "guestinfo.netmask" | awk -F ' ' '{print $1}')
GATEWAY=$(/root/setup/getOvfProperty.py "guestinfo.gateway")
DNS_SERVER=$(/root/setup/getOvfProperty.py "guestinfo.dns")
DNS_DOMAIN=$(/root/setup/getOvfProperty.py "guestinfo.domain")
NTP_SERVER=$(/root/setup/getOvfProperty.py "guestinfo.ntp")
ROOT_PASSWORD=$(/root/setup/getOvfProperty.py "guestinfo.root_password")
SSH_PUBLIC_KEY=$(/root/setup/getOvfProperty.py "guestinfo.ssh_public_key")
VIS_ADMIN_USERNAME=$(/root/setup/getOvfProperty.py "guestinfo.vis_admin_username")
VIS_ADMIN_PASSWORD=$(/root/setup/getOvfProperty.py "guestinfo.vis_admin_password")
POD_CIDR_NETWORK=$(/root/setup/getOvfProperty.py "guestinfo.pod_cidr_network")

if [ -e /root/ran_customization ]; then
    exit
else
    VIS_LOG_FILE=/var/log/bootstrap.log
    if [ "${DEBUG}" = "True" ]; then
        VIS_LOG_FILE=/var/log/bootstrap-debug.log
        set -x
        exec 2>> "${VIS_LOG_FILE}"
        echo
        echo "### WARNING -- DEBUG LOG CONTAINS EXECUTED COMMANDS AND MAY INCLUDE CREDENTIALS -- WARNING ###"
        echo
    fi

    echo -e "\e[92mStarting VIS customization ..." > /dev/console

    echo -e "\e[92mStarting OS configuration ..." > /dev/console
    . /root/setup/setup-01-os.sh

    echo -e "\e[92mStarting network configuration ..." > /dev/console
    . /root/setup/setup-02-network.sh

    echo -e "\e[92mStarting VIS service staging ..." > /dev/console
    . /root/setup/setup-03-vis.sh

    echo -e "\e[92mVIS customization completed ..." > /dev/console

    vmtoolsd --cmd "info-set guestinfo.ovfEnv NULL" || true
    touch /root/ran_customization
fi
