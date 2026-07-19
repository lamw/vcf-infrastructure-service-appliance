# Develop

This page is for contributors who want to run VIS locally, make changes, verify behavior, and keep new features consistent with the appliance.

## Table of Contents

- [Contributing to VIS](#contributing-to-vis)
- [What You Need](#what-you-need)
- [Repository Layout](#repository-layout)
- [Run the Web App Locally](#run-the-web-app-locally)
- [Run Tests](#run-tests)
- [Preview Documentation](#preview-documentation)
- [State and Configuration](#state-and-configuration)
- [Common Development Workflow](#common-development-workflow)
- [UI Conventions](#ui-conventions)
- [AI-Assisted Development](#ai-assisted-development)

## Contributing to VIS

VIS is meant to be practical infrastructure for VCF lab and proof-of-concept users, so contributions should favor reliability, clear workflows, and predictable appliance behavior over broad abstractions.

Good contributions usually follow these guidelines:

- Start with a focused issue, enhancement, or bug fix. Keep each pull request scoped to one service, one workflow, or one documentation improvement when possible.
- Inspect existing service patterns before adding a new one. Most service work touches the Flask route, service definition, adapter, template, tests, Packer setup, and documentation.
- Prefer small, reviewable commits with clear messages that explain the user-facing behavior or bug being fixed.
- Keep the UI consistent with the established VIS pages. Avoid one-off controls when an existing form, toggle, badge, notice, or copy-row pattern already exists.
- Treat appliance changes as product changes. If a feature requires packages, systemd units, directories, first-boot setup, firewall behavior, or generated configuration, update the Packer scripts as part of the same change.
- Add tests for behavior that can regress: service seeding, route validation, adapter output, rendered controls, required-configuration gates, logs, and Packer expectations.
- Do not include private artifacts, credentials, SSH keys, ISO files, VCF Download Tool archives, generated OVAs, or split release files in commits.
- Update documentation when behavior changes. A contributor should be able to understand how to build, deploy, configure, or test the feature without reading the implementation first.
- Validate locally before submitting. At minimum, run focused tests for the area changed and regenerate docs when Markdown changes.

For larger changes, open the design in a draft pull request or discussion first. This is especially useful for new appliance services, storage layout changes, networking behavior, authentication flows, or anything that changes how VIS integrates with VCF.

## What You Need

For local web application development:

- Python 3.10 or newer.
- `pip` and `venv`.
- A shell environment on macOS, Linux, or another Unix-like workstation.
- The VIS repository checked out locally.

For appliance or Packer development, also review the [Build](build.html) page because those workflows require Packer, OVF Tool, an Ubuntu ISO, and access to a standalone ESX build host.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `vis/` | Flask application, service adapters, templates, and static UI assets |
| `tests/` | Unit tests for service models, adapters, routes, and packaging expectations |
| `docs/` | Markdown and generated HTML documentation |
| `scripts/` | Packer provisioning and deployment helper scripts |
| `files/` | Appliance setup payloads copied into the VM |
| `vis.json` | Packer template |
| `vis-version.json` | Appliance version, default VM sizing, disk layout, and OVF defaults |
| `vis-builder.json` | Local ESX build-host settings |

## Run the Web App Locally

Create a local virtual environment:

```shell
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r vis/requirements.txt
```

Start VIS with a local SQLite database:

```shell
VIS_DB_PATH=/tmp/vis-dev.db VIS_PORT=8080 python -m vis.app
```

Then open:

```text
http://localhost:8080
```

Use a different `VIS_PORT` if port `8080` is already in use. Local development does not require real system services unless you are specifically testing appliance adapters.

## Run Tests

Run the full test suite:

```shell
python3 -m unittest discover
```

Run a focused test file:

```shell
python3 -m unittest tests.test_services
```

Run a single test when iterating on a specific behavior:

```shell
python3 -m unittest tests.test_services.WebAppTest.test_dns_config_route_sets_required_domain
```

## Preview Documentation

After changing Markdown docs, regenerate the static HTML:

```shell
python3 docs/render_docs.py
```

Preview the docs locally:

```shell
cd docs
python3 -m http.server 8094
```

Then open:

```text
http://localhost:8094/
```

## State and Configuration

On an appliance, VIS stores application state in:

```text
/opt/vis/state/vis.db
```

For local development, always set `VIS_DB_PATH` to a temporary path so local experiments do not touch appliance state:

```shell
VIS_DB_PATH=/tmp/vis-dev.db
```

Generated service configuration normally lives under `/opt/vis/config`, and service data normally lives under `/opt/vis/data`. Local development can render and test most behavior without mutating those paths.

## Common Development Workflow

1. Inspect the existing service pattern before adding a new feature.
2. Make the smallest code and UI change that supports the requested behavior.
3. Add or update focused tests for the service adapter, route, or rendered page.
4. Run the focused tests first.
5. Run the broader test suite before handing off the change.
6. Regenerate docs when Markdown changes.
7. If the change affects appliance behavior, make sure the Packer scripts or first-boot setup include the same behavior.

## UI Conventions

These conventions keep new VIS services visually consistent with the original service pages.

### Detail Page Structure

- Use the shared service detail shell: heading, action buttons, status metrics, then a `Configuration` panel.
- Keep top-level status badges/metric panels on one row on desktop when there are four or fewer. Use a page-specific grid class when a non-service page needs four metrics, and let the existing responsive rules wrap them on smaller screens.
- Put required setup in the smallest panel that actually blocks `Enable Service`.
- Use `required_config_notice(...)` only inside the panel that needs user action.
- Use local dismissible notices in the panel where the action occurred.

### Configuration Forms

- Use `credential-form compact` for compact credential-oriented settings.
- Use `service-config-form` for infrastructure settings that need wider fields, text areas, or multiple related controls.
- Use `dns-entry-form` only for DNS record/domain workflows.
- Keep save buttons aligned under the editable field column.

### TLS

- TLS-capable services must use the established checkbox row:
  `Expose using shared VIS certificate`.
- If TLS is required and not optional, show the same checkbox checked and disabled rather than inventing a new read-only visual.
- Do not expose certificate file paths in the primary service configuration panel. Show certificate details in generated config or the Certificate Authority page.

### Binary Choices

- Use `toggle-row` for checkboxes.
- Use `segmented-choice` for radio-button choices.
- Avoid mixing ad hoc checkbox containers with established VIS controls.

### Client Values

- Use labeled, copyable rows for values users paste into VCF or other clients.
- Labels should say what the value is, for example `NTP Server FQDN`, `DHCP Server IP`, or `KMIP Port`.
- Keep generated server configuration below client values in a `stacked-config` block.

### Service Backends

- Real service adapters should render the effective backend config.
- Mock or placeholder services should not expose fake controls that look functional.
- Logs should map each real backend service to its systemd unit in `_log_targets`.

## AI-Assisted Development

VIS has repository-level guidance for AI coding agents in [AGENTS.md](https://github.com/lamw/vcf-infrastructure-service-appliance/blob/main/AGENTS.md).

Before using an AI assistant to add or modify VIS features:

- Share the relevant goal and ask the agent to inspect existing service patterns before changing code.
- Ensure new service UI follows the UI conventions on this page.
- Keep backend behavior wired to real adapters and tests whenever a control looks functional.
- Preserve Packer/appliance conventions when adding dependencies, services, or first-boot behavior.
- Ask the agent to run targeted tests and summarize what was changed before handing work back.
