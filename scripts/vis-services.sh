#!/bin/bash -eux

echo '> Installing VIS web application...'

VIS_FQDN="${VIS_APPLIANCE_FQDN:-vis.williamlam.local}"
VIS_IP="${VIS_APPLIANCE_IP:-192.168.30.99}"
VIS_ADMIN_USERNAME="${VIS_ADMIN_USERNAME:-admin}"
VIS_ADMIN_PASSWORD="${VIS_ADMIN_PASSWORD:-}"
VIS_SFTP_USER="${VIS_SFTP_USER:-}"
VIS_SFTP_PASSWORD="${VIS_SFTP_PASSWORD:-}"

if [ ! -d /tmp/vis ]; then
  echo 'VIS application payload was not uploaded to /tmp/vis' >&2
  exit 1
fi

sudo rm -rf /opt/vis/app/vis
sudo mkdir -p /opt/vis/app
sudo cp -a /tmp/vis /opt/vis/app/vis
sudo chown -R root:root /opt/vis/app/vis
sudo install -d -o root -g root -m 750 /opt/vis/config
sudo install -d -o root -g root -m 750 /opt/vis/config/tls
sudo install -d -o root -g root -m 750 /opt/vis/data/depot/.vis-upload-tmp

if [ ! -f /opt/vis/config/app-secret ]; then
  openssl rand -hex 32 | sudo tee /opt/vis/config/app-secret >/dev/null
  sudo chmod 600 /opt/vis/config/app-secret
fi
VIS_SECRET_KEY="$(sudo cat /opt/vis/config/app-secret)"

echo '> Creating VIS Python virtual environment...'
sudo python3 -m venv /opt/vis/app/venv
sudo /opt/vis/app/venv/bin/pip install --upgrade pip
sudo /opt/vis/app/venv/bin/pip install -r /opt/vis/app/vis/requirements.txt

echo '> Installing VIS web systemd service...'
sudo tee /etc/systemd/system/vis-web.service >/dev/null <<EOF
[Unit]
Description=VIS management web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/vis/app
Environment=PYTHONPATH=/opt/vis/app
Environment=VIS_DB_PATH=/opt/vis/state/vis.db
Environment=VIS_PORT=8080
Environment=VIS_APPLIANCE_FQDN=${VIS_FQDN}
Environment=VIS_APPLIANCE_IP=${VIS_IP}
Environment=VIS_ADMIN_USERNAME=${VIS_ADMIN_USERNAME}
Environment=VIS_ADMIN_PASSWORD=${VIS_ADMIN_PASSWORD}
Environment=VIS_SECRET_KEY=${VIS_SECRET_KEY}
Environment=VIS_ENABLE_LOCAL_ADAPTERS=1
Environment=VIS_UPLOAD_TMP_DIR=/opt/vis/data/depot/.vis-upload-tmp
ExecStart=/opt/vis/app/venv/bin/python -m vis.app
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

echo '> Installing VIS default landing redirect...'
sudo install -o root -g root -m 0644 /tmp/vis-redirect.service /etc/systemd/system/vis-redirect.service

echo '> Installing VIS update helpers...'
if [ -f /tmp/vis-update.sh ]; then
  sudo install -o root -g root -m 0755 /tmp/vis-update.sh /usr/local/sbin/vis-update
fi
if [ -f /tmp/vis-apply-update.sh ]; then
  sudo install -o root -g root -m 0755 /tmp/vis-apply-update.sh /usr/local/sbin/vis-apply-update
fi

sudo systemctl daemon-reload
sudo systemctl enable vis-web.service
sudo systemctl enable vis-redirect.service

echo '> Configuring VIS SFTP backup repository...'
SFTP_USER="${VIS_SFTP_USER}"
SFTP_PASSWORD="${VIS_SFTP_PASSWORD}"
SFTP_CHROOT="/opt/vis/data/sftp"
SFTP_BACKUP_DIR="${SFTP_CHROOT}/backup"

sudo install -d -o root -g root -m 755 "${SFTP_CHROOT}"
sudo install -d -o root -g root -m 755 "${SFTP_BACKUP_DIR}"

if [ -n "${SFTP_USER}" ] && [ -n "${SFTP_PASSWORD}" ]; then
  if ! id "${SFTP_USER}" >/dev/null 2>&1; then
    sudo useradd --home-dir /backup --shell /usr/sbin/nologin --no-create-home "${SFTP_USER}"
  fi

  echo "${SFTP_USER}:${SFTP_PASSWORD}" | sudo chpasswd
  sudo usermod --home /backup --shell /usr/sbin/nologin "${SFTP_USER}"
  sudo install -d -o "${SFTP_USER}" -g "${SFTP_USER}" -m 750 "${SFTP_BACKUP_DIR}"

  sudo mkdir -p /etc/ssh/sshd_config.d
  sudo tee /etc/ssh/sshd_config.d/99-vis-sftp.conf >/dev/null <<EOF
Match User ${SFTP_USER}
    ChrootDirectory ${SFTP_CHROOT}
    ForceCommand internal-sftp -d /backup
    PasswordAuthentication yes
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
EOF

  sudo sshd -t
  sudo systemctl reload ssh
else
  echo '> SFTP credentials were not provided; SFTP Backup will be configured from the VIS UI.'
fi
