#!/bin/bash

set -euo pipefail

echo -e "\e[92mConfiguring VIS web service ..." > /dev/console

VIS_POD_CIDR_NETWORK="${POD_CIDR_NETWORK:-10.10.0.0/16}"

normalize_pod_cidr() {
    python3 - "$VIS_POD_CIDR_NETWORK" <<'PY'
import ipaddress
import sys

try:
    network = ipaddress.ip_network(sys.argv[1], strict=False)
except ValueError:
    sys.exit(1)

if network.version != 4 or network.prefixlen > 24:
    sys.exit(1)

print(network.with_prefixlen)
PY
}

configure_docker_network_pool() {
    local raw_pod_cidr="${VIS_POD_CIDR_NETWORK}"
    if ! VIS_POD_CIDR_NETWORK="$(normalize_pod_cidr)"; then
        echo -e "\e[91mInvalid Pod CIDR Network '${raw_pod_cidr}'. Expected an IPv4 CIDR with prefix /24 or larger." > /dev/console
        return 1
    fi

    if [ -d /etc/docker ]; then
        cat > /etc/docker/daemon.json <<EOF
{
  "data-root": "/opt/vis/data/registry/docker",
  "default-address-pools": [
    {
      "base": "${VIS_POD_CIDR_NETWORK}",
      "size": 24
    }
  ]
}
EOF
        if systemctl list-unit-files docker.service >/dev/null 2>&1; then
            systemctl restart docker.service
        fi
    fi
}

configure_docker_network_pool

if [ -f /etc/systemd/system/vis-web.service ]; then
    mkdir -p /etc/systemd/system/vis-web.service.d
    cat > /etc/systemd/system/vis-web.service.d/ovf-env.conf <<EOF
[Service]
Environment=VIS_APPLIANCE_FQDN=${HOSTNAME}
Environment=VIS_APPLIANCE_IP=${IP_ADDRESS}
Environment=VIS_ADMIN_USERNAME=${VIS_ADMIN_USERNAME}
Environment=VIS_ADMIN_PASSWORD=${VIS_ADMIN_PASSWORD}
Environment=VIS_POD_CIDR_NETWORK=${VIS_POD_CIDR_NETWORK}
EOF
    systemctl daemon-reload
    systemctl enable vis-web.service
    systemctl restart vis-web.service
fi
