#!/bin/bash

set -euo pipefail

systemctl enable ssh
systemctl start ssh

echo -e "\e[92mConfiguring root password ..." > /dev/console
echo "root:${ROOT_PASSWORD}" | /usr/sbin/chpasswd

if [ -n "${SSH_PUBLIC_KEY}" ]; then
    echo -e "\e[92mConfiguring root SSH public key ..." > /dev/console
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    touch /root/.ssh/authorized_keys
    grep -qxF "${SSH_PUBLIC_KEY}" /root/.ssh/authorized_keys || echo "${SSH_PUBLIC_KEY}" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    chown -R root:root /root/.ssh
    sed -i -E "s/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/" /etc/ssh/sshd_config
    grep -q "^PermitRootLogin" /etc/ssh/sshd_config || echo "PermitRootLogin prohibit-password" >> /etc/ssh/sshd_config
    systemctl reload ssh
fi

mkdir -p \
  /opt/vis/config \
  /opt/vis/data/dns \
  /opt/vis/data/registry \
  /opt/vis/data/sftp/backup \
  /opt/vis/data/depot \
  /opt/vis/data/identity \
  /opt/vis/data/time \
  /opt/vis/data/dhcp \
  /opt/vis/data/kms \
  /opt/vis/state
