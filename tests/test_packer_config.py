import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackerOptionalArtifactTest(unittest.TestCase):
    def setUp(self):
        with open(ROOT / "vis.json", "r", encoding="utf-8") as handle:
            self.config = json.load(handle)

    def test_packer_stages_optional_vcf_download_tool_archive(self):
        provisioners = self.config["provisioners"]
        local_stage = provisioners[0]

        self.assertEqual("false", self.config["variables"]["install_vcf_download_tool"])
        self.assertEqual("shell-local", local_stage["type"])
        self.assertIn("mkdir -p http/optional-artifacts", local_stage["inline"])
        self.assertTrue(
            any(
                "vcf-download-tool-*" in command
                and "artifacts" in command
                and "vcf-download-tool-local.tar.gz" in command
                and "install_vcf_download_tool" in command
                for command in local_stage["inline"]
            )
        )

        shell_inline = [
            command
            for provisioner in provisioners
            if provisioner["type"] == "shell"
            for command in provisioner.get("inline", [])
        ]
        downloader = next(
            provisioner
            for provisioner in provisioners
            if provisioner["type"] == "shell"
            and any("PACKER_HTTP_ADDR" in command for command in provisioner.get("inline", []))
        )
        self.assertIn("mkdir -p /tmp/vis-optional-files", shell_inline)
        self.assertNotIn("execute_command", downloader)
        self.assertTrue(
            any(
                "curl -fsS -o /tmp/vis-optional-files/vcf-download-tool-local.tar.gz" in command
                and "optional-artifacts/vcf-download-tool-local.tar.gz" in command
                for command in shell_inline
            )
        )

    def test_packer_runs_vcf_download_tool_installer(self):
        installer = next(
            provisioner
            for provisioner in self.config["provisioners"]
            if "scripts/vis-download-tool.sh" in provisioner.get("scripts", [])
        )

        self.assertIn("VIS_INSTALL_VCF_DOWNLOAD_TOOL={{ user `install_vcf_download_tool` }}", installer["environment_vars"])

    def test_vcf_download_tool_installer_is_optional_and_uses_standard_path(self):
        script = (ROOT / "scripts" / "vis-download-tool.sh").read_text(encoding="utf-8")

        self.assertIn("VIS_INSTALL_VCF_DOWNLOAD_TOOL", script)
        self.assertIn("VCF Download Tool build-time install is disabled", script)
        self.assertIn("vcf-download-tool-*.tar.gz", script)
        self.assertIn("valid_archives", script)
        self.assertIn("VCF Download Tool archive was not provided; skipping.", script)
        self.assertIn("/root/${archive_name}", script)
        self.assertIn('if [ "${archive}" != "/root/${archive_name}" ]; then', script)
        self.assertIn("/usr/local/lib/vcf-download-tool", script)
        self.assertIn("/usr/local/bin", script)
        self.assertIn("ln -sfn", script)
        self.assertIn("/etc/profile.d/vis-download-tool.sh", script)
        self.assertIn("obtu.telemetry.config=ENABLE", script)
        self.assertIn("conf/telemetry/telemetry.flag", script)
        self.assertIn("vcf-download-tool configuration generate --software-depot-id", script)
        self.assertIn("--force", script)
        self.assertIn("/opt/vis/config/depot/vcfdt-system-id", script)

    def test_iso_is_loaded_from_user_artifacts_directory(self):
        with open(ROOT / "vis-version.json", "r", encoding="utf-8") as handle:
            version_config = json.load(handle)

        self.assertEqual("artifacts/ubuntu-26.04-live-server-amd64.iso", version_config["iso_url"])

    def test_vmx_annotation_uses_vmx_safe_newline_encoding(self):
        annotation = self.config["builders"][0]["vmx_data"]["annotation"]

        self.assertIn("VCF Infrastructure Services Appliance", annotation)
        self.assertIn("|0AVersion:", annotation)
        self.assertNotIn("\n", annotation)

    def test_packer_exposes_keycloak_image_bom_variable(self):
        self.assertEqual("quay.io/keycloak/keycloak:26.3", self.config["variables"]["keycloak_image"])
        harbor_provisioner = next(
            provisioner
            for provisioner in self.config["provisioners"]
            if "scripts/vis-harbor.sh" in provisioner.get("scripts", [])
        )

        self.assertIn("VIS_KEYCLOAK_IMAGE={{ user `keycloak_image` }}", harbor_provisioner["environment_vars"])
        script = (ROOT / "scripts" / "vis-harbor.sh").read_text(encoding="utf-8")
        self.assertIn("docker pull", script)
        self.assertIn("Keycloak image pre-pull skipped", script)

    def test_packer_prepulls_harbor_images_without_autostarting_service(self):
        script = (ROOT / "scripts" / "vis-harbor.sh").read_text(encoding="utf-8")

        self.assertIn("staging installer and pre-pulling images", script)
        self.assertIn("STAGED_HARBOR_ADMIN_PASSWORD", script)
        self.assertIn("sudo ./prepare", script)
        self.assertIn('docker compose -f "${HARBOR_HOME}/docker-compose.yml" pull', script)
        self.assertIn('HARBOR_AUTOSTART="false"', script)
        self.assertIn('sudo systemctl disable vis-harbor.service || true', script)

    def test_packer_sftp_uses_chroot_authorized_keys_path(self):
        script = (ROOT / "scripts" / "vis-services.sh").read_text(encoding="utf-8")

        self.assertIn('sudo install -d -o "${SFTP_USER}" -g "${SFTP_USER}" -m 700 "${SFTP_BACKUP_DIR}/.ssh"', script)
        self.assertIn("AuthorizedKeysFile ${SFTP_BACKUP_DIR}/.ssh/authorized_keys", script)

    def test_packer_places_containerd_storage_on_registry_disk(self):
        script = (ROOT / "scripts" / "vis-harbor.sh").read_text(encoding="utf-8")

        self.assertIn('"${REGISTRY_ROOT}/containerd"', script)
        self.assertIn('"containerd-snapshotter": false', script)
        self.assertIn("root = \"${REGISTRY_ROOT}/containerd\"", script)
        self.assertIn('state = "/run/containerd"', script)
        self.assertIn("/etc/systemd/system/containerd.service.d/vis-storage.conf", script)
        self.assertIn("/etc/systemd/system/docker.service.d/vis-storage.conf", script)
        self.assertIn("RequiresMountsFor=${REGISTRY_ROOT}", script)
        self.assertIn("systemctl stop docker.socket", script)
        self.assertIn("rm -rf /var/lib/docker", script)
        self.assertIn("rm -rf /var/lib/containerd", script)

    def test_ovf_defaults_use_public_safe_lab_values(self):
        template = (ROOT / "manual" / "vis.xml.template").read_text(encoding="utf-8")

        self.assertEqual("vis.vcf.lab", self.config["variables"]["vis_appliance_fqdn"])
        self.assertEqual("172.30.0.9", self.config["variables"]["vis_appliance_ip"])
        self.assertIn('ovf:key="guestinfo.hostname" ovf:type="string" ovf:userConfigurable="true" ovf:value="vis.vcf.lab"', template)
        self.assertIn('ovf:key="guestinfo.ipaddress" ovf:type="string" ovf:userConfigurable="true" ovf:value="172.30.0.9"', template)
        self.assertIn('ovf:key="guestinfo.gateway" ovf:type="string" ovf:userConfigurable="true" ovf:value="172.30.0.1"', template)
        self.assertIn('ovf:key="guestinfo.dns" ovf:type="string" ovf:userConfigurable="true" ovf:value="192.168.30.29"', template)
        self.assertIn('ovf:key="guestinfo.domain" ovf:type="string" ovf:userConfigurable="true" ovf:value="vcf.lab"', template)
        self.assertIn("One or more DNS servers. Separate multiple values with commas", template)
        self.assertIn("One or more NTP servers. Separate multiple values with commas", template)
        self.assertIn('ovf:key="guestinfo.ssh_public_key" ovf:type="string" ovf:userConfigurable="true" ovf:value=""', template)
        self.assertNotIn("ssh-rsa ", template)
        self.assertNotIn("ssh-ed25519 ", template)

    def test_service_disk_sizes_match_expected_layout(self):
        with open(ROOT / "vis-version.json", "r", encoding="utf-8") as handle:
            version_config = json.load(handle)

        expected = {
            "disk_size": "40960",
            "depot_disk_size": "204800",
            "sftp_disk_size": "15360",
            "registry_disk_size": "61440",
            "dns_disk_size": "2048",
            "identity_disk_size": "2048",
        }
        for key, value in expected.items():
            self.assertEqual(value, self.config["variables"][key])
            self.assertEqual(value, version_config[key])

    def test_packer_installs_dns_ldap_and_time_backends_disabled_by_default(self):
        script = (ROOT / "scripts" / "vis-settings.sh").read_text(encoding="utf-8")
        requirements = (ROOT / "vis" / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("git", script)
        self.assertIn("unbound", script)
        self.assertIn("slapd", script)
        self.assertIn("ldap-utils", script)
        self.assertIn("chrony", script)
        self.assertIn("dnsmasq-base", script)
        self.assertIn("linuxptp", script)
        self.assertIn("PyKMIP", requirements)
        self.assertIn("systemctl disable --now unbound", script)
        self.assertIn("systemctl disable --now slapd", script)
        self.assertIn("systemctl disable --now chrony", script)
        self.assertIn("systemctl disable --now vis-kms", script)

    def test_packer_installs_update_helpers(self):
        services_script = (ROOT / "scripts" / "vis-services.sh").read_text(encoding="utf-8")
        update_script = (ROOT / "scripts" / "vis-update.sh").read_text(encoding="utf-8")
        apply_script = (ROOT / "scripts" / "vis-apply-update.sh").read_text(encoding="utf-8")
        offline_script = (ROOT / "scripts" / "vis-offline-update.sh").read_text(encoding="utf-8")
        signing_key = (ROOT / "files" / "vis-update-signing.pub").read_text(encoding="utf-8")
        file_provisioners = [
            provisioner
            for provisioner in self.config["provisioners"]
            if provisioner["type"] == "file"
        ]

        self.assertTrue(
            any(
                provisioner["source"] == "scripts/vis-update.sh"
                and provisioner["destination"] == "/tmp/vis-update.sh"
                for provisioner in file_provisioners
            )
        )
        self.assertTrue(
            any(
                provisioner["source"] == "scripts/vis-apply-update.sh"
                and provisioner["destination"] == "/tmp/vis-apply-update.sh"
                for provisioner in file_provisioners
            )
        )
        self.assertTrue(
            any(
                provisioner["source"] == "scripts/vis-offline-update.sh"
                and provisioner["destination"] == "/tmp/vis-offline-update.sh"
                for provisioner in file_provisioners
            )
        )
        self.assertTrue(
            any(
                provisioner["source"] == "files/vis-update-signing.pub"
                and provisioner["destination"] == "/tmp/vis-update-signing.pub"
                for provisioner in file_provisioners
            )
        )
        self.assertIn("/usr/local/sbin/vis-update", services_script)
        self.assertIn("/usr/local/sbin/vis-apply-update", services_script)
        self.assertIn("/usr/local/sbin/vis-offline-update", services_script)
        self.assertIn("/etc/vis/update-signing.pub", services_script)
        self.assertIn("https://github.com/lamw/vcf-infrastructure-service-appliance.git", update_script)
        self.assertIn("git clone", update_script)
        self.assertIn("scripts/vis-apply-update.sh", update_script)
        self.assertNotIn("install --upgrade pip", apply_script)
        self.assertIn("VIS_UPDATE_OFFLINE", apply_script)
        self.assertIn('pip" install --no-index -r', apply_script)
        self.assertIn('pip" install -r', apply_script)
        self.assertIn("systemctl restart vis-web.service", apply_script)
        self.assertIn("vis-offline-update", apply_script)
        self.assertIn("openssl pkeyutl -verify", offline_script)
        self.assertIn("Archive SHA256 verified", offline_script)
        self.assertIn("unsafe path", offline_script)
        self.assertIn("/usr/local/sbin/vis-apply-update", offline_script)
        self.assertIn("VIS_UPDATE_OFFLINE=true", offline_script)
        self.assertIn("BEGIN PUBLIC KEY", signing_key)

    def test_packer_creates_service_data_directories(self):
        settings_script = (ROOT / "scripts" / "vis-settings.sh").read_text(encoding="utf-8")
        firstboot_script = (ROOT / "files" / "setup-01-os.sh").read_text(encoding="utf-8")

        for path in (
            "/opt/vis/data/depot",
            "/opt/vis/data/sftp/backup",
            "/opt/vis/data/registry",
            "/opt/vis/data/dns",
            "/opt/vis/data/identity",
            "/opt/vis/data/time",
            "/opt/vis/data/dhcp",
            "/opt/vis/data/kms",
        ):
            self.assertIn(path, settings_script)
            self.assertIn(path, firstboot_script)

    def test_packer_installs_default_port_80_redirect(self):
        script = (ROOT / "scripts" / "vis-services.sh").read_text(encoding="utf-8")
        unit = (ROOT / "files" / "vis-redirect.service").read_text(encoding="utf-8")
        file_provisioners = [
            provisioner
            for provisioner in self.config["provisioners"]
            if provisioner["type"] == "file"
        ]

        self.assertTrue(
            any(
                provisioner["source"] == "files/vis-redirect.service"
                and provisioner["destination"] == "/tmp/vis-redirect.service"
                for provisioner in file_provisioners
            )
        )
        self.assertIn("/tmp/vis-redirect.service", script)
        self.assertIn("/etc/systemd/system/vis-redirect.service", script)
        self.assertIn("systemctl enable vis-redirect.service", script)
        self.assertIn("VIS_REDIRECT_PORT=80", unit)
        self.assertIn("VIS_TARGET_PORT=8080", unit)
        self.assertIn("python -m vis.redirect", unit)

    def test_firstboot_removes_installer_dhcp_netplan_before_static_network(self):
        script = (ROOT / "files" / "setup-02-network.sh").read_text(encoding="utf-8")

        self.assertIn("rm -f /etc/netplan/00-installer-config.yaml", script)
        self.assertLess(
            script.index("rm -f /etc/netplan/00-installer-config.yaml"),
            script.index("cat > /etc/netplan/99-vis-appliance.yaml"),
        )

    def test_firstboot_supports_multiple_dns_and_ntp_ovf_values(self):
        script = (ROOT / "files" / "setup-02-network.sh").read_text(encoding="utf-8")
        deploy_script = (ROOT / "scripts" / "deploy_vis_esx.sh").read_text(encoding="utf-8")

        self.assertIn("normalize_ovf_list", script)
        self.assertIn("tr ','", script)
        self.assertIn('DNS_SERVERS=$(normalize_ovf_list "${DNS_SERVER}")', script)
        self.assertIn('NTP_SERVERS=$(normalize_ovf_list "${NTP_SERVER}")', script)
        self.assertIn('DNS_NETPLAN_ADDRESSES=$(printf', script)
        self.assertIn('${DNS_SYSTEMD_LINES}', script)
        self.assertIn('DNS=${DNS_RESOLVED_VALUE}', script)
        self.assertIn('NTP=${NTP_VALUE}', script)
        self.assertIn('VIS_DNS_SERVERS="192.168.30.29"', deploy_script)
        self.assertIn('VIS_NTP_SERVERS="pool.ntp.org"', deploy_script)
        self.assertIn('Separate multiple DNS or NTP servers with commas.', deploy_script)



if __name__ == "__main__":
    unittest.main()
