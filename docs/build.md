# Build

VIS currently uses Packer's `vmware-iso` workflow against a standalone ESX host. The build exports an OVA into `output-vmware-iso/`.

## Table of Contents

- [Repository Layout](#repository-layout)
- [Requirements](#requirements)
- [Build Artifacts](#build-artifacts)
- [Configure Builder Settings](#configure-builder-settings)

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

- Packer with the VMware builder plugin.
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
4. Ensure the target port group has network access for the temporary build VM.
5. Enable the Packer ESX guest IP helper:

```shell
esxcli system settings advanced set -o /Net/GuestIPHack -i 1
```

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
