#!/bin/bash

set -euo pipefail

echo -e "\e[92mConfiguring static IP address ..." > /dev/console

normalize_ovf_list() {
    local raw="${1:-}"
    printf '%s\n' "${raw}" | tr ',' '\n' | awk '{$1=$1}; NF {print}'
}

join_lines_with_space() {
    local value=""
    local item
    while IFS= read -r item; do
        [ -z "${item}" ] && continue
        if [ -z "${value}" ]; then
            value="${item}"
        else
            value="${value} ${item}"
        fi
    done
    printf '%s' "${value}"
}

DNS_SERVERS=$(normalize_ovf_list "${DNS_SERVER}")
NTP_SERVERS=$(normalize_ovf_list "${NTP_SERVER}")
DNS_RESOLVED_VALUE=$(printf '%s\n' "${DNS_SERVERS}" | join_lines_with_space)
NTP_VALUE=$(printf '%s\n' "${NTP_SERVERS}" | join_lines_with_space)

if [ -z "${DNS_RESOLVED_VALUE}" ]; then
    echo "No DNS server was provided through OVF property guestinfo.dns" > /dev/console
    exit 1
fi
if [ -z "${NTP_VALUE}" ]; then
    echo "No NTP server was provided through OVF property guestinfo.ntp" > /dev/console
    exit 1
fi

DNS_NETPLAN_ADDRESSES=$(printf '%s\n' "${DNS_SERVERS}" | sed 's/^/          - /')
DNS_SYSTEMD_LINES=$(printf '%s\n' "${DNS_SERVERS}" | sed 's/^/DNS=/')

PRIMARY_NIC=$(ip -o link show | awk -F': ' '$2 != "lo" {print $2; exit}' | cut -d@ -f1)

rm -f /etc/netplan/00-installer-config.yaml

cat > /etc/netplan/99-vis-appliance.yaml <<EOF
network:
  version: 2
  ethernets:
    ${PRIMARY_NIC}:
      dhcp4: false
      addresses:
        - ${IP_ADDRESS}/${NETMASK}
      routes:
        - to: default
          via: ${GATEWAY}
      nameservers:
        addresses:
${DNS_NETPLAN_ADDRESSES}
        search:
          - ${DNS_DOMAIN}
EOF

chmod 600 /etc/netplan/99-vis-appliance.yaml
netplan apply

mkdir -p /etc/systemd/network
cat > /etc/systemd/network/zzzzzz-vis-appliance.network <<EOF
[Match]
Name=${PRIMARY_NIC}

[Network]
DHCP=no
Address=${IP_ADDRESS}/${NETMASK}
${DNS_SYSTEMD_LINES}
Domains=${DNS_DOMAIN}

[Route]
Destination=0.0.0.0/0
Gateway=${GATEWAY}
EOF

rm -f /run/systemd/network/zzzz-dracut-default.network
systemctl restart systemd-networkd

echo -e "\e[92mConfiguring systemd-resolved for VIS DNS ..." > /dev/console
mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/vis.conf <<EOF
[Resolve]
DNS=${DNS_RESOLVED_VALUE}
Domains=${DNS_DOMAIN}
DNSStubListener=no
EOF
ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
systemctl restart systemd-resolved || true

echo -e "\e[92mConfiguring NTP ..." > /dev/console
mkdir -p /etc/systemd
if [ ! -f /etc/systemd/timesyncd.conf ]; then
    cat > /etc/systemd/timesyncd.conf <<EOF
[Time]
NTP=${NTP_VALUE}
EOF
else
    sed -i '/^#\?NTP=/d' /etc/systemd/timesyncd.conf
    grep -q '^\[Time\]' /etc/systemd/timesyncd.conf || echo '[Time]' >> /etc/systemd/timesyncd.conf
    echo "NTP=${NTP_VALUE}" >> /etc/systemd/timesyncd.conf
fi
systemctl restart systemd-timesyncd || true

echo -e "\e[92mConfiguring hostname ..." > /dev/console
echo "${IP_ADDRESS} ${HOSTNAME}" >> /etc/hosts
hostnamectl set-hostname "${HOSTNAME}"
