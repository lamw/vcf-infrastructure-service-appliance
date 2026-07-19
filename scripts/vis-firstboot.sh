#!/bin/bash -eux

sudo mkdir -p /root/setup
sudo cp /tmp/rc.local /etc/rc.local
sudo cp /tmp/getOvfProperty.py /root/setup/getOvfProperty.py
sudo cp /tmp/setup.sh /root/setup/setup.sh
sudo cp /tmp/setup-01-os.sh /root/setup/setup-01-os.sh
sudo cp /tmp/setup-02-network.sh /root/setup/setup-02-network.sh
sudo cp /tmp/setup-03-vis.sh /root/setup/setup-03-vis.sh
sudo chmod +x /etc/rc.local /root/setup/*.sh /root/setup/getOvfProperty.py

sudo tee /etc/systemd/system/vis-firstboot.service >/dev/null <<'EOF'
[Unit]
Description=VIS first boot OVF customization
After=network-online.target open-vm-tools.service
Wants=network-online.target
ConditionPathExists=!/root/ran_customization

[Service]
Type=oneshot
ExecStart=/etc/rc.local
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable vis-firstboot.service
