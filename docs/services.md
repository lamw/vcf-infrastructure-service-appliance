# Services

VIS services are disabled by default. Configure and enable only the services needed for a given lab or proof-of-concept environment.

## Table of Contents

- [📦 Software Depot](#software-depot)
- [💾 SFTP Backup](#sftp-backup)
- [🐳 Container Registry](#container-registry)
- [📇 LDAP Provider](#ldap-provider)
- [🔐 OIDC Provider](#oidc-provider)
- [🌐 DNS Server](#dns-server)
- [⏱ NTP Server](#ntp-server)
- [🧭 DHCP Server](#dhcp-server)
- [🔑 Key Management Service](#key-management-service)
- [🛡️ Certificate Authority](#certificate-authority)
- [🔄 Config Export/Import](#config-export-import)
- [📊 System Health](#system-health)
- [↻ Updates](#updates)
- [📋 Logs](#logs)

## 📦 Software Depot

- Supports HTTP or HTTPS with optional basic authentication.
- Uses the shared VIS certificate when HTTPS is enabled.
- Provides repository browsing, folder management, delete confirmation, and drag-and-drop file or folder uploads with progress, percentage, and bandwidth.
- Supports both manual VCF binary staging and VCF Download Tool (VCFDT) workflows after VCFDT is installed through the VIS UI.
- Installs the manually downloaded VCFDT tarball from the Software Depot page using drag-and-drop upload.
- Supports Broadcom Activation Code workflows when VCFDT is installed.
- Validates credentials by downloading depot metadata into `/opt/vis/data/depot`.
- Runs long VCF/VVF binary downloads asynchronously with single-job enforcement, status, cancellation, and log visibility.
- Supports Install, Upgrade, and ESX Patch downloads with a validated Activation Code.

## 💾 SFTP Backup

- Creates an SFTP-only local account with configurable username and password.
- Shows copy-ready host, port, user, password, and backup path values.
- Uses a chrooted repository under `/opt/vis/data/sftp`.
- Provides read-oriented backup artifact browsing.
- Health checks validate SSH/SFTP state and repository readiness.

## 🐳 Container Registry

- Uses Harbor as the registry backend.
- Supports configurable administrator credentials and shared VIS TLS.
- Provides endpoint details for browser and CLI access.
- Includes enable, disable, restart, and health check actions.
- Pulls Harbor artifacts during the Packer build according to the configured BOM/version.

## 📇 LDAP Provider

- Uses OpenLDAP as a simple directory service for VCF SSO.
- Supports LDAP and LDAPS using the shared VIS certificate.
- Pre-creates `vcf-admins` and `vcf-users`.
- Manages users, groups, membership, disabled state, and passwords.
- Provides copy-ready VCF SSO values, LDAP filters, attribute mappings, Base DNs, bind DN, and bind password.
- Health checks validate service state and LDAP bind behavior.

## 🔐 OIDC Provider

- Uses Keycloak for OIDC federation with VCF SSO.
- Deploys Keycloak with a default `VCF` realm and shared VIS TLS.
- Pre-creates `vcf-admins` and `vcf-users`.
- Manages OIDC users, groups, membership, disabled state, and passwords.
- Shows the OIDC discovery endpoint.
- Creates VCF SSO OIDC clients, enables client authentication/direct access grants, and displays the generated client secret.
- Includes a VIS-themed Keycloak login page.

## 🌐 DNS Server

- Uses Unbound as the DNS server.
- Configures the DNS domain before records are created.
- Adds each DNS entry as paired forward and reverse records.
- Lists, edits, and deletes records as one logical host/IP row.
- Validates duplicate names, duplicate IP addresses, and domain requirements.
- Renders Unbound configuration and performs health checks against the generated state.

## ⏱ NTP Server

- Provides a local NTP endpoint for VCF appliances, ESX hosts, and supporting lab systems.
- Uses Chrony as the backend service.
- Shows copy-ready NTP FQDN, IP, and port values for VCF configuration.
- Supports an optional local clock fallback for isolated lab environments.
- Includes service enable, disable, restart, and health check actions.

## 🧭 DHCP Server

- Provides appliance-managed DHCP addressing for lab networks that need quick bring-up.
- Uses the appliance interface automatically to keep configuration minimal.
- Configures subnet, range, gateway, DNS servers, domain, and lease timing.
- Supports static reservations for predictable appliance addressing.
- Renders DHCP backend configuration and validates ranges before saving.

## 🔑 Key Management Service

- Provides a KMIP-compatible key management endpoint for VCF encryption workflows.
- Uses PyKMIP with the shared VIS certificate.
- Shows copy-ready KMIP FQDN, IP, port, and trusted root CA values.
- Keeps configuration intentionally small so labs can quickly test key-provider workflows.

## 🛡️ Certificate Authority

- Centralizes trust for TLS-capable VIS services.
- Generates a shared VIS certificate authority and server certificate.
- Exposes the root CA and full PEM bundle for download.
- Lets Software Depot, Container Registry, LDAP/LDAPS, OIDC, and Key Management Service use the shared VIS certificate where appropriate.
- Keeps SFTP separate because SSH host keys and SFTP trust are handled differently.

## 🔄 Config Export/Import

- Captures and reuses VIS service configuration across appliances.
- Exports service settings as JSON for backup, review, or repeatable lab setup.
- Imports JSON profiles from file upload or pasted content.
- Preserves credentials and tokens in the profile, so exported files must be stored securely.
- Optionally applies enabled service backends after import.

## 📊 System Health

- Views appliance capacity and service partition health.
- Shows CPU, memory, and aggregate storage usage.
- Displays each service partition in VMDK order with capacity, used, and available space.
- Provides an Expand Filesystem action after a backing VMDK has been increased.
- Refreshes and grows filesystems online when possible.

## ↻ Updates

- Pulls the latest VIS source from the configured GitHub repository and branch when internet access is available.
- Applies signed offline release ZIP bundles for disconnected appliances.
- Verifies offline updates with the built-in VIS release public key, signed SHA256 manifest, and archive hash before applying changes.
- Applies web application, Python dependency, helper script, and supported systemd unit updates without redeploying the appliance.
- Runs asynchronously from the VIS UI because the web service may restart during the update.
- Shows recent update status and `/opt/vis/state/vis-update.log` output.
- Can also be run directly on the appliance with `sudo vis-update --repo-url <repo> --branch <branch>` or `sudo vis-offline-update --archive <zip> --sha256 <sha256> --signature <sig>`.

## 📋 Logs

- Inspects VIS-managed services from one place.
- Shows VIS web and service logs for depot, SFTP, Harbor, LDAP, OIDC, DNS, NTP, DHCP, KMS, and VCFDT where available.
- Filters by service, line count, search text, and severity.
- Downloads log snippets for troubleshooting.
