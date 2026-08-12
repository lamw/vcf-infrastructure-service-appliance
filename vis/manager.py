import ipaddress
import json
import os
import pwd
import re
import shutil
import socket
import ssl
import stat
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from urllib import error, parse, request

from vis.content_library.dataclasses import ContentLibrarySyncStats
from vis.content_library.sync import _SYNC_STATS_FILE

from .models import ServiceDefinition, ValidationResult, utc_now
from .store import ServiceStore


class ServiceAdapter(ABC):
    def __init__(self, service: ServiceDefinition):
        self.service = service

    @abstractmethod
    def validate(self) -> ValidationResult:
        pass

    @abstractmethod
    def enable(self) -> ServiceDefinition:
        pass

    @abstractmethod
    def disable(self) -> ServiceDefinition:
        pass

    @abstractmethod
    def restart(self) -> ServiceDefinition:
        pass

    @abstractmethod
    def health_check(self) -> ServiceDefinition:
        pass

    @abstractmethod
    def render_config(self) -> str:
        pass


class MockServiceAdapter(ServiceAdapter):
    def validate(self) -> ValidationResult:
        if self.service.configured:
            return ValidationResult(True, "Mock validation passed", utc_now())
        return ValidationResult(False, "Mock adapter reports missing required configuration", utc_now())

    def enable(self) -> ServiceDefinition:
        self.service.enabled = True
        return self.service

    def disable(self) -> ServiceDefinition:
        self.service.enabled = False
        return self.service

    def restart(self) -> ServiceDefinition:
        self.service.last_health_check_time = utc_now()
        return self.service

    def health_check(self) -> ServiceDefinition:
        self.service.last_health_check_time = utc_now()
        if not self.service.configured:
            self.service.health_status = "needs_configuration"
        elif self.service.enabled:
            self.service.health_status = "healthy"
        else:
            self.service.health_status = "disabled"
        return self.service

    def render_config(self) -> str:
        lines = ["# Mock VIS service configuration", f"service_id = {self.service.id}"]
        for key in sorted(self.service.settings):
            lines.append(f"{key} = {self.service.settings[key]}")
        return "\n".join(lines) + "\n"


