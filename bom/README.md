# VIS Build BOM

`vis-bom.json` is a Packer var file for appliance component and service defaults.

Use it with the normal build vars:

```bash
packer build -var-file=bom/vis-bom.json -var-file=local.pkrvars.json vis.json
```

Current BOM-controlled values:

- `harbor_version`: Harbor release tag used for the online installer, for example `v2.15.1`
- `harbor_port`: HTTPS port exposed by Harbor, default `9443`
- `harbor_http_port`: internal/non-TLS Harbor port, default `9080`
- `harbor_admin_password`: optional initial Harbor admin password; leave blank to configure Container Registry from the VIS UI
- `vis_appliance_fqdn`: appliance FQDN used by VIS and generated certs
- `vis_appliance_ip`: appliance IP used by VIS and generated certs
- `vis_admin_username`: initial VIS application administrator username
- `vis_admin_password`: initial VIS application administrator password
- `vis_sftp_user`: optional initial SFTP backup username; leave blank to configure SFTP Backup from the VIS UI
- `vis_sftp_password`: optional initial SFTP backup user password; leave blank to configure SFTP Backup from the VIS UI
- `install_vcf_download_tool`: optional build-time VCFDT install; default `false` because VCFDT can be uploaded from the VIS Software Depot UI after deployment
- service disk sizes in MiB

The Harbor provisioner downloads the online installer, renders a temporary
build-time configuration, and pre-pulls the Harbor container images so Container
Registry can start from the VIS UI without a first-use image fetch:

```text
https://github.com/goharbor/harbor/releases/download/${harbor_version}/harbor-online-installer-${harbor_version}.tgz
```

If `harbor_admin_password` is blank, the service remains disabled after the
build and VIS prompts for credentials before enabling Container Registry.

For air-gapped builds, the next step should be adding a `harbor_installer_source`
variable that points to a pre-staged installer artifact instead of GitHub.
