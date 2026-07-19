#!/bin/bash -eux

export DEBIAN_FRONTEND=noninteractive

wait_for_apt_locks() {
  local locks=(
    /var/lib/dpkg/lock-frontend
    /var/lib/dpkg/lock
    /var/lib/apt/lists/lock
    /var/cache/apt/archives/lock
  )

  while sudo fuser "${locks[@]}" >/dev/null 2>&1; do
    echo '> Waiting for apt/dpkg locks...'
    sleep 5
  done
}

echo '> Installing Docker and Harbor...'

HARBOR_VERSION="${HARBOR_VERSION:-v2.15.1}"
VIS_FQDN="${VIS_APPLIANCE_FQDN:-vis.williamlam.local}"
VIS_IP="${VIS_APPLIANCE_IP:-192.168.30.99}"
HARBOR_PORT="${VIS_HARBOR_PORT:-9443}"
HARBOR_HTTP_PORT="${VIS_HARBOR_HTTP_PORT:-9080}"
HARBOR_ADMIN_PASSWORD="${VIS_HARBOR_ADMIN_PASSWORD:-}"
KEYCLOAK_IMAGE="${VIS_KEYCLOAK_IMAGE:-quay.io/keycloak/keycloak:26.3}"
VIS_POD_CIDR_NETWORK="${VIS_POD_CIDR_NETWORK:-10.10.0.0/16}"
REGISTRY_ROOT="/opt/vis/data/registry"
HARBOR_HOME="/opt/vis/harbor"
TLS_DIR="/opt/vis/config/tls"
HARBOR_AUTOSTART="false"

wait_for_apt_locks
sudo apt-get update
wait_for_apt_locks
sudo apt-get install -y docker.io docker-compose-v2 openssl curl ca-certificates tar gzip

if ! command -v docker >/dev/null 2>&1; then
  echo '> Docker installation failed; aborting Harbor provisioning.'
  exit 1
fi

sudo install -d -o root -g root -m 755 "${REGISTRY_ROOT}/containerd" "${REGISTRY_ROOT}/docker" "${REGISTRY_ROOT}/harbor" "${HARBOR_HOME}" /opt/vis/installers
sudo install -d -o root -g root -m 750 "${TLS_DIR}"

VIS_POD_CIDR_NETWORK="$(python3 - "${VIS_POD_CIDR_NETWORK}" <<'PY'
import ipaddress
import sys

network = ipaddress.ip_network(sys.argv[1], strict=False)
if network.version != 4 or network.prefixlen > 24:
    raise SystemExit("VIS_POD_CIDR_NETWORK must be an IPv4 CIDR with prefix /24 or larger")

print(network.with_prefixlen)
PY
)"

sudo tee /etc/docker/daemon.json >/dev/null <<EOF
{
  "data-root": "${REGISTRY_ROOT}/docker",
  "features": {
    "containerd-snapshotter": false
  },
  "default-address-pools": [
    {
      "base": "${VIS_POD_CIDR_NETWORK}",
      "size": 24
    }
  ]
}
EOF

sudo install -d -o root -g root -m 755 /etc/containerd /etc/systemd/system/containerd.service.d /etc/systemd/system/docker.service.d
sudo tee /etc/containerd/config.toml >/dev/null <<EOF
version = 2
root = "${REGISTRY_ROOT}/containerd"
state = "/run/containerd"
EOF

sudo tee /etc/systemd/system/containerd.service.d/vis-storage.conf >/dev/null <<EOF
[Unit]
RequiresMountsFor=${REGISTRY_ROOT}
EOF

sudo tee /etc/systemd/system/docker.service.d/vis-storage.conf >/dev/null <<EOF
[Unit]
RequiresMountsFor=${REGISTRY_ROOT}
EOF

sudo systemctl daemon-reload
sudo systemctl stop docker.socket || true
sudo systemctl stop docker || true
sudo systemctl stop containerd || true
sudo rm -rf /var/lib/docker
sudo rm -rf /var/lib/containerd
sudo systemctl enable --now containerd
sudo systemctl enable --now docker

echo "> Pre-pulling Keycloak image ${KEYCLOAK_IMAGE} when available..."
sudo docker pull "${KEYCLOAK_IMAGE}" || echo "> Keycloak image pre-pull skipped; VIS will pull it when OIDC Provider is enabled."

if [ -n "${HARBOR_ADMIN_PASSWORD}" ]; then
  if [ ! -f "${TLS_DIR}/server.crt" ] || [ ! -f "${TLS_DIR}/server.key" ]; then
    sudo openssl genrsa -out "${TLS_DIR}/rootCA.key" 4096
    sudo openssl req -x509 -new -nodes -key "${TLS_DIR}/rootCA.key" -sha256 -days 3650 -subj "/CN=VIS Root CA" -out "${TLS_DIR}/rootCA.pem"
    sudo openssl genrsa -out "${TLS_DIR}/server.key" 2048
    sudo tee "${TLS_DIR}/server-san.cnf" >/dev/null <<EOF
