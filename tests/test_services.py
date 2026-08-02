import io
import os
import json
import subprocess
import stat
import tarfile
import tempfile
import time
import unittest
import signal
from collections import namedtuple
from pathlib import Path
from unittest.mock import PropertyMock, patch

from vis.file_manager import RepositoryFileManager
from vis.manager import DHCPServerAdapter, DNSServiceAdapter, LDAPProviderAdapter, LocalSFTPServiceAdapter, OIDCProviderAdapter, PyKMIPServiceAdapter, ServiceManager, TimeServerAdapter
from vis.redirect import VISRedirectHandler
from vis.store import ServiceStore
from vis.web import create_app, _render_harbor_config, _storage_health


class VISRedirectTest(unittest.TestCase):
    def test_redirects_default_http_landing_to_vis_ui_port(self):
        handler = VISRedirectHandler.__new__(VISRedirectHandler)
        handler.headers = {"Host": "vis.example.local"}
        handler.path = "/services/web-depot?x=1"
        handler.response_status = None
        handler.response_headers = {}
        handler.send_response = lambda status: setattr(handler, "response_status", status)
        handler.send_header = lambda key, value: handler.response_headers.__setitem__(key, value)
        handler.end_headers = lambda: None

        handler._redirect()

        self.assertEqual(302, handler.response_status)
        self.assertEqual(
            "http://vis.example.local:8080/services/web-depot?x=1",
            handler.response_headers["Location"],
        )


class ServiceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "vis.db")
        self.store = ServiceStore(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_initial_services_are_seeded(self):
        services = self.store.list_services()

        self.assertEqual(9, len(services))
        self.assertEqual(
            {"web-depot", "sftp-backup", "harbor-registry", "ldap-provider", "oidc-provider", "unbound-dns", "time-server", "dhcp-server", "kms-service"},
            {service.id for service in services},
        )
        self.assertEqual(["DNS Server", "NTP Server", "DHCP Server", "Key Management Service"], [service.name for service in services[-4:]])

    def test_service_model_contains_control_plane_fields(self):
        service = self.store.get_service("web-depot")

        self.assertEqual("Software Depot", service.name)
        self.assertFalse(service.enabled)
        self.assertFalse(service.configured)
        self.assertEqual("needs_configuration", service.health_status)
        self.assertEqual("http://vis.williamlam.local:8081/", service.endpoint)
        self.assertEqual("/opt/vis/data/depot", service.filesystem_root)
        self.assertEqual("http", service.settings["protocol"])
        self.assertFalse(service.settings["tls_enabled"])
        self.assertEqual(8081, service.settings["port"])
        self.assertFalse(service.last_validation_result.ok)

    def test_initial_services_are_disabled_by_default(self):
        for service in self.store.list_services():
            self.assertFalse(service.enabled, service.id)
            self.assertFalse(service.configured, service.id)
            self.assertEqual("needs_configuration", service.health_status)

    def test_catalog_reconcile_migrates_identity_provider_ids(self):
        service = self.store.get_service("ldap-provider")
        service.id = "directory-identity-provider"
        service.settings["base_dn"] = "dc=williamlam,dc=local"
        self.store.save_service(service)

        oidc = self.store.get_service("oidc-provider")
        oidc.id = "oidc-identity-provider"
        oidc.settings["realm"] = "vis"
        self.store.save_service(oidc)
        self.store.reconcile_catalog()

        self.assertEqual("ldap-provider", self.store.get_service("directory-identity-provider").id)
        self.assertEqual("oidc-provider", self.store.get_service("oidc-identity-provider").id)
        self.assertEqual("dc=williamlam,dc=local", self.store.get_service("ldap-provider").settings["base_dn"])
        self.assertEqual("vis", self.store.get_service("oidc-provider").settings["realm"])

    def test_service_credentials_are_not_seeded_with_default_passwords(self):
        sftp = self.store.get_service("sftp-backup")
        harbor = self.store.get_service("harbor-registry")
        oidc = self.store.get_service("oidc-provider")

        self.assertEqual("", sftp.settings["user"])
        self.assertEqual("", sftp.settings["password"])
        self.assertEqual("", harbor.settings["admin_password"])
        self.assertEqual("", oidc.settings["admin_password"])
        self.assertEqual("VCF", oidc.settings["realm"])
        self.assertEqual("vcf-admins", oidc.settings["default_group"])

    def test_identity_providers_seed_standard_groups(self):
        for service_id in ("ldap-provider", "oidc-provider"):
            service = self.store.get_service(service_id)
            group_names = {group["name"] for group in service.settings["groups"]}

            self.assertIn("vcf-admins", group_names)
            self.assertIn("vcf-users", group_names)


class ServiceManagerTest(unittest.TestCase):
    def test_mock_health_check_updates_without_system_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            manager = ServiceManager(store)

            service = manager.run_health_check("harbor-registry")

            self.assertEqual("needs_configuration", service.health_status)
            self.assertIsNotNone(service.last_health_check_time)

    def test_starting_service_is_monitored_after_enable(self):
        class TransientStartingAdapter:
            def __init__(self, service, calls):
                self.service = service
                self.calls = calls

            def enable(self):
                self.service.enabled = True
                self.service.configured = True
                self.service.health_status = "starting"
                return self.service

            def health_check(self):
                self.calls.append("health_check")
                self.service.enabled = True
                self.service.configured = True
                self.service.health_status = "healthy"
                return self.service

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            manager = ServiceManager(store, health_followup_attempts=2, health_followup_interval=0)
            calls = []
            manager.adapter_for = lambda service_id: TransientStartingAdapter(manager.get_service(service_id), calls)
            manager._start_health_followup_thread = lambda service_id: manager._followup_health_checks(service_id)

            result = manager.enable_service("harbor-registry")
            stored = manager.get_service("harbor-registry")

        self.assertEqual("starting", result.health_status)
        self.assertEqual(["health_check"], calls)
        self.assertEqual("healthy", stored.health_status)

    def test_sftp_health_check_verifies_backend_configuration(self):
        UserInfo = namedtuple("UserInfo", "pw_uid pw_gid")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("sftp-backup")
            service.settings["user"] = "vis-backup"
            service.filesystem_root = tmpdir
            adapter = LocalSFTPServiceAdapter(service)

            with patch.object(adapter, "_user_info", return_value=UserInfo(os.getuid(), os.getgid())), \
                patch.object(adapter, "_chroot_permissions_ok", return_value=True), \
                patch.object(adapter, "_ssh_active", return_value=True), \
                patch.object(adapter, "_sshd_config_valid", return_value=True), \
                patch.object(adapter, "_vis_sftp_configured", return_value=True):
                result = adapter.health_check()

        self.assertEqual("healthy", result.health_status)
        self.assertTrue(result.last_validation_result.ok)
        self.assertIn("SFTP backend verified", result.last_validation_result.message)

    def test_sftp_health_check_reports_missing_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("sftp-backup")
            service.filesystem_root = tmpdir
            adapter = LocalSFTPServiceAdapter(service)

            with patch.object(adapter, "_user_info", return_value=None), \
                patch.object(adapter, "_chroot_permissions_ok", return_value=True), \
                patch.object(adapter, "_ssh_active", return_value=True), \
                patch.object(adapter, "_sshd_config_valid", return_value=True), \
                patch.object(adapter, "_vis_sftp_configured", return_value=True):
                result = adapter.health_check()

        self.assertEqual("needs_configuration", result.health_status)
        self.assertFalse(result.last_validation_result.ok)
        self.assertIn("SFTP credentials are not configured", result.last_validation_result.message)

    def test_oidc_provider_requires_admin_password(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("oidc-provider")
            adapter = OIDCProviderAdapter(service)

            result = adapter.validate()

        self.assertFalse(result.ok)
        self.assertIn("Admin password", result.message)

    def test_oidc_provider_config_validation_accepts_realm_and_group(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("oidc-provider")
            service.settings["admin_password"] = "VMware1!"
            adapter = OIDCProviderAdapter(service)

            result = adapter.validate()

        self.assertTrue(result.ok)

    def test_oidc_https_stages_shared_tls_for_keycloak_container(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("oidc-provider")
            tls_dir = Path(tmpdir, "shared-tls")
            tls_dir.mkdir()
            for name in ("rootCA.pem", "server.crt", "server.key"):
                Path(tls_dir, name).write_text(name, encoding="utf-8")
            service.filesystem_root = os.path.join(tmpdir, "oidc")
            service.settings.update(
                {
                    "protocol": "https",
                    "port": 9444,
                    "admin_user": "admin",
                    "admin_password": "KeycloakPassword1!",
                    "tls_ca_path": str(tls_dir / "rootCA.pem"),
                    "tls_cert_path": str(tls_dir / "server.crt"),
                    "tls_key_path": str(tls_dir / "server.key"),
                }
            )
            adapter = OIDCProviderAdapter(service)
            adapter.config_dir = os.path.join(tmpdir, "oidc-config")
            adapter.unit_path = os.path.join(tmpdir, "vis-identity.service")
            adapter.theme_source_dir = os.path.join(tmpdir, "keycloak-theme")
            Path(adapter.theme_source_dir, "login").mkdir(parents=True)
            Path(adapter.theme_source_dir, "login", "theme.properties").write_text("parent=keycloak\n", encoding="utf-8")

            def local_tls_paths():
                staged = Path(adapter.config_dir, "tls")
                return {
                    "ca": str(staged / "rootCA.pem"),
                    "cert": str(staged / "server.crt"),
                    "key": str(staged / "server.key"),
                }

            with patch.object(adapter, "_apply_shared_tls_settings"), \
                patch.object(adapter, "_keycloak_tls_paths", side_effect=local_tls_paths), \
                patch("vis.manager.subprocess.run") as run:
                adapter._write_unit()

            tls_paths = local_tls_paths()
            self.assertEqual("server.key", Path(tls_paths["key"]).read_text(encoding="utf-8"))
            self.assertEqual(0o640, Path(tls_paths["key"]).stat().st_mode & 0o777)
            with open(adapter.unit_path, "r", encoding="utf-8") as handle:
                unit = handle.read()
            self.assertIn("-v {}:/opt/keycloak/vis-tls:ro".format(str(Path(tls_paths["cert"]).parent)), unit)
            self.assertIn("-v {}:/opt/keycloak/themes/vis:ro".format(str(Path(adapter.config_dir, "themes", "vis"))), unit)
            self.assertEqual("parent=keycloak\n", Path(adapter.config_dir, "themes", "vis", "login", "theme.properties").read_text(encoding="utf-8"))
            self.assertIn("1000:0", run.call_args_list[-1][0][0])

    def test_oidc_adapter_sets_vis_login_theme_on_realm_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("oidc-provider")
            service.settings["admin_password"] = "VMware1!"
            adapter = OIDCProviderAdapter(service)
            calls = []

            def fake_kc_json(method, path, token, payload=None):
                calls.append((method, path, payload))
                if method == "GET" and path == "/admin/realms/VCF":
                    return {"realm": "VCF", "enabled": True}
                if method == "GET" and path.startswith("/admin/realms/VCF/groups"):
                    return [{"id": "group-admins", "name": "vcf-admins"}, {"id": "group-users", "name": "vcf-users"}]
                return {}

            with patch.object(adapter, "_admin_token", return_value="token"), \
                patch.object(adapter, "_kc_json", side_effect=fake_kc_json):
                adapter._sync_realm_group_users_and_clients()

        realm_update = next(payload for method, path, payload in calls if method == "PUT" and path == "/admin/realms/VCF")
        self.assertEqual("vis", realm_update["loginTheme"])
        self.assertTrue(realm_update["enabled"])

    def test_oidc_adapter_creates_confidential_client_with_direct_grants(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("oidc-provider")
            service.settings["admin_password"] = "VMware1!"
            adapter = OIDCProviderAdapter(service)
            calls = []

            def fake_kc_json(method, path, token, payload=None):
                calls.append((method, path, payload))
                if path.endswith("/clients?clientId=vcf-sso"):
                    post_seen = any(call[0] == "POST" and call[1].endswith("/clients") for call in calls)
                    return [{"id": "kc-client-id", "clientId": "vcf-sso"}] if post_seen else []
                if path.endswith("/client-secret"):
                    return {"value": "generated-secret"}
                return {}

            with patch.object(adapter, "_admin_token", return_value="token"), \
                patch.object(adapter, "_ensure_realm"), \
                patch.object(adapter, "_kc_json", side_effect=fake_kc_json):
                client = adapter.ensure_oidc_client({"client_id": "vcf-sso", "redirect_url": "https://vcf.example.com/callback"})

        self.assertEqual("kc-client-id", client["keycloak_id"])
        self.assertEqual("generated-secret", client["client_secret"])
        create_payload = next(payload for method, path, payload in calls if method == "POST" and path.endswith("/clients"))
        self.assertFalse(create_payload["publicClient"])
        self.assertTrue(create_payload["directAccessGrantsEnabled"])
        self.assertEqual(["https://vcf.example.com/callback"], create_payload["redirectUris"])

    def test_dns_adapter_writes_unbound_config_and_controls_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("unbound-dns")
            service.settings["domain"] = "williamlam.local"
            service.settings["entries"] = [
                {"id": "1", "name": "sddc-manager.williamlam.local", "address": "192.168.30.60", "ttl": 300}
            ]
            adapter = DNSServiceAdapter(service)
            adapter.config_path = os.path.join(tmpdir, "unbound", "vis.conf")
            adapter.check_config_path = os.path.join(tmpdir, "unbound.conf")
            adapter.resolved_dropin_dir = os.path.join(tmpdir, "resolved.conf.d")
            adapter.resolved_dropin_path = os.path.join(adapter.resolved_dropin_dir, "vis-dns.conf")
            adapter.resolv_conf_path = os.path.join(tmpdir, "resolv.conf")
            adapter.systemd_resolved_conf_path = os.path.join(tmpdir, "systemd-resolved-resolv.conf")
            Path(adapter.systemd_resolved_conf_path).write_text("nameserver 192.168.30.29\n", encoding="utf-8")

            with patch.object(adapter, "_config_valid", return_value=True), \
                patch.object(adapter, "_service_active", return_value=True), \
                patch("vis.manager.subprocess.run") as run:
                result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            with open(adapter.config_path, "r", encoding="utf-8") as handle:
                rendered = handle.read()
            self.assertIn('local-zone: "williamlam.local." static', rendered)
            self.assertIn("access-control: 0.0.0.0/0 allow", rendered)
            self.assertIn('local-data: "sddc-manager.williamlam.local. 300 IN A 192.168.30.60"', rendered)
            self.assertIn('local-data-ptr: "192.168.30.60 sddc-manager.williamlam.local."', rendered)
            with open(adapter.resolved_dropin_path, "r", encoding="utf-8") as handle:
                resolved_dropin = handle.read()
            self.assertIn("DNSStubListener=no", resolved_dropin)
            self.assertTrue(os.path.islink(adapter.resolv_conf_path))
            self.assertEqual(adapter.systemd_resolved_conf_path, os.readlink(adapter.resolv_conf_path))
            commands = [call[0][0] for call in run.call_args_list]
            self.assertIn(["systemctl", "restart", "systemd-resolved"], commands)
            self.assertIn(["systemctl", "enable", "--now", "unbound.service"], commands)

    def test_dns_adapter_allows_enable_with_domain_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("unbound-dns")
            service.settings["domain"] = "williamlam.local"
            service.settings["entries"] = []
            adapter = DNSServiceAdapter(service)
            adapter.config_path = os.path.join(tmpdir, "unbound", "vis.conf")
            adapter.check_config_path = os.path.join(tmpdir, "unbound.conf")
            adapter.resolved_dropin_dir = os.path.join(tmpdir, "resolved.conf.d")
            adapter.resolved_dropin_path = os.path.join(adapter.resolved_dropin_dir, "vis-dns.conf")
            adapter.resolv_conf_path = os.path.join(tmpdir, "resolv.conf")
            adapter.systemd_resolved_conf_path = os.path.join(tmpdir, "systemd-resolved-resolv.conf")
            Path(adapter.systemd_resolved_conf_path).write_text("nameserver 192.168.30.29\n", encoding="utf-8")

            with patch.object(adapter, "_config_valid", return_value=True), \
                patch.object(adapter, "_service_active", return_value=True), \
                patch("vis.manager.subprocess.run") as run:
                result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            self.assertTrue(result.configured)
            self.assertTrue(result.enabled)
            self.assertTrue(result.last_validation_result.ok)
            self.assertEqual("DNS Server domain is configured", result.last_validation_result.message)
            with open(adapter.config_path, "r", encoding="utf-8") as handle:
                rendered = handle.read()
            self.assertIn('local-zone: "williamlam.local." static', rendered)
            self.assertIn("access-control: 0.0.0.0/0 allow", rendered)
            self.assertIn("# Add DNS entries to render local-data records.", rendered)
            self.assertNotIn("local-data:", rendered)
            commands = [call[0][0] for call in run.call_args_list]
            self.assertIn(["systemctl", "enable", "--now", "unbound.service"], commands)

    def test_time_server_adapter_writes_chrony_config_and_controls_service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("time-server")
            service.settings["allowed_clients"] = ["172.30.0.0/24"]
            service.settings["upstream_sources"] = ["time.google.com"]
            service.settings["local_fallback_enabled"] = False
            adapter = TimeServerAdapter(service)
            adapter.chrony_config_dir = os.path.join(tmpdir, "chrony")
            adapter.chrony_config_path = os.path.join(adapter.chrony_config_dir, "vis.conf")
            adapter.ptp_config_dir = os.path.join(tmpdir, "time")
            adapter.ptp_config_path = os.path.join(adapter.ptp_config_dir, "ptp4l.conf")
            adapter.ptp_unit_path = os.path.join(tmpdir, "vis-ptp4l.service")
            adapter.ubuntu_sources_path = os.path.join(tmpdir, "ubuntu-ntp-pools.sources")
            Path(adapter.ubuntu_sources_path).write_text("pool ntp.ubuntu.com iburst\n", encoding="utf-8")

            def active(command, check=False, **kwargs):
                if command[:3] == ["systemctl", "is-active", "--quiet"]:
                    return subprocess.CompletedProcess(command, 0)
                return subprocess.CompletedProcess(command, 0)

            with patch("vis.manager.subprocess.run", side_effect=active) as run:
                result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            with open(adapter.chrony_config_path, "r", encoding="utf-8") as handle:
                rendered = handle.read()
            self.assertIn("server time.google.com iburst", rendered)
            self.assertIn("allow 172.30.0.0/24", rendered)
            self.assertFalse(os.path.exists(adapter.ubuntu_sources_path))
            commands = [call[0][0] for call in run.call_args_list]
            self.assertIn(["systemctl", "disable", "--now", "systemd-timesyncd"], commands)
            self.assertIn(["systemctl", "enable", "--now", "chrony.service"], commands)

    def test_time_server_adapter_writes_ptp_unit_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("time-server")
            service.settings.update(
                {
                    "allowed_clients": ["172.30.0.0/24"],
                    "upstream_sources": ["time.google.com"],
                    "ptp_enabled": True,
                    "ptp_interface": "ens160",
                    "ptp_domain": 7,
                    "ptp_transport": "udp4",
                    "ptp_timestamping": "software",
                }
            )
            adapter = TimeServerAdapter(service)
            adapter.chrony_config_dir = os.path.join(tmpdir, "chrony")
            adapter.chrony_config_path = os.path.join(adapter.chrony_config_dir, "vis.conf")
            adapter.ptp_config_dir = os.path.join(tmpdir, "time")
            adapter.ptp_config_path = os.path.join(adapter.ptp_config_dir, "ptp4l.conf")
            adapter.ptp_unit_path = os.path.join(tmpdir, "vis-ptp4l.service")

            def active(command, check=False, **kwargs):
                if command[:3] == ["systemctl", "is-active", "--quiet"]:
                    return subprocess.CompletedProcess(command, 0)
                return subprocess.CompletedProcess(command, 0)

            with patch("vis.manager.subprocess.run", side_effect=active):
                result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            with open(adapter.ptp_config_path, "r", encoding="utf-8") as handle:
                ptp_config = handle.read()
            with open(adapter.ptp_unit_path, "r", encoding="utf-8") as handle:
                ptp_unit = handle.read()
            self.assertIn("domainNumber 7", ptp_config)
            self.assertIn("time_stamping software", ptp_config)
            self.assertIn("ptp4l -i ens160", ptp_unit)

    def test_dhcp_server_adapter_writes_dnsmasq_config_and_unit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("dhcp-server")
            service.filesystem_root = os.path.join(tmpdir, "dhcp-data")
            service.settings.update(
                {
                    "interface": "ens160",
                    "subnet_cidr": "172.30.0.0/24",
                    "pool_start": "172.30.0.100",
                    "pool_end": "172.30.0.199",
                    "gateway": "172.30.0.1",
                    "dns_servers": ["172.30.0.9", "192.168.30.29"],
                    "domain": "vcf.lab",
                    "default_lease_time": 3600,
                    "max_lease_time": 7200,
                    "authoritative": True,
                    "reservations": [{"mac": "00:50:56:aa:bb:cc", "ip": "172.30.0.60", "hostname": "vcf01"}],
                }
            )
            adapter = DHCPServerAdapter(service)
            adapter.config_dir = os.path.join(tmpdir, "dhcp-config")
            adapter.config_path = os.path.join(adapter.config_dir, "dnsmasq.conf")
            adapter.unit_path = os.path.join(tmpdir, "vis-dhcp.service")

            def active(command, check=False, **kwargs):
                if command[:3] == ["systemctl", "is-active", "--quiet"]:
                    return subprocess.CompletedProcess(command, 0)
                return subprocess.CompletedProcess(command, 0)

            with patch("vis.manager.subprocess.run", side_effect=active) as run:
                result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            with open(adapter.config_path, "r", encoding="utf-8") as handle:
                rendered = handle.read()
            with open(adapter.unit_path, "r", encoding="utf-8") as handle:
                unit = handle.read()
            self.assertIn("port=0", rendered)
            self.assertIn("interface=ens160", rendered)
            self.assertIn("dhcp-range=172.30.0.100,172.30.0.199,3600s", rendered)
            self.assertIn("dhcp-option=option:router,172.30.0.1", rendered)
            self.assertIn("dhcp-option=option:dns-server,172.30.0.9,192.168.30.29", rendered)
            self.assertIn("dhcp-host=00:50:56:aa:bb:cc,172.30.0.60,vcf01", rendered)
            self.assertIn("dnsmasq --keep-in-foreground", unit)
            commands = [call[0][0] for call in run.call_args_list]
            self.assertIn(["systemctl", "enable", "--now", "vis-dhcp.service"], commands)

    def test_kms_service_adapter_writes_pykmip_config_and_unit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("kms-service")
            service.filesystem_root = os.path.join(tmpdir, "kms-data")
            ca_path = os.path.join(tmpdir, "rootCA.pem")
            cert_path = os.path.join(tmpdir, "server.crt")
            key_path = os.path.join(tmpdir, "server.key")
            for path in (ca_path, cert_path, key_path):
                Path(path).write_text("test", encoding="utf-8")
            service.settings.update(
                {
                    "port": 5697,
                    "tls_ca_path": ca_path,
                    "tls_cert_path": cert_path,
                    "tls_key_path": key_path,
                    "database_path": os.path.join(service.filesystem_root, "pykmip.db"),
                }
            )
            adapter = PyKMIPServiceAdapter(service)
            adapter.config_dir = os.path.join(tmpdir, "kms-config")
            adapter.config_path = os.path.join(adapter.config_dir, "server.conf")
            adapter.policy_dir = os.path.join(adapter.config_dir, "policies")
            adapter.unit_path = os.path.join(tmpdir, "vis-kms.service")

            def active(command, check=False, **kwargs):
                if command[:3] == ["systemctl", "is-active", "--quiet"]:
                    return subprocess.CompletedProcess(command, 0)
                return subprocess.CompletedProcess(command, 0)

            with patch("vis.manager.subprocess.run", side_effect=active) as run:
                with patch("vis.manager.socket.create_connection"):
                    result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            with open(adapter.config_path, "r", encoding="utf-8") as handle:
                rendered = handle.read()
            with open(adapter.unit_path, "r", encoding="utf-8") as handle:
                unit = handle.read()
            self.assertIn("port=5697", rendered)
            self.assertIn("certificate_path={}".format(cert_path), rendered)
            self.assertIn("engine=sqlite", rendered)
            self.assertIn("python -m vis.pykmip_compat -f {} --ignore_tls_client_auth".format(adapter.config_path), unit)
            self.assertIn("Environment=PYTHONPATH=/opt/vis/app", unit)
            commands = [call[0][0] for call in run.call_args_list]
            self.assertIn(["systemctl", "enable", "--now", "vis-kms.service"], commands)

    def test_ldap_adapter_writes_openldap_config_ldif_and_unit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("ldap-provider")
            service.filesystem_root = os.path.join(tmpdir, "directory")
            service.settings.update(
                {
                    "base_dn": "dc=williamlam,dc=local",
                    "bind_dn": "cn=admin,dc=williamlam,dc=local",
                    "admin_user": "admin",
                    "admin_password": "DirectoryPassword1!",
                    "users": [
                        {
                            "id": "user-1",
                            "uid": "jdoe",
                            "display_name": "Jane Doe",
                            "email": "jane@example.com",
                            "password": "UserPassword1!",
                            "groups": ["group-1"],
                        }
                    ],
                    "groups": [{"id": "group-1", "name": "vcf-admins", "description": "VCF administrators", "members": ["user-1"]}],
                }
            )
            adapter = LDAPProviderAdapter(service)
            adapter.config_dir = os.path.join(tmpdir, "config")
            adapter.apparmor_local_path = os.path.join(tmpdir, "apparmor", "local", "usr.sbin.slapd")
            adapter.unit_path = os.path.join(tmpdir, "vis-ldap.service")
            os.makedirs(os.path.dirname(adapter.apparmor_local_path))

            def run_command(command, **kwargs):
                result = subprocess.CompletedProcess(command, 0)
                result.stdout = "{SSHA}hashed\n" if command and command[0] == "slappasswd" else ""
                result.stderr = ""
                return result

            with patch.object(adapter, "_service_active", return_value=True), \
                patch.object(adapter, "_ldap_search_ok", return_value=True), \
                patch("vis.manager.subprocess.run", side_effect=run_command) as run:
                result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            slapd_conf = Path(adapter.config_dir, "slapd.conf").read_text(encoding="utf-8")
            bootstrap = Path(adapter.config_dir, "bootstrap.ldif").read_text(encoding="utf-8")
            unit = Path(adapter.unit_path).read_text(encoding="utf-8")
            self.assertIn('suffix "dc=williamlam,dc=local"', slapd_conf)
            self.assertIn('rootdn "cn=admin,dc=williamlam,dc=local"', slapd_conf)
            self.assertIn("rootpw {SSHA}hashed", slapd_conf)
            self.assertIn("moduleload memberof", slapd_conf)
            self.assertIn("overlay memberof", slapd_conf)
            self.assertIn("dn: uid=jdoe,ou=users,dc=williamlam,dc=local", bootstrap)
            self.assertIn("objectClass: extensibleObject", bootstrap)
            self.assertIn("entryUUID:", bootstrap)
            self.assertIn("distinguishedName: uid=jdoe,ou=users,dc=williamlam,dc=local", bootstrap)
            self.assertIn("dn: cn=vcf-admins,ou=groups,dc=williamlam,dc=local", bootstrap)
            self.assertIn("distinguishedName: cn=vcf-admins,ou=groups,dc=williamlam,dc=local", bootstrap)
            self.assertIn("member: uid=jdoe,ou=users,dc=williamlam,dc=local", bootstrap)
            self.assertIn("ExecStart=/usr/sbin/slapd", unit)
            self.assertEqual(0o755, stat.S_IMODE(os.stat(adapter.config_dir).st_mode))
            self.assertEqual(0o644, stat.S_IMODE(os.stat(adapter._slapd_conf_path()).st_mode))
            apparmor = Path(adapter.apparmor_local_path).read_text(encoding="utf-8")
            self.assertIn("{}/ r,".format(adapter.config_dir), apparmor)
            self.assertIn("{}/** rwk,".format(service.filesystem_root), apparmor)
            commands = [call[0][0] for call in run.call_args_list]
            self.assertIn(["slapadd", "-f", adapter._slapd_conf_path(), "-l", adapter._bootstrap_ldif_path()], commands)
            self.assertIn(["apparmor_parser", "-r", "/etc/apparmor.d/usr.sbin.slapd"], commands)
            self.assertTrue(any(command and command[0] == "ldapmodify" for command in commands))
            self.assertIn(["systemctl", "enable", "--now", "vis-ldap.service"], commands)

    def test_ldap_ldaps_stages_shared_tls_for_openldap_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ServiceStore(os.path.join(tmpdir, "vis.db"))
            store.initialize()
            service = store.get_service("ldap-provider")
            tls_dir = Path(tmpdir, "shared-tls")
            tls_dir.mkdir()
            for name in ("rootCA.pem", "server.crt", "server.key"):
                Path(tls_dir, name).write_text(name, encoding="utf-8")
            service.filesystem_root = os.path.join(tmpdir, "directory")
            service.settings.update(
                {
                    "protocol": "ldaps",
                    "port": 636,
                    "base_dn": "dc=williamlam,dc=local",
                    "bind_dn": "cn=admin,dc=williamlam,dc=local",
                    "admin_user": "admin",
                    "admin_password": "DirectoryPassword1!",
                    "tls_ca_path": str(tls_dir / "rootCA.pem"),
                    "tls_cert_path": str(tls_dir / "server.crt"),
                    "tls_key_path": str(tls_dir / "server.key"),
                }
            )
            adapter = LDAPProviderAdapter(service)
            adapter.config_dir = os.path.join(tmpdir, "config")
            adapter.unit_path = os.path.join(tmpdir, "vis-ldap.service")

            def run_command(command, **kwargs):
                result = subprocess.CompletedProcess(command, 0)
                result.stdout = "{SSHA}hashed\n" if command and command[0] == "slappasswd" else ""
                result.stderr = ""
                return result

            with patch.object(adapter, "_service_active", return_value=True), \
                patch.object(adapter, "_ldap_search_ok", return_value=True), \
                patch("vis.manager.subprocess.run", side_effect=run_command) as run:
                result = adapter.enable()

            self.assertEqual("healthy", result.health_status)
            commands = [call[0][0] for call in run.call_args_list]
            ldapmodify = [command for command in commands if command and command[0] == "ldapmodify"]
            self.assertTrue(ldapmodify)
            self.assertIn("ldaps://127.0.0.1:636", ldapmodify[-1])
            tls_paths = adapter._ldap_tls_paths()
            self.assertEqual("server.key", Path(tls_paths["key"]).read_text(encoding="utf-8"))
            self.assertEqual(0o640, Path(tls_paths["key"]).stat().st_mode & 0o777)
            slapd_conf = Path(adapter.config_dir, "slapd.conf").read_text(encoding="utf-8")
            self.assertIn("TLSCertificateKeyFile {}".format(tls_paths["key"]), slapd_conf)


class RepositoryFileManagerTest(unittest.TestCase):
    def test_file_manager_lists_and_creates_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RepositoryFileManager(tmpdir)

            manager.mkdir("", "vcf-backups")
            listing = manager.list_dir("")

            self.assertEqual(["vcf-backups"], [entry["name"] for entry in listing["entries"]])
            self.assertEqual("Directory", listing["entries"][0]["kind"])

    def test_file_manager_hides_system_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.mkdir(os.path.join(tmpdir, "lost+found"))
            os.mkdir(os.path.join(tmpdir, ".vis-upload-tmp"))
            Path(tmpdir, ".uploading.bundle.uuid").write_text("partial", encoding="utf-8")
            manager = RepositoryFileManager(tmpdir)

            listing = manager.list_dir("")

            self.assertEqual([], listing["entries"])

    def test_file_manager_rejects_root_escape_and_root_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RepositoryFileManager(tmpdir)

            with self.assertRaises(ValueError):
                manager.list_dir("../")

            with self.assertRaises(ValueError):
                manager.delete("")

    def test_file_manager_uploads_nested_file_and_requires_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RepositoryFileManager(tmpdir)
            upload = namedtuple("Upload", "stream")

            saved = manager.save_upload("", "folder/example.txt", upload(io.BytesIO(b"first")))
            self.assertEqual("folder/example.txt", saved["path"])
            with open(os.path.join(tmpdir, "folder", "example.txt"), "rb") as handle:
                self.assertEqual(b"first", handle.read())

            with self.assertRaises(FileExistsError):
                manager.save_upload("", "folder/example.txt", upload(io.BytesIO(b"second")))

            manager.save_upload("", "folder/example.txt", upload(io.BytesIO(b"second")), overwrite=True)
            with open(os.path.join(tmpdir, "folder", "example.txt"), "rb") as handle:
                self.assertEqual(b"second", handle.read())

    def test_file_manager_uploads_chunked_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RepositoryFileManager(tmpdir)
            upload_id = "11111111-2222-4333-8444-555555555555"
            temp_root = os.path.join(tmpdir, ".vis-upload-tmp", "chunks")

            first = manager.save_upload_chunk("", "folder/example.bin", upload_id, io.BytesIO(b"abc"), 0, 2, 6, 0, temp_root=temp_root)
            second = manager.save_upload_chunk("", "folder/example.bin", upload_id, io.BytesIO(b"def"), 1, 2, 6, 3, temp_root=temp_root)

            self.assertFalse(first["complete"])
            self.assertTrue(second["complete"])
            self.assertEqual("folder/example.bin", second["path"])
            with open(os.path.join(tmpdir, "folder", "example.bin"), "rb") as handle:
                self.assertEqual(b"abcdef", handle.read())


class WebAppTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "VIS_DB_PATH": os.path.join(self.tmpdir.name, "vis.db"),
                "VIS_DEPOT_CONFIG_DIR": os.path.join(self.tmpdir.name, "depot-config"),
                "VIS_STATE_DIR": os.path.join(self.tmpdir.name, "state"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_services_api_lists_initial_services(self):
        response = self.client.get("/api/services")

        self.assertEqual(200, response.status_code)
        payload = json.loads(response.get_data(as_text=True))
        self.assertEqual(9, len(payload["services"]))
        self.assertEqual(
            {"web-depot", "sftp-backup", "harbor-registry", "ldap-provider", "oidc-provider", "unbound-dns", "time-server", "dhcp-server", "kms-service"},
            {service["id"] for service in payload["services"]},
        )

    def test_home_page_renders_service_summary(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("System Health", body)
        self.assertIn("Service Summary", body)
        self.assertIn("<h2>Services</h2>", body)
        self.assertNotIn("Managed Services", body)
        self.assertIn("Software Depot", body)
        self.assertIn("SFTP Backup", body)
        self.assertIn("Container Registry", body)
        self.assertIn("LDAP Provider", body)
        self.assertIn("OIDC Provider", body)
        self.assertIn("DNS Server", body)
        self.assertIn("NTP Server", body)
        self.assertIn("DHCP Server", body)
        self.assertIn("Key Management Service", body)
        self.assertIn("Ready to configure", body)
        self.assertIn("Enable services after configuration", body)
        self.assertNotIn("Local SFTP adapter enabled", body)
        self.assertNotIn("Adapters", body)
        self.assertNotIn(">Configure</button>", body)
        self.assertNotIn(">Restart</button>", body)
        self.assertIn(">Details</a>", body)
        self.assertIn("Config Export/Import", body)
        self.assertIn("Updates", body)
        self.assertIn("Resources", body)
        self.assertIn("VCF Upgrade Planning Tool", body)
        self.assertIn("https://vmware.github.io/vcf-upgrade-planner/", body)
        self.assertIn("VCF Docs", body)
        self.assertIn("VCF Designs", body)
        self.assertIn("VCF API Reference", body)
        self.assertEqual(9, body.count("Needs Configuration"))

    def test_updates_page_renders_status_and_update_form(self):
        response = self.client.get("/updates")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("<h1>Updates</h1>", body)
        self.assertIn('class="summary-grid service-metrics"', body)
        self.assertIn("Appliance Update", body)
        self.assertIn("Repository URL", body)
        self.assertIn("https://github.com/lamw/vcf-infrastructure-service-appliance.git", body)
        self.assertIn("Run Update", body)
        self.assertIn("Update Log", body)

    def test_updates_run_starts_configured_update_script(self):
        marker = os.path.join(self.tmpdir.name, "update-marker.txt")
        script = os.path.join(self.tmpdir.name, "fake-vis-update.sh")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
            handle.write("echo \"$@\" > '{}'\n".format(marker))
        os.chmod(script, 0o755)
        self.app.config["VIS_UPDATE_SCRIPT"] = script

        response = self.client.post(
            "/updates/run",
            data={"repo_url": "https://github.com/lamw/vcf-infrastructure-service-appliance.git", "branch": "main"},
        )

        self.assertEqual(302, response.status_code)
        for _ in range(20):
            if os.path.exists(marker):
                break
            time.sleep(0.05)
        self.assertTrue(os.path.exists(marker))
        with open(marker, "r", encoding="utf-8") as handle:
            marker_text = handle.read()
        self.assertIn("--repo-url https://github.com/lamw/vcf-infrastructure-service-appliance.git --branch main", marker_text)

    def test_config_profiles_page_exports_service_json(self):
        manager = self.app.config["service_manager"]
        service = manager.get_service("web-depot")
        service.settings["protocol"] = "https"
        service.settings["basic_auth_enabled"] = True
        service.settings["basic_auth_user"] = "vcf"
        service.settings["basic_auth_password"] = "Secret1!"
        manager.store.save_service(service)

        page = self.client.get("/config")
        self.assertEqual(200, page.status_code)
        self.assertIn("Download JSON Profile", page.get_data(as_text=True))
        self.assertIn("The exported JSON includes service credentials", page.get_data(as_text=True))

        response = self.client.get("/config/export")
        payload = json.loads(response.get_data(as_text=True))

        self.assertEqual(200, response.status_code)
        self.assertEqual("application/json", response.mimetype)
        self.assertIn("attachment; filename=vis-config-profile.json", response.headers["Content-Disposition"])
        self.assertEqual("vis.config.profile/v1", payload["schema"])
        self.assertEqual("vis.williamlam.local", payload["appliance"]["fqdn"])
        exported = {item["id"]: item for item in payload["services"]}
        self.assertEqual(9, len(exported))
        self.assertEqual("https", exported["web-depot"]["settings"]["protocol"])
        self.assertEqual("Secret1!", exported["web-depot"]["settings"]["basic_auth_password"])

    def test_config_profile_import_updates_service_settings(self):
        profile = {
            "schema": "vis.config.profile/v1",
            "services": [
                {
                    "id": "web-depot",
                    "enabled": True,
                    "configured": True,
                    "filesystem_root": "/opt/vis/data/depot",
                    "settings": {
                        "protocol": "https",
                        "port": 8443,
                        "basic_auth_enabled": True,
                        "basic_auth_user": "vcf",
                        "basic_auth_password": "Secret1!",
                    },
                }
            ],
        }

        response = self.client.post("/config/import", data={"profile_json": json.dumps(profile)})
        service = self.app.config["service_manager"].get_service("web-depot")

        self.assertEqual(302, response.status_code)
        self.assertIn("config_status=Imported+1+service+configuration", response.headers["Location"])
        self.assertTrue(service.enabled)
        self.assertTrue(service.configured)
        self.assertEqual("https", service.settings["protocol"])
        self.assertEqual("vcf", service.settings["basic_auth_user"])
        self.assertEqual("https://vis.williamlam.local:8443/", service.endpoint)
        self.assertEqual("needs_configuration", service.health_status)
        self.assertFalse(service.last_validation_result.ok)
        self.assertIn("Imported configuration", service.last_validation_result.message)

    def test_config_profile_import_accepts_uploaded_json(self):
        profile = {
            "schema": "vis.config.profile/v1",
            "services": [
                {
                    "id": "sftp-backup",
                    "enabled": False,
                    "configured": True,
                    "settings": {"user": "vis-backup", "password": "VMware1!"},
                }
            ],
        }

        response = self.client.post(
            "/config/import",
            data={"profile_file": (io.BytesIO(json.dumps(profile).encode("utf-8")), "vis-profile.json")},
            content_type="multipart/form-data",
        )
        service = self.app.config["service_manager"].get_service("sftp-backup")

        self.assertEqual(302, response.status_code)
        self.assertEqual("vis-backup", service.settings["user"])
        self.assertEqual("VMware1!", service.settings["password"])
        self.assertEqual("disabled", service.health_status)

    def test_config_profile_import_rejects_invalid_profiles(self):
        unknown = {
            "schema": "vis.config.profile/v1",
            "services": [{"id": "not-real", "settings": {}}],
        }
        response = self.client.post("/config/import", data={"profile_json": json.dumps(unknown)})
        page = self.client.get(response.headers["Location"])
        body = page.get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("Unknown service id in profile: not-real", body)

        invalid = self.client.post("/config/import", data={"profile_json": "{not-json"})
        invalid_page = self.client.get(invalid.headers["Location"])
        self.assertIn("Expecting property name enclosed in double quotes", invalid_page.get_data(as_text=True))

    def test_service_detail_page_renders_placeholder_configuration(self):
        response = self.client.get("/services/ldap-provider")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Configuration", body)
        self.assertIn("LDAP Provider Configuration", body)
        self.assertIn("OpenLDAP Server Configuration", body)
        self.assertIn("LDAP Users", body)
        self.assertIn("LDAP Groups", body)
        self.assertIn("VCF SSO Directory Values", body)
        self.assertIn("Primary Domain Controller", body)
        self.assertIn("User Search Attribute", body)
        self.assertIn("Group Search Attribute", body)
        self.assertIn('id="ldapSsoSearchAttribute" type="text" readonly value="mail"', body)
        self.assertIn('id="ldapSsoGroupSearchAttribute" type="text" readonly value="cn"', body)
        self.assertIn("LDAP Configuration Filters", body)
        self.assertIn("(objectClass=groupOfNames)", body)
        self.assertIn("(objectClass=inetOrgPerson)", body)
        self.assertIn("Attribute Mappings", body)
        self.assertIn('id="ldapSsoMapUserName"', body)
        self.assertIn('id="ldapSsoMapUserName" type="text" readonly value="mail"', body)
        self.assertIn('value="entryUUID"', body)
        self.assertIn("Provisioning Base DNs", body)
        self.assertIn("ou=groups,", body)
        self.assertIn("ou=users,", body)
        self.assertIn("LDAPS using shared VIS certificate", body)
        self.assertNotIn("Adapter Boundary", body)
        self.assertNotIn("Validation</strong>", body)
        self.assertIn("View Logs", body)
        self.assertIn("data-service-action-form", body)
        self.assertIn("data-service-progress-overlay", body)
        self.assertIn("VIS is enabling LDAP Provider", body)
        self.assertIn("service-progress-bar", body)
        self.assertIn("Required before enabling", body)
        self.assertIn("requires-configuration", body)
        self.assertIn("Set the directory base and bind account before enabling LDAP Provider.", body)
        self.assertIn("Save required configuration before enabling this service", body)

        response = self.client.get("/services/oidc-provider")
        body = response.get_data(as_text=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("OIDC Provider", body)
        self.assertIn("OIDC Provider Configuration", body)
        self.assertIn("Keycloak Server Configuration", body)
        self.assertIn("VCF SSO OIDC Clients", body)
        self.assertIn("OIDC Discovery Endpoint", body)
        self.assertIn("OIDC Users", body)
        self.assertIn("OIDC Groups", body)
        self.assertIn("Assigned Groups", body)
        self.assertIn("vcf-admins", body)
        self.assertIn("vcf-users", body)
        self.assertIn("VIS is enabling OIDC Provider", body)
        self.assertIn('data-copy-target="oidcAdminUser"', body)
        self.assertIn('data-copy-target="oidcAdminPassword"', body)
        self.assertIn('id="oidcDiscoveryEndpoint"', body)
        self.assertIn("Create OIDC Client", body)
        self.assertIn('for="oidcNewClientId"', body)
        self.assertIn('placeholder="Enter your Client ID"', body)
        self.assertIn('for="oidcNewRedirectUrl"', body)
        self.assertIn('placeholder="Enter your Redirect URL"', body)
        self.assertIn("copyTextToClipboard", body)
        self.assertIn('document.createElement("textarea")', body)
        self.assertIn("Browser blocked clipboard copy", body)
        self.assertIn("Set the Keycloak administrator password before enabling OIDC Provider.", body)

    def test_required_configuration_highlight_clears_after_service_is_configured(self):
        service_id = "sftp-backup"
        body = self.client.get("/services/{}".format(service_id)).get_data(as_text=True)

        self.assertIn("Required before enabling", body)
        self.assertIn("Set the SFTP username and password before enabling SFTP Backup.", body)
        self.assertIn("requires-configuration", body)
        self.assertIn("Save required configuration before enabling this service", body)

        service = self.app.config["service_manager"].get_service(service_id)
        service.configured = True
        self.app.config["service_manager"].store.save_service(service)
        configured_body = self.client.get("/services/{}".format(service_id)).get_data(as_text=True)

        self.assertNotIn("Required before enabling", configured_body)
        self.assertNotIn("requires-configuration", configured_body)
        self.assertNotIn("Save required configuration before enabling this service", configured_body)

    def test_unconfigured_service_cannot_be_enabled_by_post(self):
        response = self.client.post("/services/sftp-backup/toggle")
        service = self.app.config["service_manager"].get_service("sftp-backup")

        self.assertEqual(302, response.status_code)
        self.assertFalse(service.enabled)
        self.assertIn("config_required=Save+required+configuration+before+enabling+this+service", response.headers["Location"])

    def test_oidc_provider_config_and_default_group_user_are_managed(self):
        response = self.client.post(
            "/services/oidc-provider/config",
            data={
                "realm": "VCF",
                "default_group": "vcf-admins",
                "admin_user": "admin",
                "admin_password": "KeycloakPassword1!",
            },
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")

        self.assertEqual(302, response.status_code)
        self.assertTrue(service.configured)
        self.assertEqual("VCF", service.settings["realm"])
        self.assertEqual("vcf-admins", service.settings["default_group"])
        self.assertEqual("admin", service.settings["admin_user"])
        self.assertEqual("KeycloakPassword1!", service.settings["admin_password"])
        self.assertEqual("http://vis.williamlam.local:9081/", service.endpoint)
        self.assertIn("oidc_config_status=OIDC+Provider+configuration+updated", response.headers["Location"])
        self.assertIn("#oidc-config", response.headers["Location"])

        user_response = self.client.post(
            "/services/oidc-provider/users",
            data={
                "username": "jdoe",
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "jane@example.com",
                "password": "UserPassword1!",
            },
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")

        self.assertEqual(302, user_response.status_code)
        self.assertIn("oidc_user_status", user_response.headers["Location"])
        self.assertIn("#oidc-users", user_response.headers["Location"])
        self.assertEqual("jdoe", service.settings["users"][0]["username"])
        self.assertEqual("vcf-admins", service.settings["groups"][0]["name"])
        self.assertEqual([service.settings["groups"][0]["id"]], service.settings["users"][0]["groups"])

        duplicate = self.client.post(
            "/services/oidc-provider/users",
            data={"username": "jdoe", "password": "UserPassword1!"},
        )
        self.assertEqual(302, duplicate.status_code)
        self.assertIn("oidc_user_error=OIDC+user+jdoe+already+exists", duplicate.headers["Location"])
        self.assertIn("#oidc-users", duplicate.headers["Location"])

    def test_oidc_provider_groups_users_and_membership_are_managed(self):
        self.client.get("/services/oidc-provider")
        create_group = self.client.post(
            "/services/oidc-provider/groups",
            data={"name": "vcf-operators", "description": "Operators"},
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")
        group_id = service.settings["groups"][0]["id"]

        create_user = self.client.post(
            "/services/oidc-provider/users",
            data={
                "username": "asmith",
                "first_name": "Ann",
                "last_name": "Smith",
                "email": "ann@example.com",
                "password": "UserPassword1!",
                "groups": group_id,
            },
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")
        user_id = service.settings["users"][0]["id"]

        self.assertEqual(302, create_group.status_code)
        self.assertIn("oidc_group_status=OIDC+group+created", create_group.headers["Location"])
        self.assertIn("#oidc-groups", create_group.headers["Location"])
        self.assertEqual(302, create_user.status_code)
        self.assertEqual("asmith", service.settings["users"][0]["username"])
        self.assertIn(group_id, service.settings["users"][0]["groups"])
        self.assertIn(user_id, service.settings["groups"][0]["members"])

        update_user = self.client.post(
            "/services/oidc-provider/users/{}".format(user_id),
            data={
                "username": "asmith",
                "first_name": "Anna",
                "last_name": "Smith",
                "email": "anna@example.com",
                "password": "",
                "disabled": "on",
                "groups": group_id,
            },
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")
        self.assertEqual(302, update_user.status_code)
        self.assertIn("#oidc-users", update_user.headers["Location"])
        self.assertEqual("Anna", service.settings["users"][0]["first_name"])
        self.assertTrue(service.settings["users"][0]["disabled"])

        members = self.client.post(
            "/services/oidc-provider/groups/{}/members".format(group_id),
            data={"members": user_id},
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")
        self.assertEqual(302, members.status_code)
        self.assertIn("#oidc-groups", members.headers["Location"])
        self.assertIn(user_id, service.settings["groups"][0]["members"])

        delete_user = self.client.post("/services/oidc-provider/users/{}/delete".format(user_id))
        service = self.app.config["service_manager"].get_service("oidc-provider")
        self.assertEqual(302, delete_user.status_code)
        self.assertEqual([], service.settings["users"])
        self.assertEqual([], service.settings["groups"][0]["members"])

        delete_group = self.client.post("/services/oidc-provider/groups/{}/delete".format(group_id))
        service = self.app.config["service_manager"].get_service("oidc-provider")
        self.assertEqual(302, delete_group.status_code)
        self.assertNotIn(group_id, [group["id"] for group in service.settings["groups"]])

    def test_oidc_provider_config_error_is_scoped_to_config_panel(self):
        response = self.client.post(
            "/services/oidc-provider/config",
            data={"realm": "VCF", "default_group": "vcf-admins", "admin_user": "", "admin_password": ""},
        )
        body = self.client.get("/services/oidc-provider?oidc_config_error=Admin+user+is+required").get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("oidc_config_error=Admin+user+is+required", response.headers["Location"])
        self.assertIn("#oidc-config", response.headers["Location"])
        self.assertIn('id="oidc-config"', body)
        self.assertIn("Admin user is required", body)

    def test_oidc_user_error_is_scoped_to_users_panel(self):
        response = self.client.get("/services/oidc-provider?oidc_user_error=OIDC+user+william+already+exists")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("id=\"oidc-users\"", body)
        self.assertIn("OIDC user error", body)
        self.assertIn("OIDC user william already exists", body)
        self.assertNotIn("<p class=\"notice bad\">OIDC user william already exists</p>", body)

    def test_oidc_provider_clients_are_managed_in_vis(self):
        vcf_redirect_url = "https://vcf-idb01.vcf.lab/federation/t/CUSTOMER/auth/response/oauth2"
        create = self.client.post(
            "/services/oidc-provider/clients",
            data={"client_id": "vcf-sso", "redirect_url": vcf_redirect_url},
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")
        client = service.settings["oidc_clients"][0]

        self.assertEqual(302, create.status_code)
        self.assertIn("oidc_client_status", create.headers["Location"])
        self.assertIn("#oidc-clients", create.headers["Location"])
        self.assertEqual("vcf-sso", client["client_id"])
        self.assertEqual(vcf_redirect_url, client["redirect_url"])
        self.assertTrue(client["client_secret"])

        body = self.client.get("/services/oidc-provider").get_data(as_text=True)
        self.assertIn("vcf-sso", body)
        self.assertIn("id=\"oidc-clients\"", body)
        self.assertIn("oidcClientSecret{}".format(client["id"]), body)
        self.assertIn("http://vis.williamlam.local:9081/realms/VCF/.well-known/openid-configuration", body)

        update = self.client.post(
            "/services/oidc-provider/clients/{}".format(client["id"]),
            data={"client_id": "vcf-vcenter", "redirect_url": "https://vcf.example.com/new-callback"},
        )
        service = self.app.config["service_manager"].get_service("oidc-provider")
        self.assertEqual(302, update.status_code)
        self.assertEqual("vcf-vcenter", service.settings["oidc_clients"][0]["client_id"])
        self.assertEqual("https://vcf.example.com/new-callback", service.settings["oidc_clients"][0]["redirect_url"])

        delete = self.client.post("/services/oidc-provider/clients/{}/delete".format(client["id"]))
        service = self.app.config["service_manager"].get_service("oidc-provider")
        self.assertEqual(302, delete.status_code)
        self.assertEqual([], service.settings["oidc_clients"])

    def test_oidc_provider_client_rejects_duplicate_and_invalid_redirect(self):
        self.client.post(
            "/services/oidc-provider/clients",
            data={"client_id": "vcf-sso", "redirect_url": "https://vcf.example.com/callback"},
        )
        duplicate = self.client.post(
            "/services/oidc-provider/clients",
            data={"client_id": "vcf-sso", "redirect_url": "https://vcf.example.com/other"},
        )
        invalid = self.client.post(
            "/services/oidc-provider/clients",
            data={"client_id": "vcf-other", "redirect_url": "not-a-url"},
        )

        self.assertEqual(302, duplicate.status_code)
        self.assertIn("already+exists", duplicate.headers["Location"])
        self.assertIn("oidc_client_error", duplicate.headers["Location"])
        self.assertIn("#oidc-clients", duplicate.headers["Location"])
        self.assertEqual(302, invalid.status_code)
        self.assertIn("Redirect+URL", invalid.headers["Location"])
        self.assertIn("oidc_client_error", invalid.headers["Location"])

    def test_oidc_provider_client_error_is_scoped_to_client_panel(self):
        response = self.client.get("/services/oidc-provider?oidc_client_error=Redirect+URL+is+invalid")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("OIDC client error", body)
        self.assertIn("Redirect URL is invalid", body)
        self.assertIn("VCF SSO OIDC Clients", body)
        self.assertNotIn("<p class=\"notice bad\">Redirect URL is invalid</p>", body)

    def test_service_detail_normalizes_endpoint_for_current_protocol_and_port(self):
        updates = {
            "web-depot": ("https", 8443, "/", "https://vis.williamlam.local:8443/"),
            "harbor-registry": ("https", 9443, "/", "https://vis.williamlam.local:9443/"),
            "oidc-provider": ("https", 9444, "/", "https://vis.williamlam.local:9444/"),
            "ldap-provider": ("ldaps", 636, "", "ldaps://vis.williamlam.local"),
            "unbound-dns": ("dns", 53, "", "dns://vis.williamlam.local"),
            "time-server": ("ntp", 123, "", "ntp://vis.williamlam.local"),
            "dhcp-server": ("dhcp", 67, "", "dhcp://vis.williamlam.local"),
            "kms-service": ("kmip", 5696, "", "kmip://vis.williamlam.local"),
        }
        manager = self.app.config["service_manager"]
        for service_id, (protocol, port, path, stale_endpoint) in updates.items():
            service = manager.get_service(service_id)
            service.settings["protocol"] = protocol
            service.settings["port"] = port
            service.settings["path"] = path
            service.endpoint = stale_endpoint
            manager.store.save_service(service)

        oidc_response = self.client.get("/services/oidc-provider")
        self.assertEqual(200, oidc_response.status_code)
        self.assertIn('href="https://vis.williamlam.local:9444/"', oidc_response.get_data(as_text=True))
        self.assertEqual("https://vis.williamlam.local:9444/", manager.get_service("oidc-provider").endpoint)

        payload = json.loads(self.client.get("/api/services").get_data(as_text=True))
        endpoints = {service["id"]: service["endpoint"] for service in payload["services"]}
        self.assertEqual("https://vis.williamlam.local:8443/", endpoints["web-depot"])
        self.assertEqual("https://vis.williamlam.local:9443/", endpoints["harbor-registry"])
        self.assertEqual("https://vis.williamlam.local:9444/", endpoints["oidc-provider"])
        self.assertEqual("ldaps://vis.williamlam.local", endpoints["ldap-provider"])
        self.assertEqual("dns://vis.williamlam.local", endpoints["unbound-dns"])
        self.assertEqual("ntp://vis.williamlam.local", endpoints["time-server"])
        self.assertEqual("dhcp://vis.williamlam.local", endpoints["dhcp-server"])
        self.assertEqual("kmip://vis.williamlam.local", endpoints["kms-service"])

    def test_ldap_provider_config_supports_ldaps_with_shared_certificate(self):
        with patch("vis.web._ensure_shared_tls"):
            response = self.client.post(
                "/services/ldap-provider/config",
                data={
                    "protocol": "ldaps",
                    "base_dn": "dc=williamlam,dc=local",
                    "bind_dn": "cn=admin,dc=williamlam,dc=local",
                    "admin_user": "admin",
                    "admin_password": "DirectoryPassword1!",
                },
            )
        service = self.app.config["service_manager"].get_service("ldap-provider")

        self.assertEqual(302, response.status_code)
        self.assertEqual("ldaps", service.settings["protocol"])
        self.assertTrue(service.settings["tls_enabled"])
        self.assertEqual(636, service.settings["port"])
        self.assertEqual("ldaps://vis.williamlam.local", service.endpoint)
        self.assertEqual("/opt/vis/config/tls/server.crt", service.settings["tls_cert_path"])
        self.assertIn("ldap_config_status=LDAP+provider+configuration+updated", response.headers["Location"])
        self.assertIn("#ldap-config", response.headers["Location"])

    def test_ldap_provider_config_error_is_scoped_to_config_panel(self):
        response = self.client.post(
            "/services/ldap-provider/config",
            data={"protocol": "ldap", "base_dn": "not-a-dn", "admin_user": "admin", "admin_password": "DirectoryPassword1!"},
        )
        body = self.client.get("/services/ldap-provider?ldap_config_error=Base+DN+is+invalid").get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("ldap_config_error", response.headers["Location"])
        self.assertIn("#ldap-config", response.headers["Location"])
        self.assertIn('id="ldap-config"', body)
        self.assertIn("Base DN is invalid", body)

        admin_base = self.client.post(
            "/services/ldap-provider/config",
            data={"protocol": "ldap", "base_dn": "cn=admin,dc=vmug,dc=local", "admin_user": "admin", "admin_password": "DirectoryPassword1!"},
        )
        admin_base_body = self.client.get(admin_base.headers["Location"]).get_data(as_text=True)

        self.assertEqual(302, admin_base.status_code)
        self.assertIn("ldap_config_error", admin_base.headers["Location"])
        self.assertIn("Base DN must be a domain root", admin_base_body)
        self.assertIn("Do not include the admin user in the Base DN", admin_base_body)

    def test_service_action_backend_errors_render_on_service_page(self):
        service = self.app.config["service_manager"].get_service("ldap-provider")
        service.configured = True
        self.app.config["service_manager"].store.save_service(service)

        with patch.object(self.app.config["service_manager"], "enable_service", side_effect=OSError("Unable to load VIS LDAP directory")):
            response = self.client.post("/services/ldap-provider/toggle")
        body = self.client.get(response.headers["Location"]).get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("service_error=Unable+to+load+VIS+LDAP+directory", response.headers["Location"])
        self.assertIn("Service action failed", body)
        self.assertIn("Unable to load VIS LDAP directory", body)

    def test_ldap_provider_users_groups_and_membership_are_managed(self):
        self.client.post(
            "/services/ldap-provider/groups",
            data={"name": "vcf-admins", "description": "VCF administrators"},
        )
        service = self.app.config["service_manager"].get_service("ldap-provider")
        group_id = service.settings["groups"][0]["id"]

        create = self.client.post(
            "/services/ldap-provider/users",
            data={
                "uid": "jdoe",
                "display_name": "Jane Doe",
                "email": "jane@example.com",
                "password": "DirectoryPassword1!",
                "groups": group_id,
            },
        )
        service = self.app.config["service_manager"].get_service("ldap-provider")
        user_id = service.settings["users"][0]["id"]

        self.assertEqual(302, create.status_code)
        self.assertIn("ldap_user_status=LDAP+user+created", create.headers["Location"])
        self.assertIn("#ldap-users", create.headers["Location"])
        self.assertEqual("jdoe", service.settings["users"][0]["uid"])
        self.assertIn(group_id, service.settings["users"][0]["groups"])
        self.assertIn(user_id, service.settings["groups"][0]["members"])

        update = self.client.post(
            "/services/ldap-provider/users/{}".format(user_id),
            data={
                "uid": "jdoe",
                "display_name": "Jane Q. Doe",
                "email": "jane.q@example.com",
                "password": "",
                "disabled": "on",
            },
        )
        service = self.app.config["service_manager"].get_service("ldap-provider")
        self.assertEqual(302, update.status_code)
        self.assertIn("#ldap-users", update.headers["Location"])
        self.assertEqual("Jane Q. Doe", service.settings["users"][0]["display_name"])
        self.assertTrue(service.settings["users"][0]["disabled"])

        delete = self.client.post("/services/ldap-provider/users/{}/delete".format(user_id))
        service = self.app.config["service_manager"].get_service("ldap-provider")
        self.assertEqual(302, delete.status_code)
        self.assertIn("#ldap-users", delete.headers["Location"])
        self.assertEqual([], service.settings["users"])
        self.assertEqual([], service.settings["groups"][0]["members"])

    def test_ldap_user_and_group_messages_are_scoped_to_local_panels(self):
        self.client.post("/services/ldap-provider/groups", data={"name": "vcf-admins"})
        duplicate_group = self.client.post("/services/ldap-provider/groups", data={"name": "vcf-admins"})
        invalid_user = self.client.post(
            "/services/ldap-provider/users",
            data={"uid": "", "display_name": "Missing User", "password": "DirectoryPassword1!"},
        )
        user_body = self.client.get("/services/ldap-provider?ldap_user_error=Username+must+start+with+a+letter").get_data(as_text=True)
        group_body = self.client.get("/services/ldap-provider?ldap_group_error=vcf-admins+already+exists.").get_data(as_text=True)

        self.assertEqual(302, invalid_user.status_code)
        self.assertIn("ldap_user_error", invalid_user.headers["Location"])
        self.assertIn("#ldap-users", invalid_user.headers["Location"])
        self.assertEqual(302, duplicate_group.status_code)
        self.assertIn("ldap_group_error", duplicate_group.headers["Location"])
        self.assertIn("#ldap-groups", duplicate_group.headers["Location"])
        self.assertIn('id="ldap-users"', user_body)
        self.assertIn("Username must start with a letter", user_body)
        self.assertIn('id="ldap-groups"', group_body)
        self.assertIn("vcf-admins already exists.", group_body)

    def test_http_endpoint_metric_is_clickable(self):
        response = self.client.get("/services/web-depot")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn('href="http://vis.williamlam.local:8081/"', body)

    def test_sftp_endpoint_metric_is_not_browser_link(self):
        response = self.client.get("/services/sftp-backup")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("sftp://vis.williamlam.local/backup", body)
        self.assertNotIn('href="sftp://vis-backup@vis.williamlam.local/backup"', body)

    def test_harbor_detail_page_renders_configuration(self):
        response = self.client.get("/services/harbor-registry")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Container Registry Client Configuration", body)
        self.assertIn("Expose using shared VIS certificate", body)
        self.assertIn("Harbor Server Configuration", body)
        self.assertIn('href="http://vis.williamlam.local:9080/"', body)
        self.assertIn('data-copy-target="harborAdminUser"', body)
        self.assertIn('data-copy-target="harborAdminPassword"', body)

    def test_harbor_config_error_is_scoped_to_configuration_panel(self):
        response = self.client.post(
            "/services/harbor-registry/config",
            data={"tls_enabled": "on", "admin_user": "", "admin_password": ""},
        )
        body = self.client.get("/services/harbor-registry?config_error=Username+is+required").get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("config_error=Username+is+required", response.headers["Location"])
        self.assertIn("#harbor-config", response.headers["Location"])
        self.assertIn('id="harbor-config"', body)
        self.assertIn("Username is required", body)

    def test_dns_detail_page_renders_entry_manager(self):
        response = self.client.get("/services/unbound-dns")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Add DNS Entry", body)
        self.assertIn("DNS Domain Configuration", body)
        self.assertIn("Configure a DNS domain before adding entries.", body)
        self.assertIn("DNS Entries", body)
        self.assertIn("One row per hostname and address pair", body)
        self.assertIn("Forward and reverse lookup records are saved together", body)
        self.assertIn("Short Hostname", body)
        self.assertIn('placeholder="vcf01"', body)
        self.assertIn('placeholder="172.30.0.60"', body)
        self.assertIn("data-dns-modal", body)
        self.assertIn("data-dns-search", body)
        self.assertIn("Configure DNS domain before adding records", body)
        self.assertIn("data-dns-add disabled", body)

    def test_dns_config_route_sets_required_domain(self):
        response = self.client.post(
            "/services/unbound-dns/config",
            data={"domain": "williamlam.local", "default_ttl": "300"},
        )
        service = self.app.config["service_manager"].get_service("unbound-dns")

        self.assertEqual(302, response.status_code)
        self.assertEqual("williamlam.local", service.settings["domain"])
        self.assertEqual(300, service.settings["default_ttl"])
        self.assertTrue(service.configured)
        self.assertIn("dns_config_status=DNS+configuration+updated", response.headers["Location"])
        self.assertIn("#dns-config", response.headers["Location"])

    def test_dns_config_error_is_scoped_to_dns_config_panel(self):
        response = self.client.post(
            "/services/unbound-dns/config",
            data={"domain": "", "default_ttl": "300"},
        )
        body = self.client.get("/services/unbound-dns?dns_config_error=DNS+domain+is+required").get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("dns_config_error=DNS+domain+is+required", response.headers["Location"])
        self.assertIn("#dns-config", response.headers["Location"])
        self.assertIn('id="dns-config"', body)
        self.assertIn("DNS domain is required", body)

    def test_time_server_detail_page_renders_configuration(self):
        response = self.client.get("/services/time-server")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("NTP Server Configuration", body)
        self.assertIn("VCF Client Configuration", body)
        self.assertIn("Precision Time Protocol", body)
        self.assertIn("Add at least one upstream NTP source", body)
        self.assertNotIn("Listen Address", body)
        self.assertNotIn("PTP Profile", body)
        self.assertIn("Save NTP Configuration", body)
        self.assertIn('data-copy-target="timeClientHost"', body)
        self.assertIn("ntp://vis.williamlam.local", body)

    def test_time_server_config_route_saves_ntp_settings(self):
        response = self.client.post(
            "/services/time-server/config",
            data={
                "mode": "ntp",
                "listen_address": "127.0.0.1",
                "allowed_clients": "172.30.0.0/24\n192.168.30.0/24",
                "upstream_sources": "time.google.com\npool.ntp.org",
                "fallback_stratum": "10",
                "ptp_profile": "telecom",
            },
        )
        service = self.app.config["service_manager"].get_service("time-server")

        self.assertEqual(302, response.status_code)
        self.assertEqual(["172.30.0.0/24", "192.168.30.0/24"], service.settings["allowed_clients"])
        self.assertEqual(["time.google.com", "pool.ntp.org"], service.settings["upstream_sources"])
        self.assertEqual("0.0.0.0", service.settings["listen_address"])
        self.assertEqual("default", service.settings["ptp_profile"])
        self.assertTrue(service.configured)
        self.assertIn("time_config_status=NTP+Server+configuration+updated", response.headers["Location"])
        self.assertIn("#time-config", response.headers["Location"])

    def test_time_server_config_error_is_scoped_to_time_panel(self):
        response = self.client.post(
            "/services/time-server/config",
            data={
                "mode": "ntp",
                "listen_address": "0.0.0.0",
                "allowed_clients": "172.30.0.0/24",
                "upstream_sources": "",
                "fallback_stratum": "10",
            },
        )
        body = self.client.get("/services/time-server?time_config_error=Add+at+least+one+upstream+NTP+source").get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("time_config_error", response.headers["Location"])
        self.assertIn("#time-config", response.headers["Location"])
        self.assertIn('id="time-config"', body)
        self.assertIn("Add at least one upstream NTP source", body)

    def test_dhcp_server_detail_page_renders_configuration(self):
        response = self.client.get("/services/dhcp-server")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("DHCP Server Configuration", body)
        self.assertIn("DHCP Client Configuration", body)
        self.assertIn("Subnet CIDR", body)
        self.assertIn("Pool Start", body)
        self.assertIn("Reservations", body)
        self.assertIn("Save DHCP Configuration", body)
        self.assertNotIn('id="dhcpInterface"', body)

    def test_dhcp_server_config_route_saves_settings(self):
        response = self.client.post(
            "/services/dhcp-server/config",
            data={
                "subnet_cidr": "172.30.0.0/24",
                "pool_start": "172.30.0.100",
                "pool_end": "172.30.0.199",
                "gateway": "172.30.0.1",
                "dns_servers": "172.30.0.9\n192.168.30.29",
                "domain": "vcf.lab",
                "default_lease_time": "3600",
                "max_lease_time": "7200",
                "authoritative": "on",
                "reservations": "00:50:56:aa:bb:cc,172.30.0.60,vcf01",
            },
        )
        service = self.app.config["service_manager"].get_service("dhcp-server")

        self.assertEqual(302, response.status_code)
        self.assertTrue(service.configured)
        self.assertEqual("ens160", service.settings["interface"])
        self.assertEqual("172.30.0.0/24", service.settings["subnet_cidr"])
        self.assertEqual(["172.30.0.9", "192.168.30.29"], service.settings["dns_servers"])
        self.assertEqual([{"mac": "00:50:56:aa:bb:cc", "ip": "172.30.0.60", "hostname": "vcf01"}], service.settings["reservations"])
        self.assertIn("dhcp_config_status=DHCP+Server+configuration+updated", response.headers["Location"])
        self.assertIn("#dhcp-config", response.headers["Location"])

    def test_dhcp_server_config_error_is_scoped_to_dhcp_panel(self):
        response = self.client.post(
            "/services/dhcp-server/config",
            data={
                "subnet_cidr": "172.30.0.0/24",
                "pool_start": "172.30.1.100",
                "pool_end": "172.30.0.199",
                "default_lease_time": "3600",
                "max_lease_time": "7200",
            },
        )
        body = self.client.get("/services/dhcp-server?dhcp_config_error=Pool+Start+must+be+inside+172.30.0.0/24").get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("dhcp_config_error", response.headers["Location"])
        self.assertIn("#dhcp-config", response.headers["Location"])
        self.assertIn('id="dhcp-config"', body)
        self.assertIn("Pool Start must be inside", body)

    def test_kms_service_detail_page_renders_configuration(self):
        response = self.client.get("/services/kms-service")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Key Management Service Configuration", body)
        self.assertIn("KMIP Port", body)
        self.assertIn("Expose using shared VIS certificate", body)
        self.assertIn('id="kmsTlsEnabled"', body)
        self.assertIn("VCF Client Configuration", body)
        self.assertIn("PyKMIP Server Configuration", body)
        self.assertIn("Save Key Management Configuration", body)

    def test_kms_service_config_route_saves_settings(self):
        with patch("vis.web._ensure_shared_tls"), patch("vis.manager.PyKMIPServiceAdapter._validation_problems", return_value=[]):
            response = self.client.post("/services/kms-service/config", data={"port": "5697"})
        service = self.app.config["service_manager"].get_service("kms-service")

        self.assertEqual(302, response.status_code)
        self.assertTrue(service.configured)
        self.assertEqual(5697, service.settings["port"])
        self.assertEqual("pykmip", service.settings["provider"])
        self.assertTrue(service.settings["tls_enabled"])
        self.assertIn("kms_config_status=Key+Management+Service+configuration+updated", response.headers["Location"])
        self.assertIn("#kms-config", response.headers["Location"])

    def test_kms_service_config_error_is_scoped_to_kms_panel(self):
        response = self.client.post("/services/kms-service/config", data={"port": "70000"})
        body = self.client.get("/services/kms-service?kms_config_error=KMIP+port+must+be+between+1+and+65535.").get_data(as_text=True)

        self.assertEqual(302, response.status_code)
        self.assertIn("kms_config_error", response.headers["Location"])
        self.assertIn("#kms-config", response.headers["Location"])
        self.assertIn('id="kms-config"', body)
        self.assertIn("KMIP port must be between", body)

    def test_dns_entry_routes_create_update_delete_paired_entry(self):
        self.client.post(
            "/services/unbound-dns/config",
            data={"domain": "williamlam.local", "default_ttl": "3600"},
        )
        response = self.client.post(
            "/services/unbound-dns/entries",
            data={"name": "vcf01", "address": "172.30.0.60", "ttl": "3600"},
        )
        service = self.app.config["service_manager"].get_service("unbound-dns")
        entries = service.settings["entries"]

        self.assertEqual(302, response.status_code)
        self.assertIn("dns_entry_status=DNS+entry+added", response.headers["Location"])
        self.assertIn("#dns-entries", response.headers["Location"])
        self.assertEqual(1, len(entries))
        self.assertEqual("vcf01.williamlam.local", entries[0]["name"])
        self.assertEqual("172.30.0.60", entries[0]["address"])
        self.assertEqual(3600, entries[0]["ttl"])

        body = self.client.get("/services/unbound-dns").get_data(as_text=True)
        self.assertIn("vcf01.williamlam.local", body)
        self.assertIn("172.30.0.60", body)
        self.assertIn("Enter the short hostname only. VIS appends .williamlam.local", body)
        self.assertIn("local-data: &#34;vcf01.williamlam.local. 3600 IN A 172.30.0.60&#34;", body)
        self.assertIn("local-data-ptr: &#34;172.30.0.60 vcf01.williamlam.local.&#34;", body)

        entry_id = entries[0]["id"]
        update = self.client.post(
            "/services/unbound-dns/entries/{}".format(entry_id),
            data={"name": "vcf01", "address": "172.30.0.61", "ttl": "300"},
        )
        service = self.app.config["service_manager"].get_service("unbound-dns")
        self.assertEqual(302, update.status_code)
        self.assertIn("#dns-entries", update.headers["Location"])
        self.assertEqual("172.30.0.61", service.settings["entries"][0]["address"])
        self.assertEqual(300, service.settings["entries"][0]["ttl"])

        delete = self.client.post("/services/unbound-dns/entries/{}/delete".format(entry_id))
        service = self.app.config["service_manager"].get_service("unbound-dns")
        self.assertEqual(302, delete.status_code)
        self.assertIn("#dns-entries", delete.headers["Location"])
        self.assertEqual([], service.settings["entries"])

    def test_dns_entry_route_requires_domain_first(self):
        response = self.client.post(
            "/services/unbound-dns/entries",
            data={"name": "sddc-manager", "address": "192.168.30.60", "ttl": "3600"},
        )
        service = self.app.config["service_manager"].get_service("unbound-dns")

        self.assertEqual(302, response.status_code)
        self.assertIn("dns_entry_error=Configure+a+DNS+domain+before+adding+DNS+entries", response.headers["Location"])
        self.assertIn("#dns-entries", response.headers["Location"])
        self.assertEqual([], service.settings["entries"])

    def test_dns_entry_route_rejects_duplicate_ip_and_invalid_address(self):
        self.client.post(
            "/services/unbound-dns/config",
            data={"domain": "williamlam.local", "default_ttl": "3600"},
        )
        self.client.post(
            "/services/unbound-dns/entries",
            data={"name": "sddc-manager", "address": "192.168.30.60", "ttl": "3600"},
        )

        duplicate = self.client.post(
            "/services/unbound-dns/entries",
            data={"name": "vcenter", "address": "192.168.30.60", "ttl": "3600"},
        )
        invalid = self.client.post(
            "/services/unbound-dns/entries",
            data={"name": "bad-ip", "address": "not-an-ip", "ttl": "3600"},
        )

        self.assertEqual(302, duplicate.status_code)
        self.assertIn("already+used+by+sddc-manager.williamlam.local", duplicate.headers["Location"])
        self.assertIn("dns_entry_error", duplicate.headers["Location"])
        self.assertIn("#dns-entries", duplicate.headers["Location"])
        self.assertEqual(302, invalid.status_code)
        self.assertIn("not-an-ip+is+not+a+valid+IP+address", invalid.headers["Location"])
        self.assertIn("dns_entry_error", invalid.headers["Location"])
        self.assertIn("#dns-entries", invalid.headers["Location"])

    def test_dns_entry_error_is_scoped_to_dns_entries_panel(self):
        response = self.client.get("/services/unbound-dns?dns_entry_error=Address+is+already+in+use")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn('id="dns-entries"', body)
        self.assertIn("Address is already in use", body)
        self.assertNotIn("<p class=\"notice bad\">Address is already in use</p>\n    <div class=\"detail-grid dns-config-grid\"", body)

    def test_depot_detail_page_renders_configuration_and_repository_files(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "bundle.txt"), "w") as handle:
                handle.write("content")
            service.filesystem_root = tmpdir
            self.app.config["service_manager"].store.save_service(service)

            with patch("vis.web.shutil.which", return_value="/usr/local/bin/vcf-download-tool"):
                response = self.client.get("/services/web-depot")
            body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Software Depot Client Configuration", body)
        self.assertIn("VCF Download Tool", body)
        self.assertIn("Replace VCF Download Tool", body)
        self.assertIn('data-vcfdt-upload-form', body)
        self.assertIn('name="vcfdt_archive"', body)
        self.assertIn("Software Download Configuration", body)
        self.assertIn("Expose using shared VIS certificate", body)
        self.assertIn("Download Mode", body)
        self.assertIn("Manually Uploaded", body)
        self.assertIn("Automatic using Broadcom Activation Code", body)
        self.assertIn("VCFDT System ID", body)
        self.assertIn("Activation Code", body)
        self.assertIn('id="depotDownloadCredential"', body)
        self.assertIn("data-depot-download-form", body)
        self.assertIn("Validating Broadcom Activation Code", body)
        self.assertIn("data-action-toast", body)
        self.assertIn("VCF Software Download", body)
        self.assertIn("Only a single download instance can run at a time", body)
        self.assertNotIn("Binary Download", body)
        self.assertNotIn("Only one download can run at a time", body)
        self.assertIn("Save and validate an Activation Code", body)
        self.assertIn("disabled", body)
        self.assertIn("Repository Files", body)
        self.assertIn("View or manage folders within Software Depot", body)
        self.assertIn("data-confirm-delete", body)
        self.assertIn("upload-percent", body)
        self.assertIn("upload-speed", body)
        self.assertIn("upload-check", body)
        self.assertIn("Drop files or folders into the current location.", body)
        self.assertIn("Created", body)
        self.assertIn("Last Updated", body)
        self.assertIn("bundle.txt", body)
        self.assertIn('data-repository-files', body)
        self.assertIn('data-files-url="/api/services/web-depot/files"', body)
        self.assertIn("data-repo-tree", body)
        self.assertIn("data-repo-entries", body)
        self.assertIn("data-repo-nav", body)
        self.assertIn('data-copy-target="depotAuthUser"', body)
        self.assertIn('data-copy-target="depotAuthPassword"', body)
        self.assertIn('data-copy-target="depotDownloadCredential"', body)

    def test_depot_vcfdt_install_rejects_missing_or_invalid_archive(self):
        missing = self.client.post("/services/web-depot/vcfdt/install", data={})
        invalid = self.client.post(
            "/services/web-depot/vcfdt/install",
            data={"vcfdt_archive": (io.BytesIO(b"not an archive"), "not-vcfdt.tar.gz")},
            content_type="multipart/form-data",
        )

        self.assertEqual(302, missing.status_code)
        self.assertIn("vcfdt_error=Select+a+vcf-download-tool+tarball", missing.headers["Location"])
        self.assertIn("#vcfdt-install", missing.headers["Location"])
        self.assertEqual(302, invalid.status_code)
        self.assertIn("vcfdt_error=Upload+a+Broadcom+VCF+Download+Tool+archive", invalid.headers["Location"])
        self.assertIn("#vcfdt-install", invalid.headers["Location"])

    def test_depot_vcfdt_install_uploads_archive_and_generates_system_id(self):
        bin_dir = os.path.join(self.tmpdir.name, "bin")
        install_root = os.path.join(self.tmpdir.name, "vcf-download-tool")
        profile_path = os.path.join(self.tmpdir.name, "profile.d", "vis-download-tool.sh")
        archive = io.BytesIO()
        script = (
            "#!/bin/sh\n"
            "if [ \"$1\" = \"configuration\" ]; then\n"
            "  echo 'Software depot ID: aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'\n"
            "fi\n"
        ).encode("utf-8")
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            info = tarfile.TarInfo("vcf-download-tool-9.1.0/bin/vcf-download-tool")
            info.size = len(script)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(script))
        archive.seek(0)

        self.app.config["VCF_DOWNLOAD_TOOL_INSTALL_ROOT"] = install_root
        self.app.config["VCF_DOWNLOAD_TOOL_BIN_DIR"] = bin_dir
        self.app.config["VCF_DOWNLOAD_TOOL_PROFILE_PATH"] = profile_path
        with patch.dict(os.environ, {"PATH": "{}:{}".format(bin_dir, os.environ.get("PATH", ""))}):
            response = self.client.post(
                "/services/web-depot/vcfdt/install",
                data={"vcfdt_archive": (archive, "vcf-download-tool-9.1.0.0.25371089.tar.gz")},
                content_type="multipart/form-data",
            )

        service = self.app.config["service_manager"].get_service("web-depot")
        self.assertEqual(302, response.status_code)
        self.assertIn("vcfdt_status=VCF+Download+Tool+installed", response.headers["Location"])
        self.assertIn("#vcfdt-install", response.headers["Location"])
        self.assertTrue(os.path.islink(os.path.join(bin_dir, "vcf-download-tool")))
        self.assertEqual("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", service.settings["vcfdt_system_id"])
        with open(os.path.join(self.app.config["VIS_DEPOT_CONFIG_DIR"], "vcfdt-system-id"), encoding="utf-8") as handle:
            self.assertEqual("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", handle.read().strip())

    def test_depot_download_modes_are_disabled_when_vcfdt_is_missing(self):
        with patch("vis.web.shutil.which", return_value=None):
            response = self.client.get("/services/web-depot")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("VCF Download Tool not installed", body)
        self.assertIn("Drop vcf-download-tool-*.tar.gz here", body)
        self.assertIn("Install VCFDT", body)
        self.assertIn("Manually Uploaded", body)
        self.assertIn('name="download_mode" value="manual" checked', body)
        self.assertIn('name="download_mode" value="activation_code"', body)
        self.assertIn("disabled", body)
        self.assertIn("VCF Download Tool must be installed before automatic downloads are available.", body)

    def test_depot_config_rejects_automatic_mode_when_vcfdt_is_missing(self):
        with patch("vis.web.shutil.which", return_value=None):
            response = self.client.post(
                "/services/web-depot/config",
                data={
                    "form_context": "download_config",
                    "download_mode": "activation_code",
                    "download_credential": "activation-value",
                },
            )

        service = self.app.config["service_manager"].get_service("web-depot")
        self.assertEqual(302, response.status_code)
        self.assertIn("download_config_error=VCF+Download+Tool+is+not+installed", response.headers["Location"])
        self.assertEqual("manual", service.settings["download_mode"])

    def test_depot_config_route_updates_protocol_and_basic_auth(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as depot_root:
            service.filesystem_root = depot_root
            self.app.config["service_manager"].store.save_service(service)

            def metadata_success(command, **kwargs):
                if command[:3] == ["vcf-download-tool", "metadata", "download"]:
                    self.assertNotIn("--ceip=ENABLE", command)
                    self.assertLess(command.index("--depot-store={}".format(depot_root)), command.index("--depot-download-activation-code-file={}".format(os.path.join(self.app.config["VIS_DEPOT_CONFIG_DIR"], "activation-code"))))
                os.makedirs(os.path.join(depot_root, "PROD"))
                result = subprocess.CompletedProcess(command, 0)
                result.stdout = "ok"
                result.stderr = ""
                return result

            with patch("vis.web._ensure_shared_tls"), \
                patch("vis.web.shutil.which", return_value="/usr/local/bin/vcf-download-tool"), \
                patch("vis.web.subprocess.run", side_effect=metadata_success):
                response = self.client.post(
                    "/services/web-depot/config",
                    data={
                        "tls_enabled": "on",
                        "basic_auth_enabled": "on",
                        "auth_user": "depot-user",
                        "auth_password": "DepotPassword1!",
                        "download_mode": "activation_code",
                        "download_credential": "activation-value",
                    },
                )
        service = self.app.config["service_manager"].get_service("web-depot")

        self.assertEqual(302, response.status_code)
        self.assertEqual("https", service.settings["protocol"])
        self.assertTrue(service.settings["tls_enabled"])
        self.assertEqual("shared", service.settings["tls_mode"])
        self.assertEqual(8443, service.settings["port"])
        self.assertTrue(service.settings["basic_auth_enabled"])
        self.assertEqual("depot-user", service.settings["auth_user"])
        self.assertEqual("DepotPassword1!", service.settings["auth_password"])
        self.assertEqual("activation_code", service.settings["download_mode"])
        self.assertEqual("", service.settings["download_token"])
        self.assertEqual("activation-value", service.settings["activation_code"])
        self.assertTrue(service.settings["download_credential_path"].endswith("activation-code"))
        self.assertEqual("https://vis.williamlam.local:8443/", service.endpoint)
        self.assertIn("#download-config", response.headers["Location"])

    def test_depot_config_route_verifies_activation_code_and_stores_system_id(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as depot_root:
            service.filesystem_root = depot_root
            self.app.config["service_manager"].store.save_service(service)

            def command_runner(command, **kwargs):
                result = subprocess.CompletedProcess(command, 0)
                result.stderr = ""
                if command[:3] == ["vcf-download-tool", "configuration", "generate"]:
                    result.stdout = "Software depot ID: 11111111-2222-4333-8444-555555555555"
                else:
                    self.assertNotIn("--ceip=ENABLE", command)
                    self.assertLess(command.index("--depot-store={}".format(depot_root)), command.index("--depot-download-activation-code-file={}".format(os.path.join(self.app.config["VIS_DEPOT_CONFIG_DIR"], "activation-code"))))
                    os.makedirs(os.path.join(depot_root, "PROD"), exist_ok=True)
                    result.stdout = "metadata downloaded"
                return result

            with patch("vis.web.shutil.which", return_value="/usr/local/bin/vcf-download-tool"), \
                patch("vis.web.subprocess.run", side_effect=command_runner):
                response = self.client.post(
                    "/services/web-depot/config",
                    data={"download_mode": "activation_code", "download_credential": "activation-value"},
                )

        service = self.app.config["service_manager"].get_service("web-depot")
        self.assertEqual(302, response.status_code)
        self.assertEqual("activation_code", service.settings["download_mode"])
        self.assertEqual("activation-value", service.settings["activation_code"])
        self.assertEqual("", service.settings["download_token"])
        self.assertEqual("11111111-2222-4333-8444-555555555555", service.settings["vcfdt_system_id"])
        self.assertTrue(service.settings["download_credential_path"].endswith("activation-code"))
        self.assertIn("#download-config", response.headers["Location"])

    def test_depot_config_route_rejects_invalid_download_credential(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as depot_root:
            service.filesystem_root = depot_root
            self.app.config["service_manager"].store.save_service(service)

            result = subprocess.CompletedProcess(["vcf-download-tool"], 1)
            result.stdout = ""
            result.stderr = "invalid token"
            with patch("vis.web.shutil.which", return_value="/usr/local/bin/vcf-download-tool"), \
                patch("vis.web.subprocess.run", return_value=result):
                response = self.client.post(
                    "/services/web-depot/config",
                    data={"form_context": "download_config", "download_mode": "activation_code", "download_credential": "bad-token"},
                )

        service = self.app.config["service_manager"].get_service("web-depot")
        self.assertEqual(302, response.status_code)
        self.assertIn("download_config_error=VCF+Download+Tool+validation+failed", response.headers["Location"])
        self.assertIn("#download-config", response.headers["Location"])
        self.assertEqual("manual", service.settings["download_mode"])

    def test_depot_download_validation_error_is_prominent_near_download_config(self):
        response = self.client.get(
            "/services/web-depot?download_config_error=VCF+Download+Tool+validation+failed:+invalid+activation+code"
        )
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertNotIn("Configuration Error", body)
        self.assertIn("Download validation failed", body)
        self.assertIn("inline-error-notice", body)
        self.assertIn("VCF Download Tool validation failed: invalid activation code", body)

    def test_depot_download_success_is_prominent_and_dismissible(self):
        response = self.client.get(
            "/services/web-depot?download_config_status=Configuration+updated"
        )
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertNotIn("Configuration Saved", body)
        self.assertIn("Download settings validated", body)
        self.assertIn("inline-success-notice", body)
        self.assertIn("data-dismiss-notice", body)
        self.assertIn("Configuration updated", body)

    def test_depot_client_config_status_is_scoped_to_client_panel(self):
        response = self.client.get("/services/web-depot?client_status=Configuration+updated")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn('id="depot-client-config"', body)
        self.assertIn("Configuration saved", body)
        self.assertIn("Configuration updated", body)
        self.assertNotIn("Download settings validated", body)

    def test_depot_client_config_preserves_download_configuration(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        service.settings["download_mode"] = "activation_code"
        service.settings["activation_code"] = "existing-activation-code"
        service.settings["download_credential_path"] = "/opt/vis/config/depot/activation-code"
        self.app.config["service_manager"].store.save_service(service)

        with patch("vis.web._ensure_shared_tls"):
            response = self.client.post(
                "/services/web-depot/config",
                data={
                    "form_context": "client_config",
                    "tls_enabled": "on",
                    "auth_user": "vcf",
                    "auth_password": "secret",
                },
            )
        updated = self.app.config["service_manager"].get_service("web-depot")

        self.assertEqual(302, response.status_code)
        self.assertIn("client_status=Configuration+updated", response.headers["Location"])
        self.assertIn("#depot-client-config", response.headers["Location"])
        self.assertEqual("activation_code", updated.settings["download_mode"])
        self.assertEqual("existing-activation-code", updated.settings["activation_code"])
        self.assertEqual("/opt/vis/config/depot/activation-code", updated.settings["download_credential_path"])

    def test_depot_manual_mode_with_shared_tls_can_be_enabled_without_vcfdt(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as depot_root, tempfile.TemporaryDirectory() as tls_root:
            service.filesystem_root = depot_root
            self.app.config["service_manager"].store.save_service(service)
            tls_dir = Path(tls_root)
            tls_paths = {
                "dir": tls_dir,
                "ca_key": tls_dir / "rootCA.key",
                "ca_pem": tls_dir / "rootCA.pem",
                "server_key": tls_dir / "server.key",
                "server_csr": tls_dir / "server.csr",
                "server_crt": tls_dir / "server.crt",
                "san_conf": tls_dir / "server-san.cnf",
                "full_pem": tls_dir / "vis-full.pem",
            }

            def create_shared_tls(*_args):
                tls_dir.mkdir(parents=True, exist_ok=True)
                for key in ("ca_pem", "server_key", "server_crt", "full_pem"):
                    tls_paths[key].write_text(key, encoding="utf-8")

            def apply_test_shared_tls(adapter):
                adapter.service.settings.update(
                    {
                        "tls_enabled": True,
                        "tls_mode": "shared",
                        "tls_ca_path": str(tls_paths["ca_pem"]),
                        "tls_cert_path": str(tls_paths["server_crt"]),
                        "tls_key_path": str(tls_paths["server_key"]),
                        "tls_full_pem_path": str(tls_paths["full_pem"]),
                    }
                )

            completed = subprocess.CompletedProcess(["systemctl"], 0)
            with patch.dict(os.environ, {"VIS_ENABLE_LOCAL_ADAPTERS": "1"}), \
                patch("vis.web._shared_tls_paths", return_value=tls_paths), \
                patch("vis.web._generate_shared_tls", side_effect=create_shared_tls) as generate_tls, \
                patch("vis.web._vcfdt_available", return_value=False), \
                patch("vis.manager.LocalDepotServiceAdapter._apply_shared_tls_settings", apply_test_shared_tls), \
                patch("vis.manager.LocalDepotServiceAdapter._write_unit"), \
                patch("vis.manager.subprocess.run", return_value=completed):
                response = self.client.post(
                    "/services/web-depot/config",
                    data={
                        "form_context": "client_config",
                        "tls_enabled": "on",
                    },
                )
                configured = self.app.config["service_manager"].get_service("web-depot")
                detail_body = self.client.get("/services/web-depot").get_data(as_text=True)
                toggle_response = self.client.post("/services/web-depot/toggle")

        enabled = self.app.config["service_manager"].get_service("web-depot")
        self.assertEqual(302, response.status_code)
        generate_tls.assert_called_once()
        self.assertTrue(configured.configured)
        self.assertEqual("manual", configured.settings["download_mode"])
        self.assertEqual("", configured.settings["download_credential_path"])
        self.assertNotIn("Save required configuration before enabling this service", detail_body)
        self.assertNotIn("disabled aria-disabled", detail_body)
        self.assertEqual(302, toggle_response.status_code)
        self.assertTrue(enabled.enabled)

    def test_depot_download_cancel_warning_is_prominent_and_dismissible(self):
        response = self.client.get(
            "/services/web-depot?binary_download_warn=Software+depot+download+cancelled"
        )
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertNotIn("Download Cancelled", body)
        self.assertIn("Download cancelled", body)
        self.assertIn("inline-warning-notice", body)
        self.assertIn("data-dismiss-notice", body)
        self.assertIn("Software depot download cancelled", body)

    def test_depot_detail_page_renders_binary_download_controls_after_activation_code_validation(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        credential_path = os.path.join(self.tmpdir.name, "activation-code")
        with open(credential_path, "w") as handle:
            handle.write("activation")
        service.settings["download_mode"] = "activation_code"
        service.settings["download_credential_path"] = credential_path
        self.app.config["service_manager"].store.save_service(service)

        with patch("vis.web.shutil.which", return_value="/usr/local/bin/vcf-download-tool"):
            response = self.client.get("/services/web-depot")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("VCF Software Download", body)
        self.assertIn("Product", body)
        self.assertIn('name="sku"', body)
        self.assertIn('name="vcf_version"', body)
        self.assertIn('value="INSTALL"', body)
        self.assertIn('value="UPGRADE"', body)
        self.assertIn('value="ESX_PATCHES"', body)
        self.assertIn("Download", body)
        self.assertIn("data-binary-download-form", body)
        self.assertNotIn("Cancel Download", body)

    def test_depot_detail_page_renders_cancel_button_for_running_download(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        credential_path = os.path.join(self.tmpdir.name, "activation-code")
        with open(credential_path, "w") as handle:
            handle.write("activation")
        service.settings["download_mode"] = "activation_code"
        service.settings["download_credential_path"] = credential_path
        self.app.config["service_manager"].store.save_service(service)
        state_dir = self.app.config["VIS_STATE_DIR"]
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "depot-download-job.json"), "w") as handle:
            json.dump({"status": "running", "pid": 4242, "message": "running"}, handle)

        with patch("vis.web.shutil.which", return_value="/usr/local/bin/vcf-download-tool"), \
            patch("vis.web._pid_running", return_value=True):
            response = self.client.get("/services/web-depot")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Cancel Download", body)
        self.assertIn('data-binary-download-cancel-form', body)
        self.assertIn('/services/web-depot/download/cancel', body)

    def test_depot_binary_download_route_starts_async_job(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        credential_path = os.path.join(self.tmpdir.name, "activation-code")
        with open(credential_path, "w") as handle:
            handle.write("activation")
        service.settings["download_mode"] = "activation_code"
        service.settings["download_credential_path"] = credential_path
        self.app.config["service_manager"].store.save_service(service)

        fake_process = type("Process", (), {"pid": 4242})()
        with patch("vis.web.subprocess.Popen", return_value=fake_process) as popen:
            response = self.client.post(
                "/services/web-depot/download",
                data={"sku": "VCF", "vcf_version": "9.1.0", "download_type": "INSTALL"},
            )

        self.assertEqual(302, response.status_code)
        self.assertIn("Software+depot+download+started", response.headers["Location"])
        self.assertIn("#binary-download", response.headers["Location"])
        command_payload = json.loads(popen.call_args[0][0][3])
        command = command_payload["commands"][0]
        self.assertNotIn("--ceip=ENABLE", command)
        self.assertLess(command.index("--depot-store=/opt/vis/data/depot"), command.index("--depot-download-activation-code-file={}".format(credential_path)))
        self.assertIn("--depot-download-activation-code-file={}".format(credential_path), command)
        self.assertIn("--depot-store=/opt/vis/data/depot", command)
        self.assertIn("--sku=VCF", command)
        self.assertIn("--vcf-version=9.1.0", command)
        self.assertIn("--type=INSTALL", command)
        self.assertIn("--automated-install", command)

    def test_depot_binary_download_route_supports_esx_patches_with_activation_code(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        credential_path = os.path.join(self.tmpdir.name, "activation-code")
        esx_config_path = os.path.join(self.tmpdir.name, "esxUserConfig.json")
        with open(credential_path, "w") as handle:
            handle.write("activation")
        service.settings["download_mode"] = "activation_code"
        service.settings["download_credential_path"] = credential_path
        self.app.config["service_manager"].store.save_service(service)

        fake_process = type("Process", (), {"pid": 4242})()
        with patch("vis.web.subprocess.Popen", return_value=fake_process) as popen, \
            patch("vis.web._write_esx_user_config", return_value=esx_config_path) as write_esx:
            response = self.client.post(
                "/services/web-depot/download",
                data={"sku": "VCF", "vcf_version": "9.1.0", "download_type": ["INSTALL", "UPGRADE", "ESX_PATCHES"]},
            )

        self.assertEqual(302, response.status_code)
        write_esx.assert_called_once_with("9.1.0")
        command_payload = json.loads(popen.call_args[0][0][3])
        commands = command_payload["commands"]
        self.assertEqual(3, len(commands))
        self.assertIn("--type=INSTALL", commands[0])
        self.assertIn("--type=UPGRADE", commands[1])
        self.assertEqual(
            [
                "vcf-download-tool",
                "esx",
                "download",
                "--depot-store=/opt/vis/data/depot/",
                "--depot-download-activation-code-file={}".format(credential_path),
            ],
            commands[2],
        )
        self.assertEqual(esx_config_path, command_payload["state"]["esx_config_path"])

    def test_depot_binary_download_route_rejects_manual_mode(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        service.settings["download_mode"] = "manual"
        service.settings["download_credential_path"] = ""
        self.app.config["service_manager"].store.save_service(service)

        response = self.client.post(
            "/services/web-depot/download",
            data={"sku": "VCF", "vcf_version": "9.1", "download_type": "ESX_PATCHES"},
        )

        self.assertEqual(302, response.status_code)
        self.assertIn("Configure+and+validate+an+Activation+Code", response.headers["Location"])

    def test_esx_user_config_excludes_all_platforms_except_requested_version(self):
        from vis.web import _write_esx_user_config

        config_path = os.path.join(self.tmpdir.name, "vcf-download-tool", "conf", "esxUserConfig.json")
        result = _write_esx_user_config("9.1.0", path=config_path)

        self.assertEqual(config_path, result)
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertNotIn("embeddedEsx-9.1-INTL", config["disabledPlatforms"])
        self.assertIn("embeddedEsx-9.0-INTL", config["disabledPlatforms"])
        self.assertIn("esxio-9.1-INTL", config["disabledPlatforms"])

    def test_depot_binary_download_route_rejects_invalid_request_and_running_job(self):
        response = self.client.post(
            "/services/web-depot/download",
            data={"sku": "VCF", "vcf_version": "9.1.0", "download_type": "INSTALL"},
        )
        self.assertEqual(302, response.status_code)
        self.assertIn("Configure+and+validate", response.headers["Location"])
        self.assertIn("#binary-download", response.headers["Location"])

        service = self.app.config["service_manager"].get_service("web-depot")
        credential_path = os.path.join(self.tmpdir.name, "activation-code")
        with open(credential_path, "w") as handle:
            handle.write("activation")
        service.settings["download_mode"] = "activation_code"
        service.settings["download_credential_path"] = credential_path
        self.app.config["service_manager"].store.save_service(service)
        state_dir = self.app.config["VIS_STATE_DIR"]
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "depot-download-job.json"), "w") as handle:
            json.dump({"status": "running", "pid": 999999, "message": "running"}, handle)

        with patch("vis.web._pid_running", return_value=True):
            running = self.client.post(
                "/services/web-depot/download",
                data={"sku": "VCF", "vcf_version": "9.1.0", "download_type": "INSTALL"},
            )

        self.assertEqual(302, running.status_code)
        self.assertIn("already+running", running.headers["Location"])
        self.assertIn("binary_download_error", running.headers["Location"])
        self.assertIn("#binary-download", running.headers["Location"])

    def test_depot_binary_download_cancel_route_stops_running_job(self):
        state_dir = self.app.config["VIS_STATE_DIR"]
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "depot-download-job.json")
        with open(state_path, "w") as handle:
            json.dump({"status": "running", "pid": 4242, "message": "running"}, handle)

        with patch("vis.web._pid_running", return_value=True), patch("vis.web.os.killpg") as killpg:
            response = self.client.post("/services/web-depot/download/cancel")

        self.assertEqual(302, response.status_code)
        self.assertIn("Software+depot+download+cancelled", response.headers["Location"])
        self.assertIn("binary_download_warn", response.headers["Location"])
        self.assertIn("#binary-download", response.headers["Location"])
        killpg.assert_called_once()
        self.assertEqual(4242, killpg.call_args[0][0])
        self.assertEqual(signal.SIGTERM, killpg.call_args[0][1])
        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual("cancelled", state["status"])
        self.assertEqual("Software Depot binary download was cancelled.", state["message"])

    def test_depot_binary_download_cancel_route_handles_missing_pid(self):
        state_dir = self.app.config["VIS_STATE_DIR"]
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "depot-download-job.json")
        with open(state_path, "w") as handle:
            json.dump({"status": "running", "pid": None, "message": "Downloading INSTALL binaries."}, handle)

        with patch("vis.web._pid_running", return_value=True), patch("vis.web.os.killpg") as killpg:
            response = self.client.post("/services/web-depot/download/cancel")

        self.assertEqual(302, response.status_code)
        self.assertIn("no+active+process+identifier", response.headers["Location"])
        self.assertIn("binary_download_warn", response.headers["Location"])
        self.assertIn("#binary-download", response.headers["Location"])
        killpg.assert_not_called()
        with open(state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual("cancelled", state["status"])
        self.assertEqual("Software Depot binary download was cancelled.", state["message"])
        self.assertIn("no active process identifier", state["cancel_warning"])

    def test_harbor_config_route_updates_tls_and_credentials(self):
        response = self.client.post(
            "/services/harbor-registry/config",
            data={
                "admin_user": "admin",
                "admin_password": "NewHarborPassword1!",
            },
        )
        service = self.app.config["service_manager"].get_service("harbor-registry")

        self.assertEqual(302, response.status_code)
        self.assertEqual("http", service.settings["protocol"])
        self.assertFalse(service.settings["tls_enabled"])
        self.assertEqual(9080, service.settings["port"])
        self.assertEqual("admin", service.settings["admin_user"])
        self.assertEqual("NewHarborPassword1!", service.settings["admin_password"])
        self.assertEqual("http://vis.williamlam.local:9080/", service.endpoint)
        self.assertTrue(service.configured)
        self.assertEqual("disabled", service.health_status)
        self.assertIn("config_status=Configuration+updated", response.headers["Location"])
        self.assertIn("#harbor-config", response.headers["Location"])

    def test_harbor_config_saves_configuration_before_local_restart(self):
        self.app.config["VIS_LOCAL_ADAPTERS_ENABLED"] = True
        saved_state = {}

        def restart_after_save(service_id):
            service = self.app.config["service_manager"].store.get_service(service_id)
            saved_state["configured"] = service.configured
            saved_state["admin_user"] = service.settings.get("admin_user")
            saved_state["health_status"] = service.health_status
            service.health_status = "disabled"
            self.app.config["service_manager"].store.save_service(service)
            return service

        with patch("vis.web._write_harbor_config"), \
            patch.object(self.app.config["service_manager"], "restart_service", side_effect=restart_after_save):
            response = self.client.post(
                "/services/harbor-registry/config",
                data={
                    "admin_user": "registry",
                    "admin_password": "RegistryPassword1!",
                },
            )

        service = self.app.config["service_manager"].get_service("harbor-registry")
        self.assertEqual(302, response.status_code)
        self.assertTrue(saved_state["configured"])
        self.assertEqual("registry", saved_state["admin_user"])
        self.assertEqual("disabled", saved_state["health_status"])
        self.assertTrue(service.configured)
        self.assertEqual("disabled", service.health_status)

    def test_harbor_config_renderer_rewrites_http_port_when_tls_is_enabled(self):
        service = self.app.config["service_manager"].get_service("harbor-registry")
        service.settings["protocol"] = "https"
        service.settings["tls_enabled"] = True
        service.settings["port"] = 9443
        service.settings["admin_password"] = "Secret123!"
        source = """hostname: reg.mydomain.com
http:
  port: 80
https:
  port: 443
  certificate: /your/certificate/path
  private_key: /your/private/key/path
external_url: https://reg.mydomain.com:8433
harbor_admin_password: Harbor12345
data_volume: /data
"""

        rendered = _render_harbor_config(source, service, "vis.vcf.lab")

        self.assertIn("hostname: vis.vcf.lab", rendered)
        self.assertIn("http:\n  port: 9080", rendered)
        self.assertIn("https:\n  port: 9443", rendered)
        self.assertIn("external_url: https://vis.vcf.lab:9443", rendered)
        self.assertNotIn("port: 80", rendered)

    def test_harbor_health_check_uses_configured_protocol_and_port(self):
        store = ServiceStore(os.path.join(self.tmpdir.name, "harbor-health.db"))
        store.initialize()
        service = store.get_service("harbor-registry")
        service.settings["protocol"] = "http"
        service.settings["port"] = 9080
        service.filesystem_root = self.tmpdir.name
        from vis.manager import LocalHarborServiceAdapter

        adapter = LocalHarborServiceAdapter(service)
        with patch("vis.manager.os.path.isfile", return_value=True), \
            patch.object(adapter, "_service_active", return_value=True), \
            patch("vis.manager.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "Pong"
            self.assertTrue(adapter._harbor_ping_ok())

        command = run.call_args[0][0]
        self.assertIn("http://127.0.0.1:9080/api/v2.0/ping", command)
        self.assertNotIn("-k", command)

    def test_log_targets_use_vis_systemd_units(self):
        from vis.web import _log_targets

        targets = _log_targets(self.app.config["service_manager"])

        self.assertIn("vis-harbor.service", targets["harbor-registry"]["units"])
        self.assertIn("vis-identity.service", targets["oidc-provider"]["units"])
        self.assertIn("vis-ldap.service", targets["ldap-provider"]["units"])
        self.assertIn("/opt/vis/state/depot-download-job.log", targets["web-depot"]["files"])
        self.assertIn("/usr/local/lib/vcf-download-tool/log/vdt.log", targets["web-depot"]["files"])
        self.assertNotIn("harbor.service", targets["harbor-registry"]["units"])
        self.assertNotIn("keycloak.service", targets["oidc-provider"]["units"])

    def test_certificates_page_renders_shared_tls_control_plane(self):
        response = self.client.get("/certificates")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Certificate Authority", body)
        self.assertIn("certificate-metrics", body)
        self.assertIn("Shared VIS Certificate", body)
        self.assertIn("Initialize / Rotate VIS CA", body)
        self.assertIn("Upload Existing CA Certificate", body)
        self.assertIn("Issued Certificates", body)
        self.assertIn("Create / Update Certificate", body)
        self.assertIn("Software Depot", body)
        self.assertIn("Container Registry", body)
        self.assertIn("LDAP Provider", body)
        self.assertIn("OIDC Provider", body)
        self.assertIn("SFTP Backup is intentionally excluded", body)
        self.assertIn("SANs", body)
        self.assertIn("Full PEM", body)
        self.assertIn("Not in use", body)

    def test_certificate_authority_issues_and_deletes_certificate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tls_dir = os.path.join(tmpdir, "tls")
            paths = {
                "dir": Path(tls_dir),
                "ca_key": Path(tls_dir) / "rootCA.key",
                "ca_pem": Path(tls_dir) / "rootCA.pem",
                "server_key": Path(tls_dir) / "server.key",
                "server_csr": Path(tls_dir) / "server.csr",
                "server_crt": Path(tls_dir) / "server.crt",
                "san_conf": Path(tls_dir) / "server-san.cnf",
                "full_pem": Path(tls_dir) / "vis-full.pem",
            }
            with patch("vis.web._shared_tls_paths", return_value=paths):
                self.client.post("/certificates/generate")
                response = self.client.post(
                    "/certificates/issued",
                    data={
                        "name": "vcf-sso",
                        "common_name": "vcf-sso.vcf.lab",
                        "san_dns": "vcf-sso.vcf.lab\nvcf-sso",
                        "san_ips": "172.30.0.60",
                        "days": "365",
                    },
                )
                body = self.client.get("/certificates").get_data(as_text=True)

                self.assertEqual(302, response.status_code)
                self.assertIn("cert_status=Certificate+vcf-sso+issued", response.headers["Location"])
                self.assertTrue((Path(tls_dir) / "issued" / "vcf-sso" / "certificate.pem").is_file())
                self.assertTrue((Path(tls_dir) / "issued" / "vcf-sso" / "private-key.pem").is_file())
                self.assertTrue((Path(tls_dir) / "issued" / "vcf-sso" / "full-chain.pem").is_file())
                self.assertIn("vcf-sso.vcf.lab", body)
                self.assertIn("172.30.0.60", body)

                delete_response = self.client.post("/certificates/issued/vcf-sso/delete")

                self.assertEqual(302, delete_response.status_code)
                self.assertFalse((Path(tls_dir) / "issued" / "vcf-sso").exists())

    def test_certificate_authority_requires_initialized_ca(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tls_dir = os.path.join(tmpdir, "tls")
            paths = {
                "dir": Path(tls_dir),
                "ca_key": Path(tls_dir) / "rootCA.key",
                "ca_pem": Path(tls_dir) / "rootCA.pem",
                "server_key": Path(tls_dir) / "server.key",
                "server_csr": Path(tls_dir) / "server.csr",
                "server_crt": Path(tls_dir) / "server.crt",
                "san_conf": Path(tls_dir) / "server-san.cnf",
                "full_pem": Path(tls_dir) / "vis-full.pem",
            }
            with patch("vis.web._shared_tls_paths", return_value=paths):
                response = self.client.post(
                    "/certificates/issued",
                    data={"name": "vcf-sso", "common_name": "vcf-sso.vcf.lab", "days": "365"},
                )

            self.assertEqual(302, response.status_code)
            self.assertIn("Initialize+the+VIS+Certificate+Authority", response.headers["Location"])

    def test_certificates_upload_validates_and_builds_full_pem(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tls_dir = os.path.join(tmpdir, "tls")
            paths = {
                "dir": Path(tls_dir),
                "ca_key": Path(tls_dir) / "rootCA.key",
                "ca_pem": Path(tls_dir) / "rootCA.pem",
                "server_key": Path(tls_dir) / "server.key",
                "server_csr": Path(tls_dir) / "server.csr",
                "server_crt": Path(tls_dir) / "server.crt",
                "san_conf": Path(tls_dir) / "server-san.cnf",
                "full_pem": Path(tls_dir) / "vis-full.pem",
            }
            with patch("vis.web._shared_tls_paths", return_value=paths), \
                patch("vis.web._validate_certificate_upload") as validate:
                response = self.client.post(
                    "/certificates/upload",
                    data={
                        "cert": (io.BytesIO(b"CERT"), "server.crt"),
                        "key": (io.BytesIO(b"KEY"), "server.key"),
                        "ca": (io.BytesIO(b"CA"), "rootCA.pem"),
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(302, response.status_code)
            validate.assert_called_once()
            with open(paths["full_pem"], "rb") as handle:
                self.assertEqual(b"CERT\nCA", handle.read())

    def test_certificates_upload_reports_validation_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tls_dir = os.path.join(tmpdir, "tls")
            paths = {
                "dir": Path(tls_dir),
                "ca_key": Path(tls_dir) / "rootCA.key",
                "ca_pem": Path(tls_dir) / "rootCA.pem",
                "server_key": Path(tls_dir) / "server.key",
                "server_csr": Path(tls_dir) / "server.csr",
                "server_crt": Path(tls_dir) / "server.crt",
                "san_conf": Path(tls_dir) / "server-san.cnf",
                "full_pem": Path(tls_dir) / "vis-full.pem",
            }
            with patch("vis.web._shared_tls_paths", return_value=paths), \
                patch("vis.web._validate_certificate_upload", side_effect=OSError("Uploaded certificate and private key do not match.")):
                response = self.client.post(
                    "/certificates/upload",
                    data={
                        "cert": (io.BytesIO(b"CERT"), "server.crt"),
                        "key": (io.BytesIO(b"KEY"), "server.key"),
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(302, response.status_code)
            self.assertIn("cert_error=Uploaded+certificate+and+private+key+do+not+match", response.headers["Location"])

    def test_sftp_detail_page_renders_repository_file_manager(self):
        service = self.app.config["service_manager"].get_service("sftp-backup")
        with tempfile.TemporaryDirectory() as tmpdir:
            service.filesystem_root = tmpdir
            self.app.config["service_manager"].store.save_service(service)

            response = self.client.get("/services/sftp-backup")
            body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Repository Files", body)
        self.assertIn("New Folder", body)
        self.assertIn("Refresh", body)
        self.assertIn("SFTP Client Configuration", body)
        self.assertIn("SFTP Server Configuration", body)
        self.assertIn("SFTP Credentials", body)
        self.assertIn("Created", body)
        self.assertIn("Last Updated", body)
        self.assertIn('id="sftpUser"', body)
        self.assertIn('data-copy-target="sftpUser"', body)
        self.assertIn('data-copy-target="sftpPasswordCurrent"', body)
        self.assertIn('data-copy-target="sftpPasswordNew"', body)
        self.assertIn('data-copy-target="sftpPasswordConfirm"', body)
        self.assertNotIn("Drop files or folders into the current location.", body)
        self.assertNotIn("Select Folder", body)

    def test_repository_files_api_lists_entries_for_ajax_navigation(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "VCF910"))
            with open(os.path.join(tmpdir, "VCF910", "bundle.txt"), "w") as handle:
                handle.write("content")
            service.filesystem_root = tmpdir
            self.app.config["service_manager"].store.save_service(service)

            response = self.client.get("/api/services/web-depot/files")
            nested = self.client.get("/api/services/web-depot/files?path=VCF910")

        self.assertEqual(200, response.status_code)
        payload = json.loads(response.get_data(as_text=True))
        self.assertTrue(payload["ok"])
        self.assertEqual("", payload["current"])
        self.assertIsNone(payload["parent"])
        self.assertIn({"name": "VCF910", "kind": "Directory"}, [{"name": entry["name"], "kind": entry["kind"]} for entry in payload["entries"]])

        self.assertEqual(200, nested.status_code)
        nested_payload = json.loads(nested.get_data(as_text=True))
        self.assertTrue(nested_payload["ok"])
        self.assertEqual("VCF910", nested_payload["current"])
        self.assertEqual("", nested_payload["parent"])
        self.assertIn("bundle.txt", [entry["name"] for entry in nested_payload["entries"]])

    def test_repository_file_actions_anchor_to_repository_panel(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as tmpdir:
            service.filesystem_root = tmpdir
            self.app.config["service_manager"].store.save_service(service)

            created = self.client.post(
                "/services/web-depot/files/mkdir",
                data={"path": "", "name": "VCF910"},
            )
            duplicate = self.client.post(
                "/services/web-depot/files/mkdir",
                data={"path": "", "name": "VCF910"},
            )

        self.assertEqual(302, created.status_code)
        self.assertIn("#repository-files", created.headers["Location"])
        self.assertEqual(302, duplicate.status_code)
        self.assertIn("file_error", duplicate.headers["Location"])
        self.assertIn("#repository-files", duplicate.headers["Location"])

    def test_repository_upload_route_saves_file_and_reports_conflict(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as tmpdir:
            service.filesystem_root = tmpdir
            self.app.config["service_manager"].store.save_service(service)

            response = self.client.post(
                "/services/web-depot/files/upload",
                data={
                    "path": "",
                    "relative_path": "nested/example.txt",
                    "file": (io.BytesIO(b"payload"), "example.txt"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(200, response.status_code)
            with open(os.path.join(tmpdir, "nested", "example.txt"), "rb") as handle:
                self.assertEqual(b"payload", handle.read())

            conflict = self.client.post(
                "/services/web-depot/files/upload",
                data={
                    "path": "",
                    "relative_path": "nested/example.txt",
                    "file": (io.BytesIO(b"new payload"), "example.txt"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(409, conflict.status_code)

            overwrite = self.client.post(
                "/services/web-depot/files/upload",
                data={
                    "path": "",
                    "relative_path": "nested/example.txt",
                    "overwrite": "true",
                    "file": (io.BytesIO(b"new payload"), "example.txt"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(200, overwrite.status_code)
            with open(os.path.join(tmpdir, "nested", "example.txt"), "rb") as handle:
                self.assertEqual(b"new payload", handle.read())

    def test_repository_chunk_upload_route_saves_file_and_reports_conflict(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as tmpdir:
            service.filesystem_root = tmpdir
            self.app.config["service_manager"].store.save_service(service)

            common = {
                "path": "",
                "relative_path": "nested/example.txt",
                "upload_id": "11111111-2222-4333-8444-555555555555",
                "total_chunks": "2",
                "total_size": "11",
            }
            first = self.client.post(
                "/services/web-depot/files/upload-chunk",
                data=dict(common, chunk_index="0", offset="0", chunk=(io.BytesIO(b"hello "), "example.txt")),
                content_type="multipart/form-data",
            )
            second = self.client.post(
                "/services/web-depot/files/upload-chunk",
                data=dict(common, chunk_index="1", offset="6", chunk=(io.BytesIO(b"world"), "example.txt")),
                content_type="multipart/form-data",
            )

            self.assertEqual(200, first.status_code)
            self.assertFalse(json.loads(first.get_data(as_text=True))["complete"])
            self.assertEqual(200, second.status_code)
            with open(os.path.join(tmpdir, "nested", "example.txt"), "rb") as handle:
                self.assertEqual(b"hello world", handle.read())

            conflict = self.client.post(
                "/services/web-depot/files/upload-chunk",
                data=dict(common, upload_id="22222222-2222-4333-8444-555555555555", chunk_index="0", offset="0", chunk=(io.BytesIO(b"x"), "example.txt")),
                content_type="multipart/form-data",
            )
            self.assertEqual(409, conflict.status_code)

    def test_repository_upload_route_reports_staging_space_error(self):
        service = self.app.config["service_manager"].get_service("web-depot")
        with tempfile.TemporaryDirectory() as tmpdir:
            service.filesystem_root = tmpdir
            self.app.config["service_manager"].store.save_service(service)
            with patch("werkzeug.wrappers.request.Request.files", new_callable=PropertyMock) as files:
                files.side_effect = OSError(28, "No space left on device")

                response = self.client.post(
                    "/services/web-depot/files/upload",
                    data=b"",
                    content_type="multipart/form-data; boundary=vis",
                )
                payload = json.loads(response.get_data(as_text=True))

        self.assertEqual(507, response.status_code)
        self.assertEqual("insufficient_storage", payload["error"])
        self.assertIn("staging space", payload["message"])

    def test_sftp_upload_route_is_not_available(self):
        response = self.client.post(
            "/services/sftp-backup/files/upload",
            data={
                "path": "",
                "relative_path": "example.txt",
                "file": (io.BytesIO(b"payload"), "example.txt"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(404, response.status_code)

    def test_logs_page_renders_service_targets(self):
        with patch("vis.web.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "May 18 00:00:00 vis sshd[1]: Accepted password for vis-backup\nMay 18 00:01:00 vis sshd[1]: Failed password for bad-user\n"
            run.return_value.stderr = ""

            response = self.client.get("/logs?service=sftp-backup&lines=100&level=error&q=bad-user")
            body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Service Logs", body)
        self.assertIn("SFTP Backup", body)
        self.assertIn("ssh.service", body)
        self.assertIn("Failed password for bad-user", body)
        self.assertNotIn("Accepted password for vis-backup", body)
        self.assertIn("Download", body)
        run.assert_called_once()
        self.assertIn("ssh.service", run.call_args[0][0])

    def test_logs_page_includes_software_depot_file_logs(self):
        def fake_read_log_file(path, lines):
            if path == "/opt/vis/state/depot-download-job.log":
                return "Software Depot binary download failed\nERROR upgrade token rejected"
            return "VCF Download Tool detail log"

        with patch("vis.web.subprocess.run") as run, patch("vis.web._read_log_file", side_effect=fake_read_log_file):
            run.return_value.returncode = 0
            run.return_value.stdout = "Started VIS depot service\n"
            run.return_value.stderr = ""

            response = self.client.get("/logs?service=web-depot&lines=100&level=error")
            body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Software Depot", body)
        self.assertIn("/opt/vis/state/depot-download-job.log", body)
        self.assertIn("/usr/local/lib/vcf-download-tool/log/vdt.log", body)
        self.assertIn("ERROR upgrade token rejected", body)
        self.assertNotIn("Started VIS depot service", body)

    def test_logs_download_returns_filtered_text_attachment(self):
        with patch("vis.web.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "Started VIS\nERROR failed to start DNS\n"
            run.return_value.stderr = ""

            response = self.client.get("/logs/download?service=unbound-dns&lines=100&level=error")

        self.assertEqual(200, response.status_code)
        self.assertEqual("text/plain; charset=utf-8", response.content_type)
        self.assertIn("attachment; filename=vis-unbound-dns-logs.txt", response.headers["Content-Disposition"])
        self.assertEqual("ERROR failed to start DNS\n", response.get_data(as_text=True))

    def test_system_health_page_renders_option_b_dashboard(self):
        response = self.client.get("/system-health")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Appliance Health", body)
        self.assertIn("CPU", body)
        self.assertIn("Memory", body)
        self.assertIn("Storage Partition", body)
        self.assertTrue(body.index("Disk 1") < body.index("Disk 2") < body.index("Disk 3") < body.index("Disk 4") < body.index("Disk 5") < body.index("Disk 6"))
        self.assertIn("200 GiB VMDK", body)
        self.assertIn("60 GiB VMDK", body)
        self.assertIn("2 GiB VMDK", body)
        self.assertIn("Expand Filesystem", body)
        self.assertIn("Software Depot", body)
        self.assertIn("Identity Providers", body)
        self.assertIn("DNS Server", body)

    def test_storage_health_detects_backing_disk_capacity(self):
        usage = namedtuple("usage", "total used free")(1024, 256, 768)

        def run(command, stdout=None, stderr=None, text=None, check=None):
            if command[:4] == ["findmnt", "-no", "SOURCE", "-T"]:
                return subprocess.CompletedProcess(command, 0, stdout="/dev/sdf1\n", stderr="")
            if command == ["lsblk", "-no", "TYPE", "/dev/sdf1"]:
                return subprocess.CompletedProcess(command, 0, stdout="part\n", stderr="")
            if command == ["lsblk", "-no", "PKNAME", "/dev/sdf1"]:
                return subprocess.CompletedProcess(command, 0, stdout="sdf\n", stderr="")
            if command == ["lsblk", "-b", "-dn", "-o", "SIZE", "/dev/sdf"]:
                return subprocess.CompletedProcess(command, 0, stdout="2147483648\n", stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="unexpected command")

        with patch("vis.web.shutil.disk_usage", return_value=usage), patch("vis.web.subprocess.run", side_effect=run):
            partition = _storage_health("identity-providers", "Disk 6", "Identity Providers", "/opt/vis/data/identity", 5, "#5ad1c8")

        self.assertEqual("2 GiB VMDK", partition["vmdk_capacity"])
        self.assertEqual("detected", partition["vmdk_capacity_source"])

    def test_system_health_expand_message_is_dismissible_and_clears_url(self):
        response = self.client.get("/system-health?expand_status=ok&expand_message=expanded+depot")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Filesystem Expansion Complete", body)
        self.assertIn("expanded depot", body)
        self.assertIn("dismissible-notice", body)
        self.assertIn("data-dismiss-notice", body)
        self.assertIn('data-clear-query-params="expand_status,expand_message"', body)
        self.assertIn("url.searchParams.delete(param)", body)

    def test_storage_expand_route_reports_result(self):
        with patch("vis.web._expand_filesystem", return_value={"ok": True, "message": "expanded depot"}):
            response = self.client.post("/system-health/storage/web-depot/expand")

        self.assertEqual(302, response.status_code)
        self.assertIn("expand_status=ok", response.headers["Location"])
        self.assertIn("expanded+depot", response.headers["Location"])

    def test_sftp_password_route_updates_stored_password(self):
        response = self.client.post(
            "/services/sftp-backup/password",
            data={"username": "backup-user", "password": "NewPassword1!", "confirm_password": "NewPassword1!"},
        )
        service = self.app.config["service_manager"].get_service("sftp-backup")

        self.assertEqual(302, response.status_code)
        self.assertEqual("backup-user", service.settings["user"])
        self.assertEqual("NewPassword1!", service.settings["password"])
        self.assertEqual("sftp://backup-user@vis.williamlam.local/backup", service.endpoint)
        self.assertIn("password_status=SFTP+credentials+updated", response.headers["Location"])
        self.assertIn("#sftp-credentials", response.headers["Location"])

    def test_sftp_credentials_route_can_update_username_label_without_password_change(self):
        service = self.app.config["service_manager"].get_service("sftp-backup")
        service.settings["user"] = "backup-user"
        service.settings["password"] = "StoredPassword1!"
        self.app.config["service_manager"].store.save_service(service)

        response = self.client.post(
            "/services/sftp-backup/password",
            data={"username": "backup-user", "password": "", "confirm_password": ""},
        )
        service = self.app.config["service_manager"].get_service("sftp-backup")

        self.assertEqual(302, response.status_code)
        self.assertEqual("backup-user", service.settings["user"])
        self.assertEqual("StoredPassword1!", service.settings["password"])

    def test_sftp_password_route_rejects_mismatch(self):
        response = self.client.post(
            "/services/sftp-backup/password",
            data={"username": "backup-user", "password": "one", "confirm_password": "two"},
        )

        self.assertEqual(302, response.status_code)
        self.assertIn("password_error=Passwords+do+not+match", response.headers["Location"])
        self.assertIn("#sftp-credentials", response.headers["Location"])

    def test_sftp_password_error_is_scoped_to_credentials_panel(self):
        response = self.client.get("/services/sftp-backup?password_error=Passwords+do+not+match")
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn('id="sftp-credentials"', body)
        self.assertIn("Passwords do not match", body)


class WebAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "VIS_AUTH_REQUIRED": True,
                "VIS_ADMIN_USERNAME": "visadmin",
                "VIS_ADMIN_PASSWORD": "InitialPassword1!",
                "VIS_DB_PATH": os.path.join(self.tmpdir.name, "vis.db"),
                "SECRET_KEY": "test-secret",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmpdir.cleanup()

    def login(self):
        return self.client.post(
            "/login",
            data={"username": "visadmin", "password": "InitialPassword1!"},
        )

    def test_auth_redirects_to_login_and_accepts_initial_admin(self):
        response = self.client.get("/")
        self.assertEqual(302, response.status_code)
        self.assertIn("/login", response.headers["Location"])

        login_page = self.client.get("/login")
        login_body = login_page.get_data(as_text=True)
        self.assertIn("Created by", login_body)
        self.assertIn('href="https://williamlam.com"', login_body)
        self.assertIn("William Lam", login_body)
        self.assertIn('id="loginParticles"', login_body)
        self.assertIn("vis-login-particles.js", login_body)
        self.assertIn("vis-ui-effects.js", login_body)
        self.assertIn("favicon.ico", login_body)
        self.assertIn('aria-label="Toggle VIS display mode"', login_body)

        login = self.login()
        self.assertEqual(302, login.status_code)
        home = self.client.get("/")
        home_body = home.get_data(as_text=True)
        self.assertEqual(200, home.status_code)
        self.assertIn("Service Summary", home_body)
        self.assertIn("visadmin@vis.williamlam.local", home_body)
        self.assertIn("vis-ui-effects.js", home_body)
        self.assertIn("favicon.ico", home_body)
        self.assertIn('aria-label="Toggle VIS display mode"', home_body)
        self.assertIn('const themeStorageKey = "vis-theme"', home_body)
        self.assertIn("window.localStorage.setItem(themeStorageKey, nextTheme)", home_body)

    def test_users_page_creates_user_and_resets_password(self):
        self.login()
        create = self.client.post(
            "/users",
            data={"username": "ops-admin", "password": "OpsPassword1!", "confirm_password": "OpsPassword1!"},
        )
        self.assertEqual(302, create.status_code)

        store = self.app.config["service_manager"].store
        user = store.get_user_by_username("ops-admin")
        self.assertIsNotNone(user)
        users_page = self.client.get("/users").get_data(as_text=True)
        self.assertIn("VIS Users", users_page)
        self.assertNotIn("<th>Role</th>", users_page)

        reset = self.client.post(
            "/users/{}/password".format(user["id"]),
            data={"password": "NewOpsPassword1!", "confirm_password": "NewOpsPassword1!"},
        )
        self.assertEqual(302, reset.status_code)

        self.client.post("/logout")
        login = self.client.post("/login", data={"username": "ops-admin", "password": "NewOpsPassword1!"})
        self.assertEqual(302, login.status_code)


if __name__ == "__main__":
    unittest.main()
