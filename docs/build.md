# Build

VIS currently uses Packer's `vmware-iso` workflow against a standalone ESX host. The build exports an OVA into `output-vmware-iso/`.

The v1.0.5 appliance includes Outbound Proxy configuration for VCFDT downloads and online updates. New builds start with the proxy disabled; users configure it after deployment. Packer creates the protected proxy configuration directory, while updates preserve saved settings.

## Table of Contents

- [Repository Layout](#repository-layout)
- [Requirements](#requirements)
- [Build Artifacts](#build-artifacts)
- [Signed Offline Update Releases](#signed-offline-update-releases)
- [Configure Builder Settings](#configure-builder-settings)
- [Security Updates](#security-updates)

## Repository Layout

| Path | Purpose |
| --- | --- |
| `vis/` | Flask web application and VIS service control plane |
| `vis.json` | Packer `vmware-iso` template |
| `vis-builder.json` | Local ESX builder settings |
| `vis-version.json` | Appliance version, ISO path, disk sizes, and guest defaults |
| `artifacts/` | User-supplied build artifacts such as the Ubuntu ISO and optional VCFDT archive |
| `files/` | Appliance first-boot/setup payloads copied into the VM |
| `scripts/` | Packer provisioning scripts |
| `http/` | Ubuntu autoinstall NoCloud seed data |
| `tests/` | Unit tests for the VIS control plane |
| `prototype/` | Static HTML prototypes used during UI design |

## Requirements

### Build Workstation

- Packer with VMware builder plugin 1.2.0 for standalone ESX.
- `ovftool`.
- Network access to the ESX build host.
- SSH access to the ESX build host.
- Enough local disk space for the Ubuntu ISO, Packer cache, and exported OVA.

HashiCorp documents that the VMware builder communicates with ESX over SSH and, for vSphere Hypervisor/ESX usage, may require `GuestIPHack` and firewall allowances for the remote build workflow. See the official [Packer VMware builder documentation](https://developer.hashicorp.com/packer/integrations/hashicorp/vmware/latest/components/builder/vmx).

### Standalone ESX Build Host

On the standalone ESX host:

1. Enable SSH.
2. Ensure the build workstation can reach the ESX management IP.
3. Ensure the target datastore has enough free space.
4. Ensure the target port group provides IPv4 DHCP for the temporary build VM and can reach the Packer workstation's autoinstall HTTP server.
5. Enable the Packer ESX guest IP helper:

```shell
esxcli system settings advanced set -o /Net/GuestIPHack -i 1
```

The live Ubuntu installer keeps SSH closed so Packer waits for the installed
system's SSH endpoint after reboot. The installer and the installed system can
receive different DHCP addresses.

## Build Artifacts

Place user-supplied build artifacts in `artifacts/`.

Required:

```text
artifacts/ubuntu-26.04-live-server-amd64.iso
```

Optional:

```text
artifacts/vcf-download-tool-*.tar.gz
```

VCFDT is an optional manual Broadcom download. The default public-style build does **not** bake VCFDT into the appliance. After deployment, install it from the Software Depot page by dragging `vcf-download-tool-*.tar.gz` into the VCF Download Tool panel.

For private lab builds, you can still preinstall VCFDT by placing `artifacts/vcf-download-tool-*.tar.gz` in `artifacts/` and setting:

```json
"install_vcf_download_tool": "true"
```

When enabled, Packer installs VCFDT under `/usr/local/lib/vcf-download-tool` and links `vcf-download-tool` into `/usr/local/bin`.

`files/` is reserved for appliance setup payloads. Do not place large user downloads there.

## Signed Offline Update Releases

Offline VIS updates use a signed SHA256 manifest so disconnected appliances can reject tampered or corrupted release archives. The private signing key must never be committed to the repository or copied into the appliance. Only the public key in `files/vis-update-signing.pub` is shipped with VIS.

To create an offline update bundle from a release checkout:

```shell
VERSION=$(jq -r .version vis-version.json)
BUNDLE="vis-update-${VERSION}.zip"

git archive --format=zip --output="${BUNDLE}" --prefix="vis-update-${VERSION}/" HEAD
shasum -a 256 "${BUNDLE}" > "${BUNDLE}.sha256"
openssl pkeyutl -sign -rawin   -inkey ~/.vis-signing/vis-update-signing-private.pem   -in "${BUNDLE}.sha256"   -out "${BUNDLE}.sha256.sig"
```

Publish all three files in the GitHub release:

```text
vis-update-<version>.zip
vis-update-<version>.zip.sha256
vis-update-<version>.zip.sha256.sig
```

The appliance verifies the `.sha256.sig` file with `/etc/vis/update-signing.pub`, verifies the ZIP hash from the signed SHA256 file, rejects unsafe ZIP paths or unsupported file types, and then applies the staged source with `/usr/local/sbin/vis-apply-update`.

## Configure Builder Settings

Edit `vis-builder.json` for your ESX build host:

```json
{
  "builder_host": "192.168.30.62",
  "builder_host_username": "root",
  "builder_host_password": "VMware1!",
  "builder_host_datastore": "datastore1",
  "builder_host_portgroup": "VM Network"
}
```

Edit `vis-version.json` if you need to adjust appliance versioning, ISO checksum, disk sizes, CPU, memory, guest defaults, or the Docker pod CIDR.

Validate the template:

```shell
packer validate -var-file=vis-builder.json -var-file=vis-version.json vis.json
```

Build the appliance:

```shell
./build.sh
```

The OVA is packaged with uncompressed stream-optimized VMDKs for broad vSphere UI import compatibility; release files can be split afterward for GitHub distribution.

## Security Updates

Packer refreshes the Ubuntu package indexes and applies available package
updates, including kernel upgrades that need newly named image and modules
packages. Boot the resulting appliance and check `uname -r` against the
installed Ubuntu kernel packages.

For an existing appliance, run `sudo apt-get update` and
`sudo apt-get --with-new-pkgs upgrade` during a maintenance window. If the
kernel is updated, reboot and verify the running kernel with `uname -r`.

After updating the VIS application on an existing appliance, set the OIDC
Provider image to `quay.io/keycloak/keycloak:26.8.2` and restart the provider
through VIS to apply the generated JVM options. The adapter disables Base64
intrinsics as an interim mitigation for CVE-2026-114938. Verify the running
JVM reports `UseBASE64Intrinsics = false`.
[Keycloak advisory](https://www.keycloak.org/2026/11/java4shell-vulnerability-cve-2026-114938)

New builds stage Harbor v2.15.5 with private network webhook targets blocked.
For an existing registry, back up its configuration and data and follow the
Harbor upgrade procedure. Set `network.allow_private_network_access` to
`false` in `harbor.yml` before regenerating the deployment configuration.
[Harbor upgrade guide](https://goharbor.io/docs/2.15.0/administration/upgrade/)
[Harbor webhook advisory](https://github.com/goharbor/harbor/security/advisories/GHSA-2phj-cp9f-qq6w)

Track rollout and verification in
[issue #36](https://github.com/lamw/vcf-infrastructure-service-appliance/issues/36).