[req]
distinguished_name=req_distinguished_name
[req_distinguished_name]
[v3_req]
subjectAltName=DNS:${VIS_FQDN},IP:${VIS_IP}
EOF
    sudo openssl req -new -key "${TLS_DIR}/server.key" -subj "/CN=${VIS_FQDN}" -out "${TLS_DIR}/server.csr"
    sudo openssl x509 -req -in "${TLS_DIR}/server.csr" -CA "${TLS_DIR}/rootCA.pem" -CAkey "${TLS_DIR}/rootCA.key" -CAcreateserial -out "${TLS_DIR}/server.crt" -days 825 -sha256 -extfile "${TLS_DIR}/server-san.cnf" -extensions v3_req
    sudo sh -c "cat '${TLS_DIR}/server.crt' '${TLS_DIR}/rootCA.pem' > '${TLS_DIR}/vis-full.pem'"
    sudo chmod 600 "${TLS_DIR}/rootCA.key" "${TLS_DIR}/server.key"
  fi
fi

cd /opt/vis/installers
if [ ! -f "harbor-online-installer-${HARBOR_VERSION}.tgz" ]; then
  sudo curl -fL -o "harbor-online-installer-${HARBOR_VERSION}.tgz" "https://github.com/goharbor/harbor/releases/download/${HARBOR_VERSION}/harbor-online-installer-${HARBOR_VERSION}.tgz"
fi

sudo rm -rf "${HARBOR_HOME:?}/"*
sudo tar -xzf "harbor-online-installer-${HARBOR_VERSION}.tgz" -C "${HARBOR_HOME}" --strip-components=1
sudo cp "${HARBOR_HOME}/harbor.yml.tmpl" "${HARBOR_HOME}/harbor.yml"

if [ -z "${HARBOR_ADMIN_PASSWORD}" ]; then
  echo '> Harbor admin password was not provided; staging installer and pre-pulling images for VIS UI configuration.'
  STAGED_HARBOR_ADMIN_PASSWORD="$(openssl rand -hex 16)Aa1!"
  sudo python3 - <<PY
from pathlib import Path
p = Path("${HARBOR_HOME}/harbor.yml")
lines = p.read_text().splitlines()
rendered = []
section = ""
skip_https = False
for line in lines:
    stripped = line.strip()
    is_top_level = bool(line) and not line.startswith(" ") and not line.startswith("#")
    if skip_https:
        if is_top_level:
            skip_https = False
        else:
            continue
    if stripped == "https:":
        skip_https = True
        continue
    if is_top_level and stripped.endswith(":"):
        section = stripped[:-1]
    if line.startswith("hostname:"):
        rendered.append("hostname: ${VIS_FQDN}")
    elif section == "http" and stripped.startswith("port:"):
        rendered.append("  port: ${HARBOR_HTTP_PORT}")
    elif stripped.startswith("# external_url:") or stripped.startswith("external_url:"):
        continue
    elif stripped.startswith("harbor_admin_password:"):
        rendered.append("harbor_admin_password: ${STAGED_HARBOR_ADMIN_PASSWORD}")
    elif line.startswith("data_volume:"):
        rendered.append("data_volume: ${REGISTRY_ROOT}/harbor")
    else:
        rendered.append(line)
p.write_text("\\n".join(rendered) + "\\n")
PY

  cd "${HARBOR_HOME}"
  sudo ./prepare
  echo "> Pre-pulling Harbor images for ${HARBOR_VERSION}..."
  sudo docker compose -f "${HARBOR_HOME}/docker-compose.yml" pull
else
  HARBOR_AUTOSTART="true"
  sudo python3 - <<PY
from pathlib import Path
p = Path("${HARBOR_HOME}/harbor.yml")
s = p.read_text()
s = s.replace("hostname: reg.mydomain.com", "hostname: ${VIS_FQDN}")
s = s.replace("port: 80", "port: ${HARBOR_HTTP_PORT}", 1)
s = s.replace("  port: 443", "  port: ${HARBOR_PORT}", 1)
s = s.replace("  certificate: /your/certificate/path", "  certificate: ${TLS_DIR}/server.crt")
s = s.replace("  private_key: /your/private/key/path", "  private_key: ${TLS_DIR}/server.key")
s = s.replace("harbor_admin_password: Harbor12345", "harbor_admin_password: ${HARBOR_ADMIN_PASSWORD}")
s = s.replace("data_volume: /data", "data_volume: ${REGISTRY_ROOT}/harbor")
s = s.replace("# external_url: https://reg.mydomain.com:8433", "external_url: https://${VIS_FQDN}:${HARBOR_PORT}")
p.write_text(s)
PY

  cd "${HARBOR_HOME}"
  sudo ./install.sh
fi

sudo tee /etc/systemd/system/vis-harbor.service >/dev/null <<EOF
[Unit]
Description=VIS Harbor container registry
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${HARBOR_HOME}
ExecStart=/usr/bin/docker compose -f ${HARBOR_HOME}/docker-compose.yml up -d
ExecStop=/usr/bin/docker compose -f ${HARBOR_HOME}/docker-compose.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
if [ "${HARBOR_AUTOSTART}" = "true" ]; then
  sudo systemctl enable vis-harbor.service
else
  sudo systemctl disable vis-harbor.service || true
fi
