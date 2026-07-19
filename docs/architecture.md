# Architecture

VIS is an Ubuntu-based virtual appliance with a web management UI and local service adapters. The UI stores appliance state in SQLite and manages backend services through explicit adapters so services can be configured, enabled, restarted, disabled, and health checked from one control plane.

## Table of Contents

- [Data Layout](#data-layout)
- [State](#state)

```mermaid
flowchart LR
  Admin["VI / VCF administrator"] --> UI["VIS Web UI"]
  UI --> Depot["Software Depot"]
  UI --> SFTP["SFTP Backup"]
  UI --> Registry["Container Registry"]
  UI --> LDAP["LDAP Provider"]
  UI --> OIDC["OIDC Provider"]
  UI --> DNS["DNS Server"]
  UI --> NTP["NTP Server"]
  UI --> DHCP["DHCP Server"]
  UI --> KMS["Key Management Service"]
  UI --> Certs["Shared Certificates"]
  UI --> Health["System Health"]

  Depot --> DepotDisk["/opt/vis/data/depot"]
  SFTP --> SFTPDisk["/opt/vis/data/sftp"]
  Registry --> RegistryDisk["/opt/vis/data/registry"]
  LDAP --> IdentityDisk["/opt/vis/data/identity"]
  OIDC --> IdentityDisk
  DNS --> DNSDisk["/opt/vis/data/dns"]
  NTP --> TimeData["/opt/vis/data/time"]
  DHCP --> DHCPData["/opt/vis/data/dhcp"]
  KMS --> KMSData["/opt/vis/data/kms"]
```

## Data Layout

The default service disks are:

| Disk | Mount | Size |
| --- | --- | --- |
| Disk 1 | OS | 40 GB |
| Disk 2 | `/opt/vis/data/depot` | 200 GB |
| Disk 3 | `/opt/vis/data/sftp` | 15 GB |
| Disk 4 | `/opt/vis/data/registry` | 60 GB |
| Disk 5 | `/opt/vis/data/dns` | 2 GB |
| Disk 6 | `/opt/vis/data/identity` | 2 GB |

Each service data path is on its own VMDK so a busy depot or registry can be expanded without consuming space from another VIS service.

By default VIS stores application state in:

```text
/opt/vis/state/vis.db
```

Set `VIS_DB_PATH` for local development and tests.
