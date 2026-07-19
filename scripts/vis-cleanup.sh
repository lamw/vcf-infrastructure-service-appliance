#!/bin/bash -eux

export DEBIAN_FRONTEND=noninteractive

echo '> Clearing apt cache...'
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*

echo '> Removing transient logs...'
sudo truncate -s 0 /var/log/wtmp || true
sudo find /var/log -type f -exec truncate -s 0 {} \; || true
sudo rm -rf /var/log/journal/* || true
sudo rm -f /var/lib/dhcp/* || true

echo '> Zeroing free space...'
sudo dd if=/dev/zero of=/EMPTY bs=1M || true
sudo sync
sudo rm -f /EMPTY
sudo sync

echo '> Done'
