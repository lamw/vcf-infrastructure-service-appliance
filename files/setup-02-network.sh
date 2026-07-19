#!/bin/bash

set -euo pipefail

echo -e "\e[92mConfiguring static IP address ..." > /dev/console

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
          - ${DNS_SERVER}
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
DNS=${DNS_SERVER}
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
DNS=${DNS_SERVER}
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
NTP=${NTP_SERVER}
EOF
else
    sed -i "s/^#NTP=.*/NTP=${NTP_SERVER}/" /etc/systemd/timesyncd.conf
    grep -q "^NTP=" /etc/systemd/timesyncd.conf || echo "NTP=${NTP_SERVER}" >> /etc/systemd/timesyncd.conf
fi
systemctl restart systemd-timesyncd || true

echo -e "\e[92mConfiguring hostname ..." > /dev/console
echo "${IP_ADDRESS} ${HOSTNAME}" >> /etc/hosts
hostnamectl set-hostname "${HOSTNAME}"
