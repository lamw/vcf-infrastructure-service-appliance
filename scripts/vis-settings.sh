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

echo '> Applying Ubuntu package updates...'
wait_for_apt_locks
sudo apt-get update
wait_for_apt_locks
sudo apt-get -y upgrade

echo '> Installing VIS base packages...'
wait_for_apt_locks
sudo apt-get install -y \
  ca-certificates \
  chrony \
  cloud-guest-utils \
  curl \
  dnsmasq-base \
  git \
  logrotate \
  net-tools \
  open-vm-tools \
  openssh-server \
  ldap-utils \
  linuxptp \
  python3 \
  python3-pip \
  python3-venv \
  slapd \
  tar \
  unbound \
  unzip \
  wget

echo '> Staging DNS and LDAP services for VIS control...'
sudo rm -f /etc/chrony/sources.d/ubuntu-ntp-pools.sources
sudo systemctl disable --now chrony || true
sudo systemctl disable --now unbound || true
sudo systemctl disable --now slapd || true
sudo systemctl disable --now vis-kms || true

echo '> Expanding root filesystem to available OS disk capacity...'
if sudo vgs vg_vis >/dev/null 2>&1 && sudo lvs /dev/vg_vis/root >/dev/null 2>&1; then
  sudo lvextend -r -l +100%FREE /dev/vg_vis/root || true
fi

echo '> Creating VIS directories...'
sudo mkdir -p \
  /opt/vis/app \
  /opt/vis/config \
  /opt/vis/data/dns \
  /opt/vis/data/registry \
  /opt/vis/data/sftp/backup \
  /opt/vis/data/depot \
  /opt/vis/data/identity \
  /opt/vis/data/time \
  /opt/vis/data/dhcp \
  /opt/vis/data/kms \
  /opt/vis/state \
  /root/setup

echo '> Enabling SSH and VMware tools...'
sudo systemctl enable ssh
sudo systemctl enable open-vm-tools