class LocalSFTPServiceAdapter(ServiceAdapter):
    def validate(self) -> ValidationResult:
        problems = []
        username = str(self.service.settings.get("user", ""))
        if not username:
            return ValidationResult(False, "SFTP credentials are not configured", utc_now())
        user_info = self._user_info(username)
        if not user_info:
            problems.append(f"missing user {username}")
        chroot = "/opt/vis/data/sftp"
        if not self._chroot_permissions_ok(chroot):
            problems.append(f"{chroot} must be owned by root and not writable by group/other")
        if not os.path.isdir(self.service.filesystem_root):
            problems.append(f"missing {self.service.filesystem_root}")
        elif user_info and not self._repository_writable_by_user(self.service.filesystem_root, user_info):
            problems.append(f"{self.service.filesystem_root} is not writable by {username}")
        if not self._ssh_active():
            problems.append("ssh service is inactive")
        if not self._sshd_config_valid():
            problems.append("sshd configuration is invalid")
        if not self._vis_sftp_configured(username, chroot):
            problems.append(f"missing sshd Match User configuration for {username}")
        if problems:
            return ValidationResult(False, "; ".join(problems), utc_now())
        return ValidationResult(
            True, "SFTP backend verified: user, chroot, writable /backup, sshd config, and ssh service", utc_now()
        )

    def enable(self) -> ServiceDefinition:
        self.service.enabled = True
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        self.service.enabled = False
        self.service.health_status = "disabled"
        return self.service

    def restart(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "reload", "ssh"], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        self.service.enabled = self._ssh_active()
        self.service.health_status = "healthy" if validation.ok else "needs_configuration"
        return self.service

    def render_config(self) -> str:
        return "\n".join(
            [
                "# VIS SFTP backup repository",
                "user = {}".format(self.service.settings.get("user")),
                "chroot = /opt/vis/data/sftp",
                f"directory = {self.service.filesystem_root}",
                "port = {}".format(self.service.settings.get("port")),
                "force_command = internal-sftp -d /backup",
                "",
            ]
        )

    def _user_exists(self, username: str) -> bool:
        return self._user_info(username) is not None

    def _user_info(self, username: str):
        try:
            return pwd.getpwnam(username)
        except KeyError:
            return None

    def _ssh_active(self) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "ssh"], check=False)
        return result.returncode == 0

    def _sshd_config_valid(self) -> bool:
        result = subprocess.run(["sshd", "-t"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        return result.returncode == 0

    def _vis_sftp_configured(self, username: str, chroot: str) -> bool:
        result = subprocess.run(
            ["sshd", "-T", "-C", f"user={username},host=vis,addr=127.0.0.1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        expected = {
            f"chrootdirectory {chroot}",
            "forcecommand internal-sftp -d /backup",
        }
        rendered = set(line.strip() for line in result.stdout.splitlines())
        return expected.issubset(rendered)

    def _chroot_permissions_ok(self, path: str) -> bool:
        try:
            path_stat = os.stat(path)
        except FileNotFoundError:
            return False
        writable_by_non_root = path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        return path_stat.st_uid == 0 and path_stat.st_gid == 0 and not writable_by_non_root

    def _repository_writable_by_user(self, path: str, user_info) -> bool:
        try:
            path_stat = os.stat(path)
        except FileNotFoundError:
            return False
        mode = path_stat.st_mode
        if path_stat.st_uid == user_info.pw_uid and mode & stat.S_IWUSR:
            return True
        groups = [user_info.pw_gid]
        if path_stat.st_gid in groups and mode & stat.S_IWGRP:
            return True
        return bool(mode & stat.S_IWOTH)


class LocalDepotServiceAdapter(ServiceAdapter):
    def validate(self) -> ValidationResult:
        missing = []
        if not os.path.isdir(self.service.filesystem_root):
            missing.append(self.service.filesystem_root)
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
            for key in ("tls_cert_path", "tls_key_path"):
                if not os.path.isfile(str(self.service.settings.get(key, ""))):
                    missing.append(str(self.service.settings.get(key, key)))
        if missing:
            return ValidationResult(False, "Missing: {}".format(", ".join(missing)), utc_now())
        return ValidationResult(True, "Software Depot configuration is valid", utc_now())

    def enable(self) -> ServiceDefinition:
        self.service.enabled = True
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", "vis-depot.service"], check=False)
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", "vis-depot.service"], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        if self.service.enabled:
            subprocess.run(["systemctl", "restart", "vis-depot.service"], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        active = self._service_active()
        if not self.service.enabled:
            self.service.health_status = "disabled"
        elif validation.ok and active:
            self.service.health_status = "healthy"
        elif validation.ok:
            self.service.health_status = "stopped"
        else:
            self.service.health_status = "needs_configuration"
        return self.service

    def render_config(self) -> str:
        lines = [
            "# VIS Software Depot",
            f"protocol = {self._protocol()}",
            f"port = {self._port()}",
            f"root = {self.service.filesystem_root}",
            f"basic_auth_enabled = {self._basic_auth_enabled()}",
            "download_mode = {}".format(self.service.settings.get("download_mode", "manual")),
        ]
        if self.service.settings.get("download_credential_path"):
            lines.append("download_credential_path = {}".format(self.service.settings.get("download_credential_path")))
        if self.service.settings.get("vcfdt_system_id"):
            lines.append("vcfdt_system_id = {}".format(self.service.settings.get("vcfdt_system_id")))
        if self._basic_auth_enabled():
            lines.append("auth_user = {}".format(self.service.settings.get("auth_user", "")))
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
            lines.extend(
                [
                    "tls_mode = {}".format(self.service.settings.get("tls_mode", "shared")),
                    "tls_cert = {}".format(self.service.settings.get("tls_cert_path", "")),
                    "tls_key = {}".format(self.service.settings.get("tls_key_path", "")),
                    "tls_full_pem = {}".format(self.service.settings.get("tls_full_pem_path", "")),
                ]
            )
        return "\n".join(lines) + "\n"

    def _write_unit(self) -> None:
        os.makedirs("/opt/vis/config/depot", exist_ok=True)
        os.makedirs(self.service.filesystem_root, exist_ok=True)
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
        auth_user = str(self.service.settings.get("auth_user", "")) if self._basic_auth_enabled() else ""
        auth_password = str(self.service.settings.get("auth_password", "")) if self._basic_auth_enabled() else ""
        unit = """[Unit]
Description=VIS Software Depot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/vis/app
Environment=PYTHONPATH=/opt/vis/app
Environment=VIS_DEPOT_ROOT={root}
Environment=VIS_DEPOT_PROTOCOL={protocol}
Environment=VIS_DEPOT_PORT={port}
Environment=VIS_DEPOT_AUTH_USER={auth_user}
Environment=VIS_DEPOT_AUTH_PASSWORD={auth_password}
Environment=VIS_DEPOT_TLS_CERT={cert}
Environment=VIS_DEPOT_TLS_KEY={key}
ExecStart=/opt/vis/app/venv/bin/python -m vis.depot_server
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
""".format(
            root=self.service.filesystem_root,
            protocol=self._protocol(),
            port=self._port(),
            auth_user=auth_user,
            auth_password=auth_password,
            cert=self.service.settings.get("tls_cert_path", ""),
            key=self.service.settings.get("tls_key_path", ""),
        )
        with open("/etc/systemd/system/vis-depot.service", "w") as handle:
            handle.write(unit)

    def _service_active(self) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "vis-depot.service"], check=False)
        return result.returncode == 0

    def _protocol(self) -> str:
        return str(self.service.settings.get("protocol", "http")).lower()

    def _port(self) -> int:
        return int(self.service.settings.get("port", 8443 if self._protocol() == "https" else 8081))

    def _basic_auth_enabled(self) -> bool:
        return bool(self.service.settings.get("basic_auth_enabled", False))

    def _apply_shared_tls_settings(self) -> None:
        self.service.settings.update(
            {
                "tls_enabled": True,
                "tls_mode": "shared",
                "tls_ca_path": "/opt/vis/config/tls/rootCA.pem",
                "tls_cert_path": "/opt/vis/config/tls/server.crt",
                "tls_key_path": "/opt/vis/config/tls/server.key",
                "tls_full_pem_path": "/opt/vis/config/tls/vis-full.pem",
            }
        )


class LocalHarborServiceAdapter(ServiceAdapter):
    compose_file = "/opt/vis/harbor/docker-compose.yml"

    def validate(self) -> ValidationResult:
        problems = []
        if not os.path.isfile(self.compose_file):
            problems.append(f"missing {self.compose_file}")
        if not os.path.isdir(self.service.filesystem_root):
            problems.append(f"missing {self.service.filesystem_root}")
        if self._protocol() == "https":
            for key in ("tls_cert_path", "tls_key_path"):
                if not os.path.isfile(str(self.service.settings.get(key, ""))):
                    problems.append(str(self.service.settings.get(key, key)))
        if problems:
            return ValidationResult(False, "Missing: {}".format(", ".join(problems)), utc_now())
        return ValidationResult(True, "Harbor configuration is valid", utc_now())

    def enable(self) -> ServiceDefinition:
        self.service.enabled = True
        subprocess.run(["systemctl", "enable", "--now", "vis-harbor.service"], check=False)
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", "vis-harbor.service"], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        if self.service.enabled:
            subprocess.run(["systemctl", "restart", "vis-harbor.service"], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        active = self._service_active()
        if not self.service.enabled:
            self.service.health_status = "disabled"
        elif validation.ok and active and self._harbor_ping_ok():
            self.service.health_status = "healthy"
        elif validation.ok and active:
            self.service.health_status = "starting"
        else:
            self.service.health_status = "needs_configuration"
        return self.service

    def render_config(self) -> str:
        return "\n".join(
            [
                "# VIS Container Registry",
                "engine = Harbor",
                f"compose_file = {self.compose_file}",
                f"external_url = {self.service.endpoint}",
                f"data_volume = {self.service.filesystem_root}",
                f"protocol = {self._protocol()}",
                "port = {}".format(self.service.settings.get("port", 9443)),
                "admin_user = {}".format(self.service.settings.get("admin_user", "")),
                "tls_mode = {}".format(self.service.settings.get("tls_mode", "shared")),
                "tls_cert = {}".format(self.service.settings.get("tls_cert_path", "")),
                "tls_key = {}".format(self.service.settings.get("tls_key_path", "")),
                "",
            ]
        )

    def _service_active(self) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "vis-harbor.service"], check=False)
        if result.returncode == 0:
            return True
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=harbor-core", "--filter", "status=running", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return bool(result.stdout.strip())

    def _harbor_ping_ok(self) -> bool:
        protocol = self._protocol()
        port = str(self.service.settings.get("port", 9443 if protocol == "https" else 9080))
        command = ["curl", "-fsS", "--connect-timeout", "5", f"{protocol}://127.0.0.1:{port}/api/v2.0/ping"]
        if protocol == "https":
            command.insert(1, "-k")
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result.returncode == 0 and "Pong" in result.stdout

    def _protocol(self) -> str:
        return str(self.service.settings.get("protocol", "https")).lower()


class DNSServiceAdapter(ServiceAdapter):
    config_path = "/etc/unbound/unbound.conf.d/vis.conf"
    check_config_path = "/etc/unbound/unbound.conf"
    root_trust_anchor_path = "/etc/unbound/unbound.conf.d/root-auto-trust-anchor-file.conf"
    disabled_root_trust_anchor_path = "/etc/unbound/unbound.conf.d/root-auto-trust-anchor-file.conf.disabled"
    service_name = "unbound.service"
    resolved_dropin_dir = "/etc/systemd/resolved.conf.d"
    resolved_dropin_path = "/etc/systemd/resolved.conf.d/vis-dns.conf"
    resolv_conf_path = "/etc/resolv.conf"
    systemd_resolved_conf_path = "/run/systemd/resolve/resolv.conf"

    def validate(self) -> ValidationResult:
        domain = str(self.service.settings.get("domain", "")).strip(".")
        if not domain:
            return ValidationResult(False, "Configure a DNS domain before enabling DNS Server", utc_now())
        entries = self._entries()
        upstream_servers = self._forward_upstream_servers()
        problems = []
        if bool(self.service.settings.get("forward_upstream_enabled", False)):
            if not upstream_servers:
                problems.append("Add at least one upstream DNS server or disable Forward Upstream DNS")
            for server in upstream_servers:
                try:
                    ipaddress.ip_address(server)
                except ValueError:
                    problems.append(f"{server} is not a valid upstream DNS server IP address")
        names = set()
        addresses = set()
        for entry in entries:
            name = str(entry.get("name", "")).lower()
            address = str(entry.get("address", ""))
            if not name:
                problems.append("missing hostname")
            elif (
                not name.rstrip(".").lower().endswith(f".{domain.lower()}")
                and name.rstrip(".").lower() != domain.lower()
            ):
                problems.append("{} is outside {}".format(entry.get("name"), domain))
            elif name in names:
                problems.append("{} is listed more than once".format(entry.get("name")))
            names.add(name)
            try:
                ipaddress.ip_address(address)
            except ValueError:
                problems.append("{} is not a valid IP address".format(address or "blank address"))
            if address in addresses:
                problems.append(f"{address} is listed more than once")
            addresses.add(address)
        if problems:
            return ValidationResult(False, "; ".join(problems), utc_now())
        if not entries:
            return ValidationResult(True, "DNS Server domain is configured", utc_now())
        return ValidationResult(True, f"{len(entries)} paired DNS entries are valid", utc_now())

    def enable(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if not validation.ok:
            self.service.enabled = False
            self.service.health_status = "needs_configuration"
            self.service.last_health_check_time = utc_now()
            return self.service
        self.service.enabled = True
        self._write_config()
        self._apply_dnssec_setting()
        self._prepare_systemd_resolved()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", self.service_name], check=False)
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", self.service_name], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled" if self.validate().ok else "needs_configuration"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if validation.ok:
            self._write_config()
            self._apply_dnssec_setting()
            self._prepare_systemd_resolved()
            if self.service.enabled:
                subprocess.run(["systemctl", "restart", self.service_name], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        if not self.service.enabled:
            self.service.health_status = "disabled" if validation.ok else "needs_configuration"
        elif not validation.ok:
            self.service.health_status = "needs_configuration"
        elif not self._config_valid():
            self.service.health_status = "needs_configuration"
            self.service.last_validation_result = ValidationResult(
                False, "Unbound configuration check failed", utc_now()
            )
        elif self._service_active():
            self.service.health_status = "healthy"
        else:
            self.service.health_status = "stopped"
        return self.service

    def render_config(self) -> str:
        lines = [
            "# VIS DNS Server",
            "# Paired forward and reverse lookup records rendered for Unbound",
            "server:",
            "  interface: 0.0.0.0",
            "  port: {}".format(self.service.settings.get("port", 53)),
            "  access-control: 0.0.0.0/0 allow",
        ]
        if bool(self.service.settings.get("disable_dnssec", False)):
            lines.extend(
                [
                    "  # DNSSEC validation disabled by VIS",
                    f"  # {self.root_trust_anchor_path} is disabled",
                ]
            )
        else:
            lines.append("  # DNSSEC validation uses the system root trust anchor")
        domain = str(self.service.settings.get("domain", "")).strip(".")
        if domain:
            lines.append(f'  local-zone: "{domain}." static')
        else:
            lines.extend(["", "# Configure DNS domain before adding records."])
            return "\n".join(lines) + "\n"
        entries = self._entries()
        if bool(self.service.settings.get("forward_upstream_enabled", False)) and self._forward_upstream_servers():
            lines.extend(["", "forward-zone:", '  name: "."'])
            for server in self._forward_upstream_servers():
                lines.append(f"  forward-addr: {server}")
            lines.append("  forward-no-cache: yes")
        if not entries:
            lines.extend(["", "# Add DNS entries to render local-data records."])
            return "\n".join(lines) + "\n"
        lines.append("")
        for entry in entries:
            name = str(entry.get("name", "")).rstrip(".")
            address = str(entry.get("address", ""))
            ttl = int(entry.get("ttl", self.service.settings.get("default_ttl", 3600)))
            lines.append(f'local-data: "{name}. {ttl} IN A {address}"')
            lines.append(f'local-data-ptr: "{address} {name}."')
        return "\n".join(lines) + "\n"

    def _forward_upstream_servers(self) -> list[str]:
        servers = self.service.settings.get("forward_upstream_servers", [])
        if isinstance(servers, str):
            return [line.strip() for line in servers.splitlines() if line.strip()]
        if isinstance(servers, list):
            return [str(server).strip() for server in servers if str(server).strip()]
        return []

    def _entries(self) -> list[dict[str, object]]:
        entries = self.service.settings.get("entries", [])
        return entries if isinstance(entries, list) else []

    def _write_config(self) -> None:
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(self.render_config())

    def _apply_dnssec_setting(self) -> None:
        if bool(self.service.settings.get("disable_dnssec", False)):
            if os.path.exists(self.root_trust_anchor_path):
                os.replace(self.root_trust_anchor_path, self.disabled_root_trust_anchor_path)
            return
        if os.path.exists(self.disabled_root_trust_anchor_path) and not os.path.exists(self.root_trust_anchor_path):
            os.replace(self.disabled_root_trust_anchor_path, self.root_trust_anchor_path)

    def _prepare_systemd_resolved(self) -> None:
        os.makedirs(self.resolved_dropin_dir, exist_ok=True)
        with open(self.resolved_dropin_path, "w", encoding="utf-8") as handle:
            handle.write("[Resolve]\nDNSStubListener=no\n")
        if os.path.exists(self.systemd_resolved_conf_path):
            try:
                if os.path.lexists(self.resolv_conf_path):
                    os.remove(self.resolv_conf_path)
                os.symlink(self.systemd_resolved_conf_path, self.resolv_conf_path)
            except OSError:
                pass
        subprocess.run(["systemctl", "restart", "systemd-resolved"], check=False)

    def _config_valid(self) -> bool:
        if not os.path.isfile(self.config_path):
            return False
        result = subprocess.run(
            ["unbound-checkconf", self.check_config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result.returncode == 0

    def _service_active(self) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", self.service_name], check=False)
        return result.returncode == 0


class TimeServerAdapter(ServiceAdapter):
    chrony_config_dir = "/etc/chrony/conf.d"
    chrony_config_path = "/etc/chrony/conf.d/vis.conf"
    ubuntu_sources_path = "/etc/chrony/sources.d/ubuntu-ntp-pools.sources"
    chrony_service_name = "chrony.service"
    ptp_config_dir = "/opt/vis/config/time"
    ptp_config_path = "/opt/vis/config/time/ptp4l.conf"
    ptp_unit_path = "/etc/systemd/system/vis-ptp4l.service"
    ptp_service_name = "vis-ptp4l.service"

    def validate(self) -> ValidationResult:
        problems = []
        listen_address = str(self.service.settings.get("listen_address", "0.0.0.0")).strip()
        try:
            ipaddress.ip_address(listen_address)
        except ValueError:
            problems.append("{} is not a valid listen address".format(listen_address or "blank address"))
        for network in self._allowed_clients():
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError:
                problems.append(f"{network} is not a valid allowed client network")
        upstream_sources = self._upstream_sources()
        local_fallback_enabled = bool(self.service.settings.get("local_fallback_enabled", False))
        if not upstream_sources and not local_fallback_enabled:
            problems.append("Add at least one upstream NTP source or enable local clock fallback")
        try:
            fallback_stratum = int(self.service.settings.get("fallback_stratum", 10))
            if fallback_stratum < 1 or fallback_stratum > 15:
                problems.append("Fallback stratum must be between 1 and 15")
        except (TypeError, ValueError):
            problems.append("Fallback stratum must be a number")
        if bool(self.service.settings.get("ptp_enabled", False)):
            interface = str(self.service.settings.get("ptp_interface", "")).strip()
            if not interface:
                problems.append("Select a PTP interface")
            try:
                domain = int(self.service.settings.get("ptp_domain", 0))
                if domain < 0 or domain > 127:
                    problems.append("PTP domain must be between 0 and 127")
            except (TypeError, ValueError):
                problems.append("PTP domain must be a number")
        if problems:
            return ValidationResult(False, "; ".join(problems), utc_now())
        return ValidationResult(True, "NTP Server configuration is valid", utc_now())

    def enable(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if not validation.ok:
            self.service.enabled = False
            self.service.health_status = "needs_configuration"
            self.service.last_health_check_time = utc_now()
            return self.service
        self.service.enabled = True
        self._write_config()
        self._write_ptp_unit()
        subprocess.run(["systemctl", "disable", "--now", "systemd-timesyncd"], check=False)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", self.chrony_service_name], check=False)
        if bool(self.service.settings.get("ptp_enabled", False)):
            subprocess.run(["systemctl", "enable", "--now", self.ptp_service_name], check=False)
        else:
            subprocess.run(["systemctl", "disable", "--now", self.ptp_service_name], check=False)
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", self.ptp_service_name], check=False)
        subprocess.run(["systemctl", "disable", "--now", self.chrony_service_name], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled" if self.validate().ok else "needs_configuration"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if validation.ok:
            self._write_config()
            self._write_ptp_unit()
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            if self.service.enabled:
                subprocess.run(["systemctl", "restart", self.chrony_service_name], check=False)
                if bool(self.service.settings.get("ptp_enabled", False)):
                    subprocess.run(["systemctl", "restart", self.ptp_service_name], check=False)
                else:
                    subprocess.run(["systemctl", "disable", "--now", self.ptp_service_name], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        if not self.service.enabled:
            self.service.health_status = "disabled" if validation.ok else "needs_configuration"
        elif not validation.ok:
            self.service.health_status = "needs_configuration"
        elif (
            not self._service_active(self.chrony_service_name)
            or bool(self.service.settings.get("ptp_enabled", False))
            and not self._service_active(self.ptp_service_name)
        ):
            self.service.health_status = "stopped"
        else:
            self.service.health_status = "healthy"
        return self.service

    def render_config(self) -> str:
        lines = [
            "# VIS NTP Server",
            "engine = chrony",
            "listen = {}".format(self.service.settings.get("listen_address", "0.0.0.0")),
            "port = {}".format(self.service.settings.get("port", 123)),
        ]
        for network in self._allowed_clients():
            lines.append(f"allow = {network}")
        for source in self._upstream_sources():
            lines.append(f"server = {source} iburst")
        if bool(self.service.settings.get("local_fallback_enabled", False)):
            lines.append("local_fallback = true")
            lines.append("fallback_stratum = {}".format(self.service.settings.get("fallback_stratum", 10)))
        else:
            lines.append("local_fallback = false")
        lines.extend(
            [
                "ptp_enabled = {}".format(str(bool(self.service.settings.get("ptp_enabled", False))).lower()),
                "ptp_interface = {}".format(self.service.settings.get("ptp_interface", "")),
                "ptp_domain = {}".format(self.service.settings.get("ptp_domain", 0)),
                "",
            ]
        )
        return "\n".join(lines)

    def _write_config(self) -> None:
        os.makedirs(self.chrony_config_dir, exist_ok=True)
        if os.path.exists(self.ubuntu_sources_path):
            os.remove(self.ubuntu_sources_path)
        lines = [
            "# Managed by VIS NTP Server",
            "bindaddress {}".format(self.service.settings.get("listen_address", "0.0.0.0")),
            "port {}".format(int(self.service.settings.get("port", 123))),
        ]
        for source in self._upstream_sources():
            lines.append(f"server {source} iburst")
        for network in self._allowed_clients():
            lines.append(f"allow {network}")
        if bool(self.service.settings.get("local_fallback_enabled", False)):
            lines.append("local stratum {}".format(int(self.service.settings.get("fallback_stratum", 10))))
        lines.extend(["makestep 1.0 3", "rtcsync", ""])
        with open(self.chrony_config_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    def _write_ptp_unit(self) -> None:
        interface = str(self.service.settings.get("ptp_interface", "")).strip()
        if not interface:
            return
        transport = str(self.service.settings.get("ptp_transport", "udp4"))
        network_transport = "UDPv4" if transport == "udp4" else "L2"
        domain = int(self.service.settings.get("ptp_domain", 0))
        timestamping = str(self.service.settings.get("ptp_timestamping", "auto"))
        config_lines = [
            "[global]",
            f"domainNumber {domain}",
            f"network_transport {network_transport}",
        ]
        if timestamping in ("software", "hardware"):
            config_lines.append(f"time_stamping {timestamping}")
        config_lines.append("")
        os.makedirs(self.ptp_config_dir, exist_ok=True)
        with open(self.ptp_config_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(config_lines))
        unit = "\n".join(
            [
                "[Unit]",
                "Description=VIS PTP service",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"ExecStart=/usr/sbin/ptp4l -i {interface} -m -f {self.ptp_config_path}",
                "Restart=on-failure",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )
        with open(self.ptp_unit_path, "w", encoding="utf-8") as handle:
            handle.write(unit)

    def _allowed_clients(self) -> list[str]:
        return self._setting_lines("allowed_clients")

    def _upstream_sources(self) -> list[str]:
        return self._setting_lines("upstream_sources")

    def _setting_lines(self, key: str) -> list[str]:
        value = self.service.settings.get(key, [])
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _service_active(self, service_name: str) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", service_name], check=False)
        return result.returncode == 0


class DHCPServerAdapter(ServiceAdapter):
    config_dir = "/opt/vis/config/dhcp"
    config_path = "/opt/vis/config/dhcp/dnsmasq.conf"
    unit_path = "/etc/systemd/system/vis-dhcp.service"
    service_name = "vis-dhcp.service"

    def validate(self) -> ValidationResult:
        problems = self._validation_problems()
        if problems:
            return ValidationResult(False, "; ".join(problems), utc_now())
        return ValidationResult(True, "DHCP Server configuration is valid", utc_now())

    def enable(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        if not validation.ok:
            raise OSError(validation.message)
        self.service.configured = True
        self.service.enabled = True
        self.service.health_status = "starting"
        self._write_config()
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", self.service_name], check=False)
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", self.service_name], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        if not validation.ok:
            self.service.health_status = "needs_configuration"
            return self.service
        self._write_config()
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        if self.service.enabled:
            subprocess.run(["systemctl", "restart", self.service_name], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        active = self._service_active()
        if not self.service.enabled:
            self.service.health_status = "disabled" if validation.ok else "needs_configuration"
        elif validation.ok and active:
            self.service.health_status = "healthy"
        elif validation.ok:
            self.service.health_status = "stopped"
        else:
            self.service.health_status = "needs_configuration"
        return self.service

    def render_config(self) -> str:
        lines = [
            "# VIS DHCP Server",
            "engine = dnsmasq",
            "dns = disabled",
            "interface = {}".format(self.service.settings.get("interface", "")),
            "subnet = {}".format(self.service.settings.get("subnet_cidr", "")),
            "pool = {} - {}".format(
                self.service.settings.get("pool_start", ""), self.service.settings.get("pool_end", "")
            ),
            "gateway = {}".format(self.service.settings.get("gateway", "")),
            "dns_servers = {}".format(", ".join(self.service.settings.get("dns_servers", []))),
            "domain = {}".format(self.service.settings.get("domain", "")),
            "default_lease_time = {}".format(self.service.settings.get("default_lease_time", 3600)),
            "max_lease_time = {}".format(self.service.settings.get("max_lease_time", 7200)),
            "authoritative = {}".format(bool(self.service.settings.get("authoritative", False))),
        ]
        reservations = self.service.settings.get("reservations", [])
        if reservations:
            lines.append("")
            lines.append("# Reservations")
            for reservation in reservations:
                lines.append(
                    "{} {} {}".format(
                        reservation.get("mac", ""),
                        reservation.get("ip", ""),
                        reservation.get("hostname", ""),
                    ).strip()
                )
        return "\n".join(lines) + "\n"

    def _write_config(self):
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.service.filesystem_root, exist_ok=True)
        settings = self.service.settings
        lines = [
            "# Managed by VIS DHCP Server",
            "port=0",
            "interface={}".format(settings.get("interface", "")),
            "bind-interfaces",
            "log-dhcp",
            "dhcp-leasefile={}".format(os.path.join(self.service.filesystem_root, "dnsmasq.leases")),
            "dhcp-range={},{},{}s".format(
                settings.get("pool_start", ""),
                settings.get("pool_end", ""),
                int(settings.get("default_lease_time", 3600)),
            ),
        ]
        if bool(settings.get("authoritative", False)):
            lines.append("dhcp-authoritative")
        gateway = str(settings.get("gateway", "")).strip()
        if gateway:
            lines.append(f"dhcp-option=option:router,{gateway}")
        dns_servers = settings.get("dns_servers", [])
        if dns_servers:
            lines.append("dhcp-option=option:dns-server,{}".format(",".join(dns_servers)))
        domain = str(settings.get("domain", "")).strip()
        if domain:
            lines.append(f"dhcp-option=option:domain-name,{domain}")
        max_lease_time = int(settings.get("max_lease_time", 7200))
        lines.append(f"dhcp-option=option:lease-time,{max_lease_time}")
        for reservation in settings.get("reservations", []):
            hostname = str(reservation.get("hostname", "")).strip()
            suffix = f",{hostname}" if hostname else ""
            lines.append("dhcp-host={},{}{}".format(reservation.get("mac", ""), reservation.get("ip", ""), suffix))
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def _write_unit(self):
        unit = f"""[Unit]
Description=VIS DHCP Server
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file={self.config_path}
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
        with open(self.unit_path, "w", encoding="utf-8") as handle:
            handle.write(unit)

    def _service_active(self):
        try:
            result = subprocess.run(["systemctl", "is-active", "--quiet", self.service_name], check=False)
        except OSError:
            return False
        return result.returncode == 0

    def _validation_problems(self):
        settings = self.service.settings
        problems = []
        interface = str(settings.get("interface", "")).strip()
        if not interface:
            problems.append("Network interface is required")
        elif not re.match(r"^[A-Za-z0-9_.:-]+$", interface):
            problems.append(f"{interface} is not a valid interface name")
        try:
            subnet = ipaddress.ip_network(str(settings.get("subnet_cidr", "")).strip(), strict=False)
        except ValueError:
            subnet = None
            problems.append("Subnet CIDR is required and must be valid")
        pool_start = self._ip_value(settings.get("pool_start", ""), "Pool start", problems)
        pool_end = self._ip_value(settings.get("pool_end", ""), "Pool end", problems)
        if subnet and pool_start and pool_start not in subnet:
            problems.append(f"Pool start must be inside {subnet}")
        if subnet and pool_end and pool_end not in subnet:
            problems.append(f"Pool end must be inside {subnet}")
        if pool_start and pool_end and int(pool_start) > int(pool_end):
            problems.append("Pool start must be lower than or equal to pool end")
        gateway = str(settings.get("gateway", "")).strip()
        if gateway:
            gateway_ip = self._ip_value(gateway, "Gateway", problems)
            if subnet and gateway_ip and gateway_ip not in subnet:
                problems.append(f"Gateway must be inside {subnet}")
        for dns_server in settings.get("dns_servers", []):
            self._ip_value(dns_server, f"DNS server {dns_server}", problems)
        for reservation in settings.get("reservations", []):
            mac = str(reservation.get("mac", "")).strip()
            if not re.match(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$", mac):
                problems.append("{} is not a valid reservation MAC address".format(mac or "blank MAC"))
            reserved_ip = self._ip_value(reservation.get("ip", ""), "Reservation IP", problems)
            if subnet and reserved_ip and reserved_ip not in subnet:
                problems.append(f"Reservation IP {reserved_ip} must be inside {subnet}")
        try:
            default_lease = int(settings.get("default_lease_time", 3600))
            max_lease = int(settings.get("max_lease_time", 7200))
        except (TypeError, ValueError):
            problems.append("Lease times must be whole numbers")
        else:
            if default_lease < 60 or default_lease > 604800:
                problems.append("Default lease time must be between 60 and 604800 seconds")
            if max_lease < default_lease or max_lease > 604800:
                problems.append(
                    "Max lease time must be greater than default lease time and no more than 604800 seconds"
                )
        return problems

    def _ip_value(self, value, label, problems):
        try:
            return ipaddress.ip_address(str(value).strip())
        except ValueError:
            problems.append(f"{label} must be a valid IP address")
            return None


class PyKMIPServiceAdapter(ServiceAdapter):
    config_dir = "/opt/vis/config/kms"
    config_path = "/opt/vis/config/kms/server.conf"
    policy_dir = "/opt/vis/config/kms/policies"
    unit_path = "/etc/systemd/system/vis-kms.service"
    service_name = "vis-kms.service"

    def validate(self) -> ValidationResult:
        problems = self._validation_problems()
        if problems:
            return ValidationResult(False, "; ".join(problems), utc_now())
        return ValidationResult(True, "Key Management Service configuration is valid", utc_now())

    def enable(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        if not validation.ok:
            self.service.configured = False
            self.service.enabled = False
            self.service.health_status = "needs_configuration"
            self.service.last_health_check_time = utc_now()
            return self.service
        self.service.configured = True
        self.service.enabled = True
        self.service.health_status = "starting"
        self._write_config()
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", self.service_name], check=False)
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", self.service_name], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled" if self.validate().ok else "needs_configuration"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if validation.ok:
            self._write_config()
            self._write_unit()
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            if self.service.enabled:
                subprocess.run(["systemctl", "restart", self.service_name], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        active = self._service_active()
        if not self.service.enabled:
            self.service.health_status = "disabled" if validation.ok else "needs_configuration"
        elif not validation.ok:
            self.service.health_status = "needs_configuration"
        elif active and self._port_open():
            self.service.health_status = "healthy"
        elif active:
            self.service.health_status = "starting"
        else:
            self.service.health_status = "stopped"
        return self.service

    def render_config(self) -> str:
        return "\n".join(
            [
                "# VIS Key Management Service",
                "engine = PyKMIP",
                "protocol = kmip",
                "listen = {}".format(self.service.settings.get("listen_address", "0.0.0.0")),
                "port = {}".format(self.service.settings.get("port", 5696)),
                f"database = {self._database_path()}",
                "tls_mode = shared",
                "tls_ca = {}".format(self.service.settings.get("tls_ca_path", "")),
                "tls_cert = {}".format(self.service.settings.get("tls_cert_path", "")),
                "tls_key = {}".format(self.service.settings.get("tls_key_path", "")),
                "",
            ]
        )

    def _write_config(self):
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.policy_dir, exist_ok=True)
        os.makedirs(self.service.filesystem_root, exist_ok=True)
        database_path = self._database_path()
        self.service.settings["database_path"] = database_path
        self.service.settings["config_path"] = self.config_path
        lines = [
            "[server]",
            "hostname={}".format(self.service.settings.get("listen_address", "0.0.0.0")),
            "port={}".format(int(self.service.settings.get("port", 5696))),
            "certificate_path={}".format(self.service.settings.get("tls_cert_path", "")),
            "key_path={}".format(self.service.settings.get("tls_key_path", "")),
            "ca_path={}".format(self.service.settings.get("tls_ca_path", "")),
            "auth_suite=TLS1.2",
            f"policy_path={self.policy_dir}",
            "logging_level=INFO",
            "",
            "[database]",
            "engine=sqlite",
            f"name={database_path}",
            "",
        ]
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        os.chmod(self.config_path, 0o600)

    def _write_unit(self):
        unit = f"""[Unit]
Description=VIS Key Management Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH=/opt/vis/app
ExecStart=/opt/vis/app/venv/bin/python -m vis.pykmip_compat -f {self.config_path} --ignore_tls_client_auth
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        with open(self.unit_path, "w", encoding="utf-8") as handle:
            handle.write(unit)

    def _validation_problems(self):
        problems = []
        try:
            port = int(self.service.settings.get("port", 5696))
            if port < 1 or port > 65535:
                problems.append("KMIP port must be between 1 and 65535")
        except (TypeError, ValueError):
            problems.append("KMIP port must be a whole number")
        for key, label in (
            ("tls_ca_path", "VIS Root CA"),
            ("tls_cert_path", "TLS certificate"),
            ("tls_key_path", "TLS private key"),
        ):
            path = str(self.service.settings.get(key, "")).strip()
            if not path or not os.path.isfile(path):
                problems.append(f"{label} is missing")
        return problems

    def _database_path(self):
        return str(self.service.settings.get("database_path", "")) or os.path.join(
            self.service.filesystem_root, "pykmip.db"
        )

    def _service_active(self):
        try:
            result = subprocess.run(["systemctl", "is-active", "--quiet", self.service_name], check=False)
        except OSError:
            return False
        return result.returncode == 0

    def _port_open(self):
        try:
            with socket.create_connection(("127.0.0.1", int(self.service.settings.get("port", 5696))), timeout=2):
                return True
        except OSError:
            return False


class LDAPProviderAdapter(ServiceAdapter):
    config_dir = "/opt/vis/config/ldap"
    apparmor_local_path = "/etc/apparmor.d/local/usr.sbin.slapd"
    unit_path = "/etc/systemd/system/vis-ldap.service"
    service_name = "vis-ldap.service"

    def validate(self) -> ValidationResult:
        missing = []
        for key, label in (
            ("base_dn", "Base DN"),
            ("bind_dn", "Bind DN"),
            ("admin_user", "Admin user"),
            ("admin_password", "Admin password"),
        ):
            if not str(self.service.settings.get(key, "")).strip():
                missing.append(label)
        if self._protocol() == "ldaps":
            for key, label in (
                ("tls_ca_path", "Root CA"),
                ("tls_cert_path", "TLS certificate"),
                ("tls_key_path", "TLS private key"),
            ):
                path = str(self.service.settings.get(key, ""))
                if not path or not os.path.isfile(path):
                    missing.append(label)
        if missing:
            return ValidationResult(False, "Missing: {}".format(", ".join(missing)), utc_now())
        return ValidationResult(True, "LDAP provider configuration is valid", utc_now())

    def enable(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if not validation.ok:
            self.service.enabled = False
            self.service.health_status = "needs_configuration"
            self.service.last_health_check_time = utc_now()
            return self.service
        self.service.enabled = True
        self._write_backend()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", self.service_name], check=False)
        self._refresh_memberof()
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", self.service_name], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled" if self.validate().ok else "needs_configuration"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if validation.ok:
            self._write_backend()
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            if self.service.enabled:
                subprocess.run(["systemctl", "restart", self.service_name], check=False)
                self._refresh_memberof()
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        if not self.service.enabled:
            self.service.health_status = "disabled" if validation.ok else "needs_configuration"
        elif not validation.ok:
            self.service.health_status = "needs_configuration"
        elif self._service_active() and self._ldap_search_ok():
            self.service.health_status = "healthy"
            self.service.last_validation_result = ValidationResult(
                True, "OpenLDAP service and base DN verified", utc_now()
            )
        elif self._service_active():
            self.service.health_status = "needs_configuration"
            self.service.last_validation_result = ValidationResult(
                False, "OpenLDAP service is active but LDAP bind/search failed", utc_now()
            )
        else:
            self.service.health_status = "stopped"
        return self.service

    def render_config(self) -> str:
        lines = [
            "# VIS LDAP Provider",
            "engine = OpenLDAP",
            f"protocol = {self._protocol()}",
            "port = {}".format(self.service.settings.get("port", 389)),
            "base_dn = {}".format(self.service.settings.get("base_dn", "")),
            "bind_dn = {}".format(self.service.settings.get("bind_dn", "")),
            "admin_user = {}".format(self.service.settings.get("admin_user", "")),
            f"data_root = {self.service.filesystem_root}",
            "users = {}".format(len(self._items("users"))),
            "groups = {}".format(len(self._items("groups"))),
        ]
        if self._protocol() == "ldaps":
            lines.extend(
                [
                    "tls_mode = {}".format(self.service.settings.get("tls_mode", "shared")),
                    "tls_ca = {}".format(self.service.settings.get("tls_ca_path", "")),
                    "tls_cert = {}".format(self.service.settings.get("tls_cert_path", "")),
                    "tls_key = {}".format(self.service.settings.get("tls_key_path", "")),
                ]
            )
        return "\n".join(lines) + "\n"

    def _protocol(self) -> str:
        return str(self.service.settings.get("protocol", "ldap")).lower()

    def _items(self, key: str) -> list[dict[str, object]]:
        items = self.service.settings.get(key, [])
        return items if isinstance(items, list) else []

    def _write_backend(self) -> None:
        self._write_config_files()
        self._write_apparmor_profile()
        subprocess.run(["systemctl", "stop", self.service_name], check=False)
        data_dir = self._data_dir()
        if os.path.isdir(data_dir):
            shutil.rmtree(data_dir)
        os.makedirs(data_dir, exist_ok=True)
        slapadd = subprocess.run(
            ["slapadd", "-f", self._slapd_conf_path(), "-l", self._bootstrap_ldif_path()],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if slapadd.returncode != 0:
            raise OSError(slapadd.stderr.strip() or "Unable to load VIS LDAP directory")
        subprocess.run(["chown", "-R", "openldap:openldap", self.service.filesystem_root], check=False)

    def _write_config_files(self) -> None:
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.service.filesystem_root, exist_ok=True)
        os.chmod(os.path.dirname(self.config_dir), 0o755)
        os.chmod(self.config_dir, 0o755)
        if self._protocol() == "ldaps":
            self._stage_tls_files()
        with open(self._slapd_conf_path(), "w", encoding="utf-8") as handle:
            handle.write(self._slapd_conf())
        with open(self._bootstrap_ldif_path(), "w", encoding="utf-8") as handle:
            handle.write(self._bootstrap_ldif())
        with open(self.unit_path, "w", encoding="utf-8") as handle:
            handle.write(self._unit_file())
        os.chmod(self._slapd_conf_path(), 0o644)
        os.chmod(self._bootstrap_ldif_path(), 0o600)

    def _write_apparmor_profile(self) -> None:
        profile_dir = os.path.dirname(self.apparmor_local_path)
        if not os.path.isdir(profile_dir):
            return
        rules = [
            "# VIS OpenLDAP Provider",
            "{} r,".format(self.config_dir.rstrip("/") + "/"),
            "{} r,".format(self.config_dir.rstrip("/") + "/**"),
            "{} r,".format(self.service.filesystem_root.rstrip("/") + "/"),
            "{} rwk,".format(self.service.filesystem_root.rstrip("/") + "/**"),
            "/run/vis-ldap/ rw,",
            "/run/vis-ldap/** rwk,",
        ]
        with open(self.apparmor_local_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rules) + "\n")
        subprocess.run(["apparmor_parser", "-r", "/etc/apparmor.d/usr.sbin.slapd"], check=False)

    def _slapd_conf(self) -> str:
        lines = [
            "include /etc/ldap/schema/core.schema",
            "include /etc/ldap/schema/cosine.schema",
            "include /etc/ldap/schema/inetorgperson.schema",
            "include /etc/ldap/schema/nis.schema",
            "pidfile /run/vis-ldap/slapd.pid",
            "argsfile /run/vis-ldap/slapd.args",
            "modulepath /usr/lib/ldap",
            "moduleload back_mdb",
            "moduleload memberof",
            "moduleload refint",
        ]
        if self._protocol() == "ldaps":
            tls_paths = self._ldap_tls_paths()
            lines.extend(
                [
                    "TLSCACertificateFile {}".format(tls_paths["ca"]),
                    "TLSCertificateFile {}".format(tls_paths["cert"]),
                    "TLSCertificateKeyFile {}".format(tls_paths["key"]),
                ]
            )
        lines.extend(
            [
                "database mdb",
                "maxsize 1073741824",
                f'suffix "{self._base_dn()}"',
                f'rootdn "{self._bind_dn()}"',
                f"rootpw {self._password_hash()}",
                f"directory {self._data_dir()}",
                "index objectClass eq",
                "index uid eq",
                f'access to * by dn.exact="{self._bind_dn()}" manage by * read',
                "overlay memberof",
                "memberof-group-oc groupOfNames",
                "memberof-member-ad member",
                "memberof-memberof-ad memberOf",
                "memberof-refint TRUE",
                "overlay refint",
                "refint_attributes memberof member manager owner",
                "",
            ]
        )
        return "\n".join(lines)

    def _refresh_memberof(self) -> None:
        if not self._service_active():
            return
        changes = []
        for group in self._items("groups"):
            name = self._safe_rdn(str(group.get("name", "")))
            if not name:
                continue
            members = self._group_members(group)
            if not members:
                members = [self._bind_dn()]
            changes.extend(
                [
                    f"dn: cn={name},ou=groups,{self._base_dn()}",
                    "changetype: modify",
                    "replace: member",
                ]
            )
            changes.extend(f"member: {member}" for member in members)
            changes.append("")
        if not changes:
            return
        env = os.environ.copy()
        env["LDAPTLS_REQCERT"] = "never"
        subprocess.run(
            [
                "ldapmodify",
                "-x",
                "-H",
                f"{self._protocol()}://127.0.0.1:{self._port()}",
                "-D",
                self._bind_dn(),
                "-w",
                str(self.service.settings.get("admin_password", "")),
            ],
            input="\n".join(changes) + "\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            check=False,
        )

    def _bootstrap_ldif(self) -> str:
        records = [
            [
                ("dn", self._base_dn()),
                ("objectClass", "top"),
                ("objectClass", "dcObject"),
                ("objectClass", "organization"),
                ("o", "VIS Directory"),
                ("dc", self._base_dc()),
                ("entryUUID", self._entry_uuid("base", self._base_dn())),
            ],
            [
                ("dn", f"ou=users,{self._base_dn()}"),
                ("objectClass", "top"),
                ("objectClass", "organizationalUnit"),
                ("ou", "users"),
                ("entryUUID", self._entry_uuid("ou", "users")),
            ],
            [
                ("dn", f"ou=groups,{self._base_dn()}"),
                ("objectClass", "top"),
                ("objectClass", "organizationalUnit"),
                ("ou", "groups"),
                ("entryUUID", self._entry_uuid("ou", "groups")),
            ],
        ]
        for user in self._items("users"):
            uid = str(user.get("uid", "")).strip()
            if not uid:
                continue
            display_name = str(user.get("display_name", uid)).strip() or uid
            surname = display_name.split()[-1] if display_name.split() else uid
            user_dn = self._user_dn(uid)
            record = [
                ("dn", user_dn),
                ("objectClass", "top"),
                ("objectClass", "person"),
                ("objectClass", "organizationalPerson"),
                ("objectClass", "inetOrgPerson"),
                ("objectClass", "extensibleObject"),
                ("cn", display_name),
                ("sn", surname),
                ("uid", uid),
                ("entryUUID", self._item_entry_uuid(user, "user", uid)),
                ("distinguishedName", user_dn),
            ]
            if user.get("email"):
                record.append(("mail", str(user.get("email"))))
            if user.get("password"):
                record.append(("userPassword", str(user.get("password"))))
            if user.get("disabled"):
                record.append(("description", "VIS disabled account"))
            records.append(record)
        for group in self._items("groups"):
            name = str(group.get("name", "")).strip()
            if not name:
                continue
            group_dn = f"cn={name},ou=groups,{self._base_dn()}"
            members = []
            for member_id in group.get("members", []):
                user = self._user_by_id(str(member_id))
                if user and str(user.get("uid", "")).strip():
                    members.append(self._user_dn(str(user.get("uid"))))
            if not members:
                members = [self._bind_dn()]
            record = [
                ("dn", group_dn),
                ("objectClass", "top"),
                ("objectClass", "groupOfNames"),
                ("objectClass", "extensibleObject"),
                ("cn", name),
                ("entryUUID", self._item_entry_uuid(group, "group", name)),
                ("distinguishedName", group_dn),
            ]
            if group.get("description"):
                record.append(("description", str(group.get("description"))))
            for member in members:
                record.append(("member", member))
            records.append(record)
        return "\n\n".join(self._ldif_record(record) for record in records) + "\n"

    def _unit_file(self) -> str:
        urls = "ldap://0.0.0.0:389/"
        if self._protocol() == "ldaps":
            urls = "ldaps://0.0.0.0:636/"
        return f"""[Unit]
Description=VIS OpenLDAP Provider
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
ExecStartPre=/usr/bin/install -d -o openldap -g openldap -m 755 /run/vis-ldap
ExecStart=/usr/sbin/slapd -h "{urls}" -f {self._slapd_conf_path()} -u openldap -g openldap
ExecStop=/bin/kill -TERM $MAINPID
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""

    def _ldap_search_ok(self) -> bool:
        env = os.environ.copy()
        env["LDAPTLS_REQCERT"] = "never"
        command = [
            "ldapsearch",
            "-x",
            "-H",
            "{}://127.0.0.1:{}".format(
                self._protocol(), self.service.settings.get("port", 636 if self._protocol() == "ldaps" else 389)
            ),
            "-D",
            self._bind_dn(),
            "-w",
            str(self.service.settings.get("admin_password", "")),
            "-b",
            self._base_dn(),
            "-s",
            "base",
            "dn",
        ]
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=False
        )
        return result.returncode == 0 and self._base_dn().lower() in result.stdout.lower()

    def _service_active(self) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", self.service_name], check=False)
        return result.returncode == 0

    def _password_hash(self) -> str:
        password = str(self.service.settings.get("admin_password", ""))
        result = subprocess.run(
            ["slappasswd", "-s", password], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "{CLEARTEXT}" + password.replace("\n", "")

    def _stage_tls_files(self) -> None:
        tls_paths = self._ldap_tls_paths()
        os.makedirs(os.path.dirname(tls_paths["cert"]), exist_ok=True)
        shutil.copy2(str(self.service.settings.get("tls_ca_path", "")), tls_paths["ca"])
        shutil.copy2(str(self.service.settings.get("tls_cert_path", "")), tls_paths["cert"])
        shutil.copy2(str(self.service.settings.get("tls_key_path", "")), tls_paths["key"])
        os.chmod(tls_paths["ca"], 0o644)
        os.chmod(tls_paths["cert"], 0o644)
        os.chmod(tls_paths["key"], 0o640)
        subprocess.run(["chown", "-R", "openldap:openldap", os.path.dirname(tls_paths["cert"])], check=False)

    def _ldap_tls_paths(self):
        tls_dir = os.path.join(self.config_dir, "tls")
        return {
            "ca": os.path.join(tls_dir, "rootCA.pem"),
            "cert": os.path.join(tls_dir, "server.crt"),
            "key": os.path.join(tls_dir, "server.key"),
        }

    def _slapd_conf_path(self) -> str:
        return os.path.join(self.config_dir, "slapd.conf")

    def _bootstrap_ldif_path(self) -> str:
        return os.path.join(self.config_dir, "bootstrap.ldif")

    def _data_dir(self) -> str:
        return os.path.join(self.service.filesystem_root, "db")

    def _base_dn(self) -> str:
        return str(self.service.settings.get("base_dn", "")).strip()

    def _port(self) -> int:
        return int(self.service.settings.get("port", 636 if self._protocol() == "ldaps" else 389))

    def _bind_dn(self) -> str:
        return str(self.service.settings.get("bind_dn", "")).strip()

    def _base_dc(self) -> str:
        for part in self._base_dn().split(","):
            part = part.strip()
            if part.lower().startswith("dc="):
                return part.split("=", 1)[1]
        return "vis"

    def _user_dn(self, uid: str) -> str:
        return f"uid={uid},ou=users,{self._base_dn()}"

    def _user_by_id(self, user_id: str):
        for user in self._items("users"):
            if str(user.get("id")) == user_id:
                return user
        return None

    def _group_members(self, group) -> list[str]:
        members = []
        for member_id in group.get("members", []):
            user = self._user_by_id(str(member_id))
            if user and str(user.get("uid", "")).strip():
                members.append(self._user_dn(str(user.get("uid"))))
        return members

    def _item_entry_uuid(self, item, kind: str, key: str) -> str:
        existing = str(item.get("entry_uuid", "")).strip()
        try:
            return str(uuid.UUID(existing))
        except (ValueError, TypeError, AttributeError):
            return self._entry_uuid(kind, key)

    def _entry_uuid(self, kind: str, key: str) -> str:
        value = f"vis:{self._base_dn().lower()}:{kind}:{str(key).lower()}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, value))

    def _safe_rdn(self, value: str) -> str:
        return value.strip().replace(",", "").replace("=", "")

    def _ldif_record(self, pairs) -> str:
        lines = []
        for key, value in pairs:
            lines.append("{}: {}".format(key, str(value).replace("\n", " ")))
        return "\n".join(lines)


class OIDCProviderAdapter(ServiceAdapter):
    config_dir = "/opt/vis/config/oidc"
    theme_source_dir = "/opt/vis/app/vis/keycloak-theme"
    unit_path = "/etc/systemd/system/vis-identity.service"
    service_name = "vis-identity.service"

    def validate(self) -> ValidationResult:
        missing = []
        for key, label in (
            ("admin_user", "Admin user"),
            ("admin_password", "Admin password"),
            ("realm", "Realm"),
            ("default_group", "Default group"),
        ):
            if not str(self.service.settings.get(key, "")).strip():
                missing.append(label)
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
            for key, label in (("tls_cert_path", "TLS certificate"), ("tls_key_path", "TLS private key")):
                path = str(self.service.settings.get(key, ""))
                if not path or not os.path.isfile(path):
                    missing.append(label)
        if missing:
            return ValidationResult(False, "Missing: {}".format(", ".join(missing)), utc_now())
        return ValidationResult(True, "OIDC Provider configuration is valid", utc_now())

    def enable(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if not validation.ok:
            self.service.enabled = False
            self.service.health_status = "needs_configuration"
            self.service.last_health_check_time = utc_now()
            return self.service
        self.service.enabled = True
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", self.service_name], check=False)
        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", self.service_name], check=False)
        subprocess.run(["docker", "rm", "-f", "vis-keycloak"], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled" if self.validate().ok else "needs_configuration"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.configured = validation.ok
        if validation.ok:
            self._write_unit()
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            if self.service.enabled:
                subprocess.run(["systemctl", "restart", self.service_name], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        if not self.service.enabled:
            self.service.health_status = "disabled" if validation.ok else "needs_configuration"
            return self.service
        if not validation.ok:
            self.service.health_status = "needs_configuration"
            return self.service
        if not self._service_active():
            self.service.health_status = "stopped"
            return self.service
        if self._wait_ready(timeout=45):
            try:
                self._sync_realm_group_users_and_clients()
                self.service.health_status = "healthy"
                self.service.last_validation_result = ValidationResult(
                    True, f"Keycloak realm {self._realm()} and groups verified", utc_now()
                )
            except OSError as err:
                self.service.health_status = "needs_configuration"
                self.service.last_validation_result = ValidationResult(False, str(err), utc_now())
        else:
            self.service.health_status = "stopped"
        return self.service

    def render_config(self) -> str:
        lines = [
            "# VIS OIDC Provider",
            "engine = Keycloak",
            f"image = {self._image()}",
            f"login_theme = {self._login_theme()}",
            f"protocol = {self._protocol()}",
            f"port = {self._port()}",
            f"realm = {self._realm()}",
            f"default_group = {self._group()}",
            "admin_user = {}".format(self.service.settings.get("admin_user", "")),
            f"data_root = {self.service.filesystem_root}",
            f"users = {len(self._users())}",
            f"groups = {len(self._groups())}",
            f"oidc_clients = {len(self._oidc_clients())}",
        ]
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
            lines.extend(
                [
                    "tls_mode = {}".format(self.service.settings.get("tls_mode", "shared")),
                    "tls_cert = {}".format(self.service.settings.get("tls_cert_path", "")),
                    "tls_key = {}".format(self.service.settings.get("tls_key_path", "")),
                ]
            )
        return "\n".join(lines) + "\n"

    def _write_unit(self) -> None:
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.service.filesystem_root, exist_ok=True)
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
            self._stage_tls_files()
        theme_dir = self._stage_theme()
        env_path = os.path.join(self.config_dir, "keycloak.env")
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write(
                "KC_BOOTSTRAP_ADMIN_USERNAME={}\n".format(
                    self._env_value(str(self.service.settings.get("admin_user", "")))
                )
            )
            handle.write(
                "KC_BOOTSTRAP_ADMIN_PASSWORD={}\n".format(
                    self._env_value(str(self.service.settings.get("admin_password", "")))
                )
            )
        os.chmod(env_path, 0o600)
        container_port = 8443 if self._protocol() == "https" else 8080
        start_args = "start-dev --hostname-strict=false"
        tls_mount = ""
        if self._protocol() == "https":
            tls_paths = self._keycloak_tls_paths()
            start_args = "start-dev --http-enabled=false --https-port=8443 --hostname-strict=false --https-certificate-file=/opt/keycloak/vis-tls/server.crt --https-certificate-key-file=/opt/keycloak/vis-tls/server.key"
            tls_mount = "-v {tls_dir}:/opt/keycloak/vis-tls:ro ".format(tls_dir=os.path.dirname(tls_paths["cert"]))
        theme_mount = f"-v {theme_dir}:/opt/keycloak/themes/{self._login_theme()}:ro "
        unit = f"""[Unit]
Description=VIS Keycloak OIDC Provider
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={env_path}
ExecStartPre=-/usr/bin/docker rm -f vis-keycloak
ExecStartPre=/usr/bin/install -d -o 1000 -g 0 -m 775 {self.service.filesystem_root}
ExecStart=/usr/bin/docker run --name vis-keycloak --rm --env-file {env_path} -p {self._port()}:{container_port} -v {self.service.filesystem_root}:/opt/keycloak/data {tls_mount}{theme_mount}{self._image()} {start_args}
ExecStop=/usr/bin/docker stop vis-keycloak
Restart=on-failure
RestartSec=5
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
"""
        with open(self.unit_path, "w", encoding="utf-8") as handle:
            handle.write(unit)

    def _sync_realm_group_users_and_clients(self) -> None:
        token = self._admin_token()
        self._ensure_realm(token)
        self._ensure_realm_theme(token)
        group_keycloak_ids = {}
        for group in self._groups_with_default():
            name = str(group.get("name", "")).strip()
            if name:
                group_keycloak_ids[str(group.get("id", ""))] = self._ensure_group(token, name)
        for user in self._users():
            group_ids = [
                group_keycloak_ids[group_id] for group_id in user.get("groups", []) if group_id in group_keycloak_ids
            ]
            if not group_ids and group_keycloak_ids:
                default = next(
                    (
                        group
                        for group in self._groups_with_default()
                        if str(group.get("name", "")).lower() == self._group().lower()
                    ),
                    None,
                )
                if default and default.get("id") in group_keycloak_ids:
                    group_ids = [group_keycloak_ids[default.get("id")]]
            self._ensure_user(token, user, group_ids)
        for client in self._oidc_clients():
            self.ensure_oidc_client(client, token=token)

    def _ensure_realm(self, token: str) -> None:
        realm = self._realm()
        try:
            self._kc_json("GET", "/admin/realms/{}".format(parse.quote(realm, safe="")), token=token)
        except OSError:
            self._kc_json("POST", "/admin/realms", token=token, payload={"realm": realm, "enabled": True})

    def _ensure_realm_theme(self, token: str) -> None:
        realm_name = self._realm()
        realm_path = "/admin/realms/{}".format(parse.quote(realm_name, safe=""))
        try:
            realm = self._kc_json("GET", realm_path, token=token)
        except OSError:
            realm = {"realm": realm_name, "enabled": True}
        realm["enabled"] = realm.get("enabled", True)
        realm["loginTheme"] = self._login_theme()
        self._kc_json("PUT", realm_path, token=token, payload=realm)

    def _ensure_group(self, token: str, group_name: str = None) -> str:
        realm = parse.quote(self._realm(), safe="")
        group_name = str(group_name or self._group()).strip()
        groups = self._kc_json("GET", f"/admin/realms/{realm}/groups?search={parse.quote(group_name)}", token=token)
        for group in groups or []:
            if group.get("name") == group_name:
                return str(group.get("id"))
        self._kc_json("POST", f"/admin/realms/{realm}/groups", token=token, payload={"name": group_name})
        groups = self._kc_json("GET", f"/admin/realms/{realm}/groups?search={parse.quote(group_name)}", token=token)
        for group in groups or []:
            if group.get("name") == group_name:
                return str(group.get("id"))
        raise OSError(f"Unable to create Keycloak group {group_name}")

    def _ensure_user(self, token: str, user: dict[str, object], group_ids: list[str]) -> None:
        username = str(user.get("username", "")).strip()
        password = str(user.get("password", ""))
        if not username or not password:
            return
        realm = parse.quote(self._realm(), safe="")
        matches = self._kc_json(
            "GET", f"/admin/realms/{realm}/users?username={parse.quote(username)}&exact=true", token=token
        )
        if matches:
            user_id = str(matches[0].get("id"))
            payload = {
                "username": username,
                "enabled": not bool(user.get("disabled", False)),
                "firstName": str(user.get("first_name", "")),
                "lastName": str(user.get("last_name", "")),
                "email": str(user.get("email", "")),
            }
            self._kc_json(
                "PUT",
                "/admin/realms/{}/users/{}".format(realm, parse.quote(user_id, safe="")),
                token=token,
                payload=payload,
            )
        else:
            payload = {
                "username": username,
                "enabled": not bool(user.get("disabled", False)),
                "firstName": str(user.get("first_name", "")),
                "lastName": str(user.get("last_name", "")),
                "email": str(user.get("email", "")),
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            }
            self._kc_json("POST", f"/admin/realms/{realm}/users", token=token, payload=payload)
            matches = self._kc_json(
                "GET", f"/admin/realms/{realm}/users?username={parse.quote(username)}&exact=true", token=token
            )
            if not matches:
                raise OSError(f"Unable to create Keycloak user {username}")
            user_id = str(matches[0].get("id"))
        for group_id in group_ids:
            self._kc_json("PUT", f"/admin/realms/{realm}/users/{user_id}/groups/{group_id}", token=token)

    def ensure_oidc_client(self, client: dict[str, object], token: str = None) -> dict[str, object]:
        client_id = str(client.get("client_id", "")).strip()
        redirect_url = str(client.get("redirect_url", "")).strip()
        if not client_id or not redirect_url:
            raise OSError("OIDC Client ID and Redirect URL are required")
        token = token or self._admin_token()
        self._ensure_realm(token)
        realm = parse.quote(self._realm(), safe="")
        payload = {
            "clientId": client_id,
            "name": client_id,
            "protocol": "openid-connect",
            "enabled": True,
            "publicClient": False,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": True,
            "serviceAccountsEnabled": False,
            "redirectUris": [redirect_url],
            "webOrigins": ["+"],
        }
        keycloak_id = str(client.get("keycloak_id", "")).strip()
        if keycloak_id:
            self._kc_json(
                "PUT",
                "/admin/realms/{}/clients/{}".format(realm, parse.quote(keycloak_id, safe="")),
                token=token,
                payload=payload,
            )
        else:
            matches = self._kc_json(
                "GET", f"/admin/realms/{realm}/clients?clientId={parse.quote(client_id)}", token=token
            )
            for match in matches or []:
                if match.get("clientId") == client_id:
                    keycloak_id = str(match.get("id", ""))
                    break
            if keycloak_id:
                self._kc_json(
                    "PUT",
                    "/admin/realms/{}/clients/{}".format(realm, parse.quote(keycloak_id, safe="")),
                    token=token,
                    payload=payload,
                )
            else:
                self._kc_json("POST", f"/admin/realms/{realm}/clients", token=token, payload=payload)
                matches = self._kc_json(
                    "GET", f"/admin/realms/{realm}/clients?clientId={parse.quote(client_id)}", token=token
                )
                for match in matches or []:
                    if match.get("clientId") == client_id:
                        keycloak_id = str(match.get("id", ""))
                        break
        if not keycloak_id:
            raise OSError(f"Unable to create Keycloak OIDC client {client_id}")
        secret_payload = self._kc_json(
            "GET",
            "/admin/realms/{}/clients/{}/client-secret".format(realm, parse.quote(keycloak_id, safe="")),
            token=token,
        )
        secret = str(secret_payload.get("value", "")).strip()
        if not secret:
            raise OSError(f"Keycloak did not return a client secret for {client_id}")
        updated = dict(client)
        updated.update(
            {"client_id": client_id, "redirect_url": redirect_url, "keycloak_id": keycloak_id, "client_secret": secret}
        )
        return updated

    def delete_oidc_client(self, client: dict[str, object]) -> None:
        keycloak_id = str(client.get("keycloak_id", "")).strip()
        if not keycloak_id:
            return
        token = self._admin_token()
        realm = parse.quote(self._realm(), safe="")
        self._kc_json(
            "DELETE", "/admin/realms/{}/clients/{}".format(realm, parse.quote(keycloak_id, safe="")), token=token
        )

    def _admin_token(self) -> str:
        body = parse.urlencode(
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": str(self.service.settings.get("admin_user", "")),
                "password": str(self.service.settings.get("admin_password", "")),
            }
        ).encode("utf-8")
        req = request.Request(
            self._base_url() + "/realms/master/protocol/openid-connect/token", data=body, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._urlopen(req, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.URLError as err:
            raise OSError(f"Unable to authenticate to Keycloak: {err}") from err
        token = payload.get("access_token")
        if not token:
            raise OSError("Keycloak did not return an admin token")
        return str(token)

    def _kc_json(self, method: str, path: str, token: str, payload: dict[str, object] = None):
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(self._base_url() + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._urlopen(req, timeout=15) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as err:
            if err.code in (201, 204):
                return {}
            raise OSError(f"Keycloak API {method} {path} failed with HTTP {err.code}") from err
        except error.URLError as err:
            raise OSError(f"Keycloak API {method} {path} failed: {err}") from err

    def _wait_ready(self, timeout: int = 90) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with self._urlopen(self._base_url() + "/realms/master", timeout=5) as response:
                    if response.status < 500:
                        return True
            except Exception:
                time.sleep(3)
        return False

    def _service_active(self) -> bool:
        result = subprocess.run(["systemctl", "is-active", "--quiet", self.service_name], check=False)
        return result.returncode == 0

    def _apply_shared_tls_settings(self) -> None:
        self.service.settings["tls_mode"] = "shared"
        self.service.settings["tls_cert_path"] = "/opt/vis/config/tls/server.crt"
        self.service.settings["tls_key_path"] = "/opt/vis/config/tls/server.key"
        self.service.settings["tls_ca_path"] = "/opt/vis/config/tls/rootCA.pem"
        self.service.settings["tls_full_pem_path"] = "/opt/vis/config/tls/vis-full.pem"

    def _stage_tls_files(self) -> None:
        tls_paths = self._keycloak_tls_paths()
        os.makedirs(os.path.dirname(tls_paths["cert"]), exist_ok=True)
        shutil.copy2(str(self.service.settings.get("tls_ca_path", "")), tls_paths["ca"])
        shutil.copy2(str(self.service.settings.get("tls_cert_path", "")), tls_paths["cert"])
        shutil.copy2(str(self.service.settings.get("tls_key_path", "")), tls_paths["key"])
        os.chmod(tls_paths["ca"], 0o644)
        os.chmod(tls_paths["cert"], 0o644)
        os.chmod(tls_paths["key"], 0o640)
        subprocess.run(["chown", "-R", "1000:0", os.path.dirname(tls_paths["cert"])], check=False)

    def _keycloak_tls_paths(self):
        tls_dir = os.path.join(self.config_dir, "tls")
        return {
            "ca": os.path.join(tls_dir, "rootCA.pem"),
            "cert": os.path.join(tls_dir, "server.crt"),
            "key": os.path.join(tls_dir, "server.key"),
        }

    def _stage_theme(self) -> str:
        theme_dir = os.path.join(self.config_dir, "themes", self._login_theme())
        if os.path.isdir(theme_dir):
            shutil.rmtree(theme_dir)
        shutil.copytree(self.theme_source_dir, theme_dir)
        subprocess.run(["chown", "-R", "1000:0", theme_dir], check=False)
        return theme_dir

    def _protocol(self) -> str:
        return str(self.service.settings.get("protocol", "http")).lower()

    def _port(self) -> int:
        return int(self.service.settings.get("port", 9081))

    def _realm(self) -> str:
        return str(self.service.settings.get("realm", "VCF")).strip() or "VCF"

    def _group(self) -> str:
        return str(self.service.settings.get("default_group", "vcf-admins")).strip() or "vcf-admins"

    def _image(self) -> str:
        return str(self.service.settings.get("image", "quay.io/keycloak/keycloak:26.3")).strip()

    def _login_theme(self) -> str:
        return str(self.service.settings.get("login_theme", "vis")).strip() or "vis"

    def _users(self) -> list[dict[str, object]]:
        users = self.service.settings.get("users", [])
        return users if isinstance(users, list) else []

    def _groups(self) -> list[dict[str, object]]:
        groups = self.service.settings.get("groups", [])
        return groups if isinstance(groups, list) else []

    def _groups_with_default(self) -> list[dict[str, object]]:
        groups = [dict(group) for group in self._groups() if isinstance(group, dict)]
        expected = [
            {"id": "vcf-admins", "name": self._group(), "description": "Default VCF administrators", "members": []},
            {"id": "vcf-users", "name": "vcf-users", "description": "Default VCF users", "members": []},
        ]
        names = {str(group.get("name", "")).lower() for group in groups}
        for group in expected:
            if str(group["name"]).lower() not in names:
                groups.append(group)
                names.add(str(group["name"]).lower())
        return groups

    def _oidc_clients(self) -> list[dict[str, object]]:
        clients = self.service.settings.get("oidc_clients", [])
        return clients if isinstance(clients, list) else []

    def _base_url(self) -> str:
        return f"{self._protocol()}://127.0.0.1:{self._port()}"

    def _env_value(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "")

    def _urlopen(self, target, timeout: int):
        if self._protocol() == "https":
            return request.urlopen(target, timeout=timeout, context=ssl._create_unverified_context())
        return request.urlopen(target, timeout=timeout)


class LocalContentLibraryServiceAdapter(ServiceAdapter):
    def validate(self) -> ValidationResult:
        missing = []
        if not os.path.isdir(self.service.filesystem_root):
            missing.append(self.service.filesystem_root)
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
            for key in ("tls_cert_path", "tls_key_path"):
                if not os.path.isfile(str(self.service.settings.get(key, ""))):
                    missing.append(str(self.service.settings.get(key, key)))
        if missing:
            return ValidationResult(False, "Missing: {}".format(", ".join(missing)), utc_now())
        return ValidationResult(True, "Content Library configuration is valid", utc_now())

    def has_synced(self) -> bool:
        stats_file = Path(self.service.filesystem_root, "cache", _SYNC_STATS_FILE)
        if not stats_file.is_file():
            return False

        try:
            stats = ContentLibrarySyncStats.from_json(stats_file.read_bytes())
            return stats.last_sync_result == "SUCCESS"
        except:
            return False

    def enable(self) -> ServiceDefinition:
        self.service.enabled = True
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", "vis-content-library-server.service"], check=False)
        if self._auto_sync_enabled():
            subprocess.run(["systemctl", "enable", "--now", "vis-content-library-sync.timer"], check=False)

        return self.health_check()

    def disable(self) -> ServiceDefinition:
        subprocess.run(["systemctl", "disable", "--now", "vis-content-library-server.service"], check=False)
        subprocess.run(["systemctl", "disable", "--now", "vis-content-library-sync.timer"], check=False)
        self.service.enabled = False
        self.service.health_status = "disabled"
        self.service.last_health_check_time = utc_now()
        return self.service

    def restart(self) -> ServiceDefinition:
        self._write_unit()
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        if self.service.enabled:
            subprocess.run(["systemctl", "restart", "vis-content-library-server.service"], check=False)
            if self._auto_sync_enabled():
                subprocess.run(["systemctl", "restart", "vis-content-library-sync.timer"], check=False)
        return self.health_check()

    def health_check(self) -> ServiceDefinition:
        validation = self.validate()
        self.service.last_validation_result = validation
        self.service.last_health_check_time = utc_now()
        self.service.configured = validation.ok
        active = self._service_active()
        if not self.service.enabled:
            self.service.health_status = "disabled"
        elif validation.ok:
            if active and self.has_synced():
                self.service.health_status = "healthy"
            elif active:
                self.service.health_status = "needs_upstream_sync"
            else:
                self.service.health_status = "stopped"
        else:
            self.service.health_status = "needs_configuration"
        return self.service

    def render_config(self) -> str:
        lines = [
            "# vSphere Content Library Service",
            f"protocol = {self._protocol()}",
            f"port = {self._port()}",
            f"root = {self.service.filesystem_root}",
            f"basic_auth_enabled = {self._basic_auth_enabled()}",
            "source_url = {}".format(
                self.service.settings.get("source_url", "https://wp-content.broadcom.com/v2/latest/lib.json")
            ),
            f"auto_source_sync_enabled = {self._auto_sync_enabled()}",
            f"worker_pool_size = {self.service.settings.get("worker_pool_size", "25")}",
        ]

        if self._basic_auth_enabled():
            lines.append("auth_user = {}".format(self.service.settings.get("auth_user", "")))
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
            lines.extend(
                [
                    "tls_mode = {}".format(self.service.settings.get("tls_mode", "shared")),
                    "tls_cert = {}".format(self.service.settings.get("tls_cert_path", "")),
                    "tls_key = {}".format(self.service.settings.get("tls_key_path", "")),
                    "tls_full_pem = {}".format(self.service.settings.get("tls_full_pem_path", "")),
                ]
            )

        return "\n".join(lines) + "\n"

    def _write_unit(self) -> None:
        os.makedirs(self.service.filesystem_root, exist_ok=True)
        self._write_server_unit()
        self._write_sync_unit()

    def _write_server_unit(self) -> None:
        if self._protocol() == "https":
            self._apply_shared_tls_settings()
        auth_user = str(self.service.settings.get("auth_user", "")) if self._basic_auth_enabled() else ""
        auth_password = str(self.service.settings.get("auth_password", "")) if self._basic_auth_enabled() else ""
        unit = f"""[Unit]
Description=vSphere Content Library Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/vis/app
Environment=PYTHONPATH=/opt/vis/app
Environment=VIS_CONTENT_LIB_ROOT={self.service.filesystem_root}
Environment=VIS_CONTENT_LIB_PROTOCOL={self._protocol()}
Environment=VIS_CONTENT_LIB_PORT={self._port()}
Environment=VIS_CONTENT_LIB_AUTH_USER={auth_user}
Environment=VIS_CONTENT_LIB_AUTH_PASSWORD={auth_password}
Environment=VIS_CONTENT_LIB_TLS_CERT={self.service.settings.get("tls_cert_path", "")}
Environment=VIS_CONTENT_LIB_TLS_KEY={self.service.settings.get("tls_key_path", "")}
ExecStart=/opt/vis/app/venv/bin/python -m vis.content_library_server
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
        with open("/etc/systemd/system/vis-content-library-server.service", "w") as handle:
            handle.write(unit)

    def _write_sync_unit(self):
        timer = f"""[Unit]
Description=vSphere Content Library Synchronization Timer

[Timer]
Unit=vis-content-library-sync.service
OnCalendar={self.service.settings.get("sync_schedule", "Sun 8:06")}

[Install]
WantedBy=timers.target
"""

        unit = f"""[Unit]
Description=vSphere Content Library Upstream Sync Service
Requires=vis-content-library-sync.timer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/vis/app
Environment=PYTHONPATH=/opt/vis/app
Environment=VIS_CONTENT_LIB_ROOT={self.service.filesystem_root}
Environment=VIS_CONTENT_LIB_SOURCE_URL={self.service.settings.get("source_url", "https://wp-content.broadcom.com/v2/latest/lib.json")}
Environment=VIS_CONTENT_LIB_SOURCE_USER={self.service.settings.get("source_user", "")}
Environment=VIS_CONTENT_LIB_SOURCE_PASSWORD={self.service.settings.get("source_password", "")}
Environment=VIS_CONTENT_LIB_WORKER_POOL_SIZE={self.service.settings.get("worker_pool_size", 25)}
ExecStart=/opt/vis/app/venv/bin/python -m vis.content_library_sync
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
        with open("/etc/systemd/system/vis-content-library-sync.service", "w") as handle:
            handle.write(unit)

        with open("/etc/systemd/system/vis-content-library-sync.timer", "w") as handle:
            handle.write(timer)

    def _service_active(self) -> bool:
        is_server_active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "vis-content-library-server.service"], check=False
        ).returncode == 0

        sync_test = not self._auto_sync_enabled() or subprocess.run(
            ["systemctl", "is-active", "--quiet", "vis-content-library-sync.timer"], check=False
        ).returncode == 0
       
        return is_server_active and sync_test

    def _protocol(self) -> str:
        return str(self.service.settings.get("protocol", "http")).lower()

    def _port(self) -> int:
        return int(self.service.settings.get("port", 9943 if self._protocol() == "https" else 9091))

    def _basic_auth_enabled(self) -> bool:
        return bool(self.service.settings.get("basic_auth_enabled", False))

    def _auto_sync_enabled(self) -> bool:
        return bool(self.service.settings.get("auto_source_sync_enabled", True))

    def _apply_shared_tls_settings(self) -> None:
        self.service.settings.update(
            {
                "tls_enabled": True,
                "tls_mode": "shared",
                "tls_ca_path": "/opt/vis/config/tls/rootCA.pem",
                "tls_cert_path": "/opt/vis/config/tls/server.crt",
                "tls_key_path": "/opt/vis/config/tls/server.key",
                "tls_full_pem_path": "/opt/vis/config/tls/vis-full.pem",
            }
        )


class ServiceManager:
    def __init__(self, store: ServiceStore, health_followup_attempts: int = 30, health_followup_interval: int = 10):
        self.store = store
        self.health_followup_attempts = health_followup_attempts
        self.health_followup_interval = health_followup_interval
        self._health_followups = set()
        self._health_followup_lock = threading.Lock()

    def list_services(self) -> list[ServiceDefinition]:
        return self.store.list_services()

    def get_service(self, service_id: str) -> ServiceDefinition:
        service = self.store.get_service(service_id)
        if not service:
            raise KeyError(service_id)
        return service

    def service_summary(self) -> dict[str, object]:
        services = self.list_services()
        enabled = len([service for service in services if service.enabled])
        configured = len([service for service in services if service.configured])
        if enabled == 0:
            health = "Ready to configure"
            health_note = "Enable services after configuration"
        elif all(service.health_status in ("healthy", "disabled", "needs_configuration") for service in services):
            health = "Healthy"
            health_note = "Enabled services are healthy"
        else:
            health = "Needs attention"
            health_note = "Review enabled service health"
        return {
            "total": len(services),
            "enabled": enabled,
            "configured": configured,
            "health": health,
            "health_note": health_note,
        }

    def adapter_for(self, service_id: str) -> ServiceAdapter:
        service = self.get_service(service_id)
        service_id = service.id
        if os.environ.get("VIS_ENABLE_LOCAL_ADAPTERS") == "1" and service_id == "web-depot":
            return LocalDepotServiceAdapter(service)
        if os.environ.get("VIS_ENABLE_LOCAL_ADAPTERS") == "1" and service_id == "harbor-registry":
            return LocalHarborServiceAdapter(service)
        if os.environ.get("VIS_ENABLE_LOCAL_ADAPTERS") == "1" and service_id == "sftp-backup":
            return LocalSFTPServiceAdapter(service)
        if os.environ.get("VIS_ENABLE_LOCAL_ADAPTERS") == "1" and service_id == "content-library":
            return LocalContentLibraryServiceAdapter(service)
        if service_id == "unbound-dns":
            return DNSServiceAdapter(service)
        if service_id == "time-server":
            return TimeServerAdapter(service)
        if service_id == "dhcp-server":
            return DHCPServerAdapter(service)
        if service_id == "kms-service":
            return PyKMIPServiceAdapter(service)
        if service_id == "ldap-provider":
            return LDAPProviderAdapter(service)
        if os.environ.get("VIS_ENABLE_LOCAL_ADAPTERS") == "1" and service_id == "oidc-provider":
            return OIDCProviderAdapter(service)
        return MockServiceAdapter(service)

    def run_health_check(self, service_id: str) -> ServiceDefinition:
        service = self.adapter_for(service_id).health_check()
        self.store.save_service(service)
        return service

    def enable_service(self, service_id: str) -> ServiceDefinition:
        service = self.adapter_for(service_id).enable()
        self._save_service_and_monitor_starting(service)
        return service

    def disable_service(self, service_id: str) -> ServiceDefinition:
        service = self.adapter_for(service_id).disable()
        self.store.save_service(service)
        return service

    def restart_service(self, service_id: str) -> ServiceDefinition:
        service = self.adapter_for(service_id).restart()
        self._save_service_and_monitor_starting(service)
        return service

    def _save_service_and_monitor_starting(self, service: ServiceDefinition) -> None:
        self.store.save_service(service)
        if service.enabled and service.health_status == "starting":
            self._schedule_health_followup(service.id)

    def _schedule_health_followup(self, service_id: str) -> None:
        with self._health_followup_lock:
            if service_id in self._health_followups:
                return
            self._health_followups.add(service_id)
        self._start_health_followup_thread(service_id)

    def _start_health_followup_thread(self, service_id: str) -> None:
        thread = threading.Thread(target=self._followup_health_checks, args=(service_id,), daemon=True)
        thread.start()

    def _followup_health_checks(self, service_id: str) -> None:
        try:
            for _ in range(self.health_followup_attempts):
                if self.health_followup_interval:
                    time.sleep(self.health_followup_interval)
                current = self.get_service(service_id)
                if not current.enabled or current.health_status != "starting":
                    break
                service = self.adapter_for(service_id).health_check()
                self.store.save_service(service)
                if service.health_status != "starting":
                    break
        except Exception:
            pass
        finally:
            with self._health_followup_lock:
                self._health_followups.discard(service_id)
