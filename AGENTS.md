# VIS Agent Guidelines

Read this before adding or changing VIS features. VIS is an appliance control plane, so every UI change usually has backend, Packer, tests, and live-appliance implications.

## Product Shape

- VIS is the VCF Infrastructure Services Appliance for lab and POC environments.
- The web app is a Flask application under `vis/`.
- Persistent application state is SQLite through `vis/store.py`.
- Service defaults are seeded from `vis/definitions.py`.
- Service runtime behavior belongs in adapters in `vis/manager.py`.
- Packer/appliance setup is under `scripts/` and `files/`.

## Service Model

Every managed service should have:

- a stable `id`
- user-facing `name` and `description`
- `enabled`, `configured`, and `health_status`
- `endpoint`
- `filesystem_root`
- protocol, port, and path settings
- validation and health-check state

New services must be added to:

- `vis/definitions.py`
- service ordering in `vis/store.py`
- `ServiceManager.adapter_for()` in `vis/manager.py`
- `vis/templates/service_detail.html`
- `vis/templates/icons.html`
- `_service_endpoint()` in `vis/web.py`
- `_log_targets()` in `vis/web.py` when a real backend has systemd units
- tests in `tests/test_services.py`

## Backend Adapters

- Real backends should implement a `ServiceAdapter`, not direct logic in routes.
- Adapters must implement `validate`, `enable`, `disable`, `restart`, `health_check`, and `render_config`.
- Routes should parse and validate form input, update service settings, save through `ServiceStore`, then call `refresh_service_backend(...)`.
- `render_config()` should show the effective generated backend config.
- `health_check()` should set `configured` from validation and use `disabled`, `needs_configuration`, `starting`, `stopped`, or `healthy` consistently.
- Long-running or slow-starting services should use the existing starting-state monitor path in `ServiceManager`.
- Do not mutate real system services from a route; go through an adapter.

## UI Conventions

- Follow the UI conventions in [docs/develop.md](docs/develop.md).
- Keep required setup notices local to the panel that blocks enablement.
- Keep success and error messages local, dismissible, and visibly colored.
- Use existing VIS components:
  - `credential-form compact` for compact credentials
  - `service-config-form` for wider service settings
  - `toggle-row` for checkboxes
  - `segmented-choice` for radio choices
  - labeled copy rows for VCF/client values
- TLS-capable services must use the wording `Expose using shared VIS certificate`.
- If TLS is mandatory, show that same checkbox checked and disabled.
- Avoid creating one-off UI components when an existing VIS component expresses the same concept.

## Certificates

- Shared service TLS is managed by the Certificate Authority page.
- TLS-capable services should use shared cert paths:
  - `/opt/vis/config/tls/rootCA.pem`
  - `/opt/vis/config/tls/server.crt`
  - `/opt/vis/config/tls/server.key`
  - `/opt/vis/config/tls/vis-full.pem`
- Add TLS-capable services to `TLS_SERVICE_IDS` in `vis/web.py`.
- Do not expose certificate file paths as primary service settings unless the user is explicitly managing certificates.

## Packer And Appliance Setup

- Packer must stay in sync with live-system features.
- If a feature needs packages, Python dependencies, directories, systemd defaults, or first-boot behavior, update the Packer scripts too.
- App Python dependencies belong in `vis/requirements.txt`; `scripts/vis-services.sh` installs them into `/opt/vis/app/venv`.
- OS packages belong in `scripts/vis-settings.sh`.
- Service data directories should be created in both Packer setup and first-boot scripts when applicable.
- Appliance update behavior belongs in `scripts/vis-update.sh` and `scripts/vis-apply-update.sh`; keep those hooks current when new files, units, or dependency steps must be applied to already deployed appliances.
- Do not rebuild the appliance unless explicitly requested.

## Live Appliance Patching

- The current live appliance is usually patched over SSH for quick validation.
- Keep local repo changes as the source of truth, then copy them to the appliance.
- Restart only the necessary service, usually `vis-web.service` for web-app changes.
- Do not interrupt active user workflows such as Software Depot downloads unless explicitly asked.
- After live patching, verify the affected page or backend on the appliance.

## Storage

- Service data roots should stay under `/opt/vis/data/<service>`.
- Do not hardcode detected disk capacity in the UI.
- System Health should derive storage size and mount details from the running system.
- Filesystem expansion should rescan and grow to the detected device capacity.

## Security And Secrets

- Do not introduce default service passwords.
- Service credentials should be configured by the user before enablement.
- Sensitive values should be masked in the UI with an eye toggle and copy button where useful.
- Config export/import may include secrets; warning text must remain clear.
- Do not commit SSH keys, ISO files, VCF Download Tool archives, generated OVAs, VMDKs, split artifacts, or temporary verification files.

## Testing

Before finishing a feature, add or update tests for:

- service seed/model behavior
- route validation and redirect anchors
- service detail rendering
- adapter config generation and unit control
- Packer dependency or directory changes
- logs mapping for real systemd-backed services

Run focused tests while developing, then run:

```bash
python3 -m unittest tests.test_services tests.test_packer_config
```

## Documentation

- Update `README.md` when user-facing behavior, build requirements, or feature capabilities change.
- Update `docs/develop.md` when a new UI pattern or contributor convention is intentionally introduced.
- Regenerate static docs with `python3 docs/render_docs.py` when Markdown docs change.
- `docs/*.html` files are generated but intentionally committed because GitHub Pages is published from the `/docs` folder.
- Keep `docs/index.html`, generated docs HTML, `docs/docs.css`, and `docs/images/` publishable.
- Keep local-only artifacts such as `prototype/`, `output-vmware-iso/`, ISOs, OVAs, VMDKs, split OVA parts, VCF Download Tool archives, and `.tmp*` files out of commits.
- Keep this file updated when a repeated implementation rule appears during development.
