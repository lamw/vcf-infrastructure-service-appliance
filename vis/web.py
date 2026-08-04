import errno
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, Response, abort, current_app, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .definitions import DEFAULT_IDENTITY_GROUPS
from .file_manager import RepositoryFileManager
from .manager import ServiceManager
from .models import ValidationResult, utc_now
from .store import ServiceStore


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(config or {})
    app.secret_key = app.config.get("SECRET_KEY") or os.environ.get("VIS_SECRET_KEY") or "vis-development-secret"
    app.config["VIS_APPLIANCE_FQDN"] = os.environ.get("VIS_APPLIANCE_FQDN", "vis.williamlam.local")
    app.config["VIS_APPLIANCE_IP"] = os.environ.get("VIS_APPLIANCE_IP", "") or _detect_primary_ip()
    app.config["VIS_LOCAL_ADAPTERS_ENABLED"] = os.environ.get("VIS_ENABLE_LOCAL_ADAPTERS") == "1"
    app.config["VIS_AUTH_REQUIRED"] = app.config.get("VIS_AUTH_REQUIRED", not app.config.get("TESTING", False))

    db_path = app.config.get("VIS_DB_PATH") or os.environ.get("VIS_DB_PATH")
    store = ServiceStore(db_path)
    store.initialize()
    admin_username = app.config.get("VIS_ADMIN_USERNAME") or os.environ.get("VIS_ADMIN_USERNAME", "admin")
    admin_password = app.config.get("VIS_ADMIN_PASSWORD") or os.environ.get("VIS_ADMIN_PASSWORD", "")
    store.ensure_initial_admin(admin_username, generate_password_hash(admin_password) if admin_password else "")
    manager = ServiceManager(store)
    if os.environ.get("VIS_REFRESH_ON_STARTUP") == "1":
        for service in manager.list_services():
            manager.run_health_check(service.id)
    app.config["service_manager"] = manager

    def refresh_service_backend(service_id):
        service = manager.get_service(service_id)
        if app.config["VIS_LOCAL_ADAPTERS_ENABLED"] and service.enabled:
            return manager.restart_service(service_id)
        return manager.run_health_check(service_id)

    @app.before_request
    def require_login():
        if not app.config["VIS_AUTH_REQUIRED"]:
            return None
        if request.endpoint in ("login", "static"):
            return None
        if session.get("user_id"):
            return None
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not app.config["VIS_AUTH_REQUIRED"]:
            return redirect(url_for("home"))
        error = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = store.get_user_by_username(username)
            if user and user["active"] and check_password_hash(user["password_hash"], password):
                session.clear()
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                store.update_user_login_time(user["id"])
                return redirect(request.form.get("next") or url_for("home"))
            error = "Invalid username or password"
        return render_template("login.html", error=error, next_url=request.args.get("next", ""))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def home():
        _normalize_service_endpoints(manager, store, app.config["VIS_APPLIANCE_FQDN"])
        services = manager.list_services()
        return render_template("home.html", services=services, summary=manager.service_summary())

    @app.route("/system-health")
    def system_health():
        expand_status = request.args.get("expand_status", "")
        expand_message = request.args.get("expand_message", "")
        return render_template(
            "system_health.html",
            health=_system_health(manager),
            expand_status=expand_status,
            expand_message=expand_message,
        )

    @app.route("/system-health/storage/<service_id>/expand", methods=["POST"])
    def storage_expand(service_id):
        health = _system_health(manager)
        partition = next((item for item in health["storage"] if item["id"] == service_id), None)
        if not partition:
            abort(404)
        try:
            result = _expand_filesystem(partition["mount"])
        except OSError as err:
            result = {"ok": False, "message": str(err)}
        return redirect(
            url_for(
                "system_health",
                expand_status="ok" if result["ok"] else "error",
                expand_message=result["message"],
            )
        )

    @app.route("/logs")
    def logs():
        log_targets = _log_targets(manager)
        selected_id = request.args.get("service", "vis-web")
        if selected_id not in log_targets:
            selected_id = "vis-web"
        try:
            lines = int(request.args.get("lines", "200"))
        except ValueError:
            lines = 200
        lines = max(50, min(lines, 1000))
        selected = log_targets[selected_id]
        query = request.args.get("q", "").strip()
        level = request.args.get("level", "all").strip().lower()
        if level not in ("all", "error", "warning", "info"):
            level = "all"
        log_text = _filter_log_text(_read_logs(selected, lines), query, level)
        return render_template(
            "logs.html",
            log_targets=log_targets,
            selected_id=selected_id,
            selected=selected,
            lines=lines,
            log_text=log_text,
            query=query,
            level=level,
        )

    @app.route("/logs/download")
    def logs_download():
        log_targets = _log_targets(manager)
        selected_id = request.args.get("service", "vis-web")
        if selected_id not in log_targets:
            selected_id = "vis-web"
        try:
            lines = int(request.args.get("lines", "1000"))
        except ValueError:
            lines = 1000
        lines = max(50, min(lines, 5000))
        query = request.args.get("q", "").strip()
        level = request.args.get("level", "all").strip().lower()
        if level not in ("all", "error", "warning", "info"):
            level = "all"
        selected = log_targets[selected_id]
        log_text = _filter_log_text(_read_logs(selected, lines), query, level)
        filename = "vis-{}-logs.txt".format(selected_id)
        return Response(
            log_text + ("\n" if log_text else ""),
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename={}".format(filename)},
        )

    @app.route("/certificates")
    def certificates():
        _normalize_service_endpoints(manager, store, app.config["VIS_APPLIANCE_FQDN"])
        return render_template(
            "certificates.html",
            certificate=_certificate_status(),
            issued_certificates=_issued_certificates(),
            tls_services=_tls_service_status(manager),
        )

    @app.route("/users")
    def users():
        return render_template("users.html", users=store.list_users())

    @app.route("/config")
    def config_profiles():
        return render_template("config_profiles.html")

    @app.route("/updates")
    def updates():
        return render_template(
            "updates.html",
            update_status=_read_update_status(app),
            update_log=_read_update_log(app),
            default_repo_url=app.config.get("VIS_UPDATE_REPO_URL")
            or os.environ.get("VIS_UPDATE_REPO_URL", "https://github.com/lamw/vcf-infrastructure-service-appliance.git"),
            default_branch=app.config.get("VIS_UPDATE_BRANCH") or os.environ.get("VIS_UPDATE_BRANCH", "main"),
        )

    @app.route("/updates/run", methods=["POST"])
    def updates_run():
        repo_url = request.form.get("repo_url", "").strip()
        branch = request.form.get("branch", "").strip() or "main"
        if not _valid_update_repo_url(repo_url):
            return redirect(url_for("updates", update_error="Repository URL must be an HTTPS GitHub URL, SSH Git URL, or local file path."))
        if not _valid_git_ref(branch):
            return redirect(url_for("updates", update_error="Branch must use letters, numbers, dot, dash, underscore, or slash."))
        status = _read_update_status(app)
        if status.get("state") == "running":
            return redirect(url_for("updates", update_warning="VIS update is already running."))
        try:
            _start_update(app, repo_url, branch)
        except OSError as err:
            return redirect(url_for("updates", update_error=str(err)))
        return redirect(url_for("updates", update_status_message="VIS update started. The web UI may briefly restart when the update is applied."))

    @app.route("/updates/offline", methods=["POST"])
    def updates_offline():
        status = _read_update_status(app)
        if status.get("state") == "running":
            return redirect(url_for("updates", update_warning="VIS update is already running."))
        archive = request.files.get("archive_file")
        checksum = request.files.get("checksum_file")
        signature = request.files.get("signature_file")
        if not archive or not archive.filename:
            return redirect(url_for("updates", update_error="Offline update requires a VIS release ZIP archive."))
        if not checksum or not checksum.filename:
            return redirect(url_for("updates", update_error="Offline update requires the release SHA256 file."))
        if not signature or not signature.filename:
            return redirect(url_for("updates", update_error="Offline update requires the release signature file."))
        try:
            archive_path, checksum_path, signature_path = _stage_offline_update_uploads(app, archive, checksum, signature)
            _start_offline_update(app, archive_path, checksum_path, signature_path)
        except OSError as err:
            return redirect(url_for("updates", update_error=str(err)))
        return redirect(url_for("updates", update_status_message="Signed offline VIS update started. The web UI may briefly restart when the update is applied."))

    @app.route("/config/export")
    def config_export():
        profile = _export_config_profile(manager, app.config["VIS_APPLIANCE_FQDN"], app.config["VIS_APPLIANCE_IP"])
        return Response(
            json.dumps(profile, indent=2, sort_keys=True) + "\n",
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=vis-config-profile.json"},
        )

    @app.route("/config/import", methods=["POST"])
    def config_import():
        raw_profile = request.form.get("profile_json", "").strip()
        upload = request.files.get("profile_file")
        if upload and upload.filename:
            try:
                raw_profile = upload.read().decode("utf-8")
            except UnicodeDecodeError:
                return redirect(url_for("config_profiles", config_error="Uploaded profile must be UTF-8 JSON."))
        if not raw_profile:
            return redirect(url_for("config_profiles", config_error="Select a JSON profile or paste profile JSON."))
        try:
            profile = json.loads(raw_profile)
            imported = _import_config_profile(store, manager, profile, app.config["VIS_APPLIANCE_FQDN"])
        except (TypeError, ValueError, KeyError) as err:
            return redirect(url_for("config_profiles", config_error=str(err)))
        if request.form.get("apply_backends") == "on":
            for service_id in imported:
                service = manager.get_service(service_id)
                try:
                    if service.enabled:
                        manager.restart_service(service_id)
                    else:
                        manager.run_health_check(service_id)
                except Exception as err:  # pragma: no cover - backend failures are surfaced to the user.
                    return redirect(url_for("config_profiles", config_error="{} import applied, but backend refresh failed: {}".format(service.name, err)))
        message = "Imported {} service configuration{}.".format(len(imported), "" if len(imported) == 1 else "s")
        return redirect(url_for("config_profiles", config_status=message))

    @app.route("/users", methods=["POST"])
    def users_create():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not _valid_app_username(username):
            return redirect(url_for("users", user_error="Username must be 3-32 characters using letters, numbers, dash, or underscore"))
        if not password:
            return redirect(url_for("users", user_error="Password is required"))
        if password != confirm:
            return redirect(url_for("users", user_error="Passwords do not match"))
        if store.get_user_by_username(username):
            return redirect(url_for("users", user_error="Username already exists"))
        store.save_user(username, generate_password_hash(password))
        return redirect(url_for("users", user_status="User created"))

    @app.route("/users/<int:user_id>/password", methods=["POST"])
    def users_password(user_id):
        user = store.get_user(user_id)
        if not user:
            abort(404)
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not password:
            return redirect(url_for("users", user_error="Password is required"))
        if password != confirm:
            return redirect(url_for("users", user_error="Passwords do not match"))
        store.update_user_password(user_id, generate_password_hash(password))
        return redirect(url_for("users", user_status="Password updated"))

    @app.route("/certificates/generate", methods=["POST"])
    def certificates_generate():
        try:
            _generate_shared_tls(app.config["VIS_APPLIANCE_FQDN"], app.config["VIS_APPLIANCE_IP"])
            _apply_shared_tls_to_services(manager, store)
            return redirect(url_for("certificates", cert_status="Shared VIS certificate generated"))
        except OSError as err:
            return redirect(url_for("certificates", cert_error=str(err)))

    @app.route("/certificates/upload", methods=["POST"])
    def certificates_upload():
        cert = request.files.get("cert")
        key = request.files.get("key")
        ca = request.files.get("ca")
        if not cert or not key:
            return redirect(url_for("certificates", cert_error="Certificate and private key are required"))
        try:
            _upload_shared_tls(cert, key, ca)
            _apply_shared_tls_to_services(manager, store)
            return redirect(url_for("certificates", cert_status="Shared VIS certificate uploaded"))
        except OSError as err:
            return redirect(url_for("certificates", cert_error=str(err)))

    @app.route("/certificates/download/root-ca")
    def certificates_download_root_ca():
        ca_pem = _shared_tls_paths()["ca_pem"]
        if not ca_pem.is_file():
            abort(404)
        return send_file(str(ca_pem), as_attachment=True, download_name="vis-rootCA.pem")

    @app.route("/certificates/download/full-pem")
    def certificates_download_full_pem():
        full_pem = _shared_tls_paths()["full_pem"]
        if not full_pem.is_file():
            abort(404)
        return send_file(str(full_pem), as_attachment=True, download_name="vis-full.pem")

    @app.route("/certificates/issued", methods=["POST"])
    def certificates_issued_create():
        name = request.form.get("name", "").strip()
        common_name = request.form.get("common_name", "").strip()
        san_dns = request.form.get("san_dns", "")
        san_ips = request.form.get("san_ips", "")
        try:
            days = int(request.form.get("days", "825"))
        except (TypeError, ValueError):
            return redirect(url_for("certificates", cert_error="Validity days must be a whole number", _anchor="issued-certificates"))
        try:
            cert = _issue_certificate(name, common_name, san_dns, san_ips, days)
            return redirect(url_for("certificates", cert_status="Certificate {} issued".format(cert["name"]), _anchor="issued-certificates"))
        except OSError as err:
            return redirect(url_for("certificates", cert_error=str(err), _anchor="issued-certificates"))

    @app.route("/certificates/issued/<name>/delete", methods=["POST"])
    def certificates_issued_delete(name):
        try:
            cert_dir = _issued_certificate_dir(name)
        except OSError as err:
            return redirect(url_for("certificates", cert_error=str(err), _anchor="issued-certificates"))
        if cert_dir.exists():
            shutil.rmtree(str(cert_dir))
        return redirect(url_for("certificates", cert_status="Certificate {} deleted".format(name), _anchor="issued-certificates"))

    @app.route("/certificates/issued/<name>/download/<artifact>")
    def certificates_issued_download(name, artifact):
        try:
            cert_dir = _issued_certificate_dir(name)
        except OSError:
            abort(404)
        artifacts = {
            "certificate": ("certificate.pem", "{}-certificate.pem".format(name)),
            "private-key": ("private-key.pem", "{}-private-key.pem".format(name)),
            "full-chain": ("full-chain.pem", "{}-full-chain.pem".format(name)),
        }
        if artifact not in artifacts:
            abort(404)
        filename, download_name = artifacts[artifact]
        path = cert_dir / filename
        if not path.is_file():
            abort(404)
        return send_file(str(path), as_attachment=True, download_name=download_name)

    @app.route("/services/<service_id>")
    def service_detail(service_id):
        try:
            service = manager.get_service(service_id)
        except KeyError:
            abort(404)
        service_id = service.id
        _ensure_service_endpoint(service, store, app.config["VIS_APPLIANCE_FQDN"])
        vcfdt_system_id = ""
        vcfdt_available = False
        if service_id == "web-depot":
            vcfdt_available = _vcfdt_available()
            if vcfdt_available:
                vcfdt_system_id = _vcfdt_system_id(app)
        adapter = manager.adapter_for(service_id)
        file_browser = None
        file_error = request.args.get("file_error", "")
        if service_id in ("sftp-backup", "web-depot"):
            try:
                file_browser = _file_manager(service).list_dir(request.args.get("path", ""))
            except (FileNotFoundError, NotADirectoryError, ValueError) as err:
                file_error = str(err)
                try:
                    file_browser = _file_manager(service).list_dir("")
                except (FileNotFoundError, NotADirectoryError, ValueError):
                    file_browser = None
        return render_template(
            "service_detail.html",
            service=service,
            rendered_config=adapter.render_config(),
            file_browser=file_browser,
            file_error=file_error,
            dns_entries=_dns_entries(service) if service_id == "unbound-dns" else [],
            directory_users=_directory_items(service, "users") if service.id == "ldap-provider" else [],
            directory_groups=_ensure_identity_groups(service, store) if service.id == "ldap-provider" else [],
            oidc_users=_directory_items(service, "users") if service.id == "oidc-provider" else [],
            oidc_groups=_ensure_identity_groups(service, store) if service.id == "oidc-provider" else [],
            oidc_clients=_oidc_clients(service) if service.id == "oidc-provider" else [],
            oidc_discovery_endpoint=_oidc_discovery_endpoint(service, app.config["VIS_APPLIANCE_FQDN"]) if service.id == "oidc-provider" else "",
            vcfdt_available=vcfdt_available,
            vcfdt_system_id=vcfdt_system_id,
            depot_download_job=_depot_download_job(app) if service_id == "web-depot" else {},
        )

    @app.route("/services/<service_id>/health", methods=["POST"])
    def service_health(service_id):
        try:
            manager.run_health_check(service_id)
        except OSError as err:
            return redirect(url_for("service_detail", service_id=service_id, service_error=str(err)))
        return redirect(url_for("service_detail", service_id=service_id))

    @app.route("/services/<service_id>/restart", methods=["POST"])
    def service_restart(service_id):
        try:
            manager.restart_service(service_id)
        except OSError as err:
            return redirect(url_for("service_detail", service_id=service_id, service_error=str(err)))
        return redirect(url_for("service_detail", service_id=service_id))

    @app.route("/services/<service_id>/toggle", methods=["POST"])
    def service_toggle(service_id):
        service = manager.get_service(service_id)
        try:
            if service.enabled:
                manager.disable_service(service_id)
            else:
                if not service.configured:
                    return redirect(url_for("service_detail", service_id=service_id, config_required="Save required configuration before enabling this service"))
                manager.enable_service(service_id)
        except OSError as err:
            return redirect(url_for("service_detail", service_id=service_id, service_error=str(err)))
        return redirect(url_for("service_detail", service_id=service_id))

    @app.route("/services/web-depot/config", methods=["POST"])
    def depot_config():
        service = manager.get_service("web-depot")
        form_context = request.form.get("form_context") or ("download_config" if "download_mode" in request.form else "client_config")
        tls_enabled = request.form.get("tls_enabled") == "on"
        protocol = "https" if tls_enabled else "http"
        service.settings["protocol"] = protocol
        service.settings["tls_enabled"] = tls_enabled
        service.settings["tls_mode"] = "shared"
        service.settings["port"] = 8443 if protocol == "https" else 8081
        service.settings["path"] = "/"
        if tls_enabled:
            try:
                _ensure_shared_tls(app)
                _apply_shared_tls_settings(service)
            except OSError as err:
                return redirect(url_for("service_detail", service_id="web-depot", client_error=str(err), _anchor="depot-client-config"))
        service.settings["basic_auth_enabled"] = request.form.get("basic_auth_enabled") == "on"
        service.settings["auth_user"] = request.form.get("auth_user", "").strip()
        password = request.form.get("auth_password", "")
        if password:
            service.settings["auth_password"] = password

        if form_context == "download_config":
            download_mode = request.form.get("download_mode", "manual").lower()
            if download_mode not in ("manual", "activation_code"):
                download_mode = "manual"
            download_secret = request.form.get("download_credential", "")
            if download_mode == "manual":
                service.settings["download_mode"] = "manual"
                service.settings["download_token"] = ""
                service.settings["activation_code"] = ""
                service.settings["download_credential_path"] = ""
            else:
                if not _vcfdt_available():
                    return redirect(
                        url_for(
                            "service_detail",
                            service_id="web-depot",
                            download_config_error="VCF Download Tool is not installed. Manually Uploaded is the only supported download mode.",
                            _anchor="download-config",
                        )
                    )
                credential_key = "activation_code"
                if not download_secret:
                    download_secret = str(service.settings.get(credential_key, ""))
                if not download_secret:
                    return redirect(url_for("service_detail", service_id="web-depot", download_config_error="Activation Code is required", _anchor="download-config"))
                try:
                    credential_path = _write_depot_download_credential(app, download_mode, download_secret)
                    verification = _verify_depot_download_credential(download_mode, credential_path, service.filesystem_root)
                except OSError as err:
                    return redirect(url_for("service_detail", service_id="web-depot", download_config_error=str(err), _anchor="download-config"))
                service.settings["download_mode"] = download_mode
                service.settings[credential_key] = download_secret
                service.settings["download_token"] = ""
                service.settings["vcfdt_system_id"] = _vcfdt_system_id(app)
                service.settings["download_credential_path"] = credential_path
                service.settings["last_metadata_download"] = verification
        _update_endpoint(service, app.config["VIS_APPLIANCE_FQDN"])
        service.configured = True
        store.save_service(service)
        manager.restart_service("web-depot")
        if form_context == "download_config":
            return redirect(url_for("service_detail", service_id="web-depot", download_config_status="Configuration updated", _anchor="download-config"))
        return redirect(url_for("service_detail", service_id="web-depot", client_status="Configuration updated", _anchor="depot-client-config"))

    @app.route("/services/web-depot/vcfdt/install", methods=["POST"])
    def depot_vcfdt_install():
        archive = request.files.get("vcfdt_archive")
        if archive is None or not archive.filename:
            return redirect(url_for("service_detail", service_id="web-depot", vcfdt_error="Select a vcf-download-tool tarball to install", _anchor="vcfdt-install"))
        try:
            system_id = _install_vcfdt_archive(app, archive)
        except OSError as err:
            return redirect(url_for("service_detail", service_id="web-depot", vcfdt_error=str(err), _anchor="vcfdt-install"))
        service = manager.get_service("web-depot")
        service.settings["vcfdt_system_id"] = system_id
        store.save_service(service)
        return redirect(url_for("service_detail", service_id="web-depot", vcfdt_status="VCF Download Tool installed", _anchor="vcfdt-install"))

    @app.route("/services/web-depot/download", methods=["POST"])
    def depot_download_start():
        service = manager.get_service("web-depot")
        try:
            _start_depot_binary_download(app, service, request.form)
            return redirect(url_for("service_detail", service_id="web-depot", binary_download_status="Software depot download started", _anchor="binary-download"))
        except OSError as err:
            return redirect(url_for("service_detail", service_id="web-depot", binary_download_error=str(err), _anchor="binary-download"))

    @app.route("/services/web-depot/download/cancel", methods=["POST"])
    def depot_download_cancel():
        try:
            state = _cancel_depot_binary_download(app)
            warning = state.get("cancel_warning")
            if warning:
                return redirect(url_for("service_detail", service_id="web-depot", binary_download_warn=warning, _anchor="binary-download"))
            return redirect(url_for("service_detail", service_id="web-depot", binary_download_warn="Software depot download cancelled", _anchor="binary-download"))
        except OSError as err:
            return redirect(url_for("service_detail", service_id="web-depot", binary_download_error=str(err), _anchor="binary-download"))

    @app.route("/services/harbor-registry/config", methods=["POST"])
    def harbor_config():
        service = manager.get_service("harbor-registry")
        tls_enabled = request.form.get("tls_enabled") == "on"
        protocol = "https" if tls_enabled else "http"
        port = 9443 if tls_enabled else 9080
        service.settings["protocol"] = protocol
        service.settings["port"] = port
        service.settings["path"] = "/"
        service.settings["mode"] = "port"
        service.settings["tls_enabled"] = tls_enabled
        service.settings["tls_mode"] = "shared"
        service.settings["admin_user"] = request.form.get("admin_user", "").strip()
        if not service.settings["admin_user"]:
            return redirect(url_for("service_detail", service_id="harbor-registry", config_error="Username is required", _anchor="harbor-config"))
        password = request.form.get("admin_password", "")
        if password:
            service.settings["admin_password"] = password
        elif not service.settings.get("admin_password"):
            return redirect(url_for("service_detail", service_id="harbor-registry", config_error="Password is required", _anchor="harbor-config"))
        if tls_enabled:
            try:
                _ensure_shared_tls(app)
                _apply_shared_tls_settings(service)
            except OSError as err:
                return redirect(url_for("service_detail", service_id="harbor-registry", config_error=str(err), _anchor="harbor-config"))
        _update_endpoint(service, app.config["VIS_APPLIANCE_FQDN"])
        service.configured = bool(service.settings.get("admin_user") and service.settings.get("admin_password"))
        try:
            if app.config["VIS_LOCAL_ADAPTERS_ENABLED"]:
                _write_harbor_config(service, app.config["VIS_APPLIANCE_FQDN"])
            if service.configured and not service.enabled:
                service.health_status = "disabled"
            store.save_service(service)
            if app.config["VIS_LOCAL_ADAPTERS_ENABLED"]:
                manager.restart_service("harbor-registry")
            return redirect(url_for("service_detail", service_id="harbor-registry", config_status="Configuration updated", _anchor="harbor-config"))
        except OSError as err:
            store.save_service(service)
            return redirect(url_for("service_detail", service_id="harbor-registry", config_error=str(err), _anchor="harbor-config"))

    @app.route("/services/directory-identity-provider/config", methods=["POST"])
    @app.route("/services/ldap-provider/config", methods=["POST"])
    def ldap_provider_config():
        service = manager.get_service("ldap-provider")
        protocol = request.form.get("protocol", "ldap").lower()
        if protocol not in ("ldap", "ldaps"):
            return redirect(url_for("service_detail", service_id=service.id, ldap_config_error="Protocol must be LDAP or LDAPS", _anchor="ldap-config"))
        base_dn = request.form.get("base_dn", "").strip()
        bind_dn = request.form.get("bind_dn", "").strip()
        admin_user = request.form.get("admin_user", "").strip()
        admin_password = request.form.get("admin_password", "")
        if not _valid_base_dn(base_dn):
            return redirect(url_for("service_detail", service_id=service.id, ldap_config_error="Base DN must be a domain root, such as dc=williamlam,dc=local. Do not include the admin user in the Base DN.", _anchor="ldap-config"))
        if not bind_dn:
            bind_dn = "cn={},{}".format(admin_user or "admin", base_dn)
        if not _valid_dn(bind_dn):
            return redirect(url_for("service_detail", service_id=service.id, ldap_config_error="Bind DN must use LDAP DN syntax", _anchor="ldap-config"))
        if not admin_user:
            return redirect(url_for("service_detail", service_id=service.id, ldap_config_error="Admin user is required", _anchor="ldap-config"))
        if admin_password:
            service.settings["admin_password"] = admin_password
        elif not service.settings.get("admin_password"):
            return redirect(url_for("service_detail", service_id=service.id, ldap_config_error="Admin password is required", _anchor="ldap-config"))
        service.settings["protocol"] = protocol
        service.settings["port"] = 636 if protocol == "ldaps" else 389
        service.settings["tls_enabled"] = protocol == "ldaps"
        service.settings["base_dn"] = base_dn
        service.settings["bind_dn"] = bind_dn
        service.settings["admin_user"] = admin_user
        if protocol == "ldaps":
            try:
                _ensure_shared_tls(app)
                _apply_shared_tls_settings(service)
            except OSError as err:
                return redirect(url_for("service_detail", service_id=service.id, ldap_config_error=str(err), _anchor="ldap-config"))
        _update_ldap_provider_endpoint(service, app.config["VIS_APPLIANCE_FQDN"])
        service.configured = bool(base_dn and bind_dn and admin_user and service.settings.get("admin_password"))
        store.save_service(service)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, ldap_config_status="LDAP provider configuration updated", _anchor="ldap-config"))

    @app.route("/services/oidc-provider/config", methods=["POST"])
    def oidc_provider_config():
        service = manager.get_service("oidc-provider")
        tls_enabled = request.form.get("tls_enabled") == "on"
        service.settings["protocol"] = "https" if tls_enabled else "http"
        service.settings["port"] = 9444 if tls_enabled else 9081
        service.settings["path"] = "/"
        service.settings["mode"] = "port"
        service.settings["provider"] = "keycloak"
        service.settings["tls_enabled"] = tls_enabled
        service.settings["tls_mode"] = "shared"
        service.settings["realm"] = request.form.get("realm", "VCF").strip() or "VCF"
        service.settings["default_group"] = request.form.get("default_group", "vcf-admins").strip() or "vcf-admins"
        service.settings["admin_user"] = request.form.get("admin_user", "").strip()
        service.settings["image"] = request.form.get("image", "").strip() or service.settings.get("image") or "quay.io/keycloak/keycloak:26.3"
        if not service.settings["admin_user"]:
            return redirect(url_for("service_detail", service_id=service.id, oidc_config_error="Admin user is required", _anchor="oidc-config"))
        password = request.form.get("admin_password", "")
        if password:
            service.settings["admin_password"] = password
        elif not service.settings.get("admin_password"):
            return redirect(url_for("service_detail", service_id=service.id, oidc_config_error="Admin password is required", _anchor="oidc-config"))
        if tls_enabled:
            try:
                _ensure_shared_tls(app)
                _apply_shared_tls_settings(service)
            except OSError as err:
                return redirect(url_for("service_detail", service_id=service.id, oidc_config_error=str(err), _anchor="oidc-config"))
        _update_endpoint(service, app.config["VIS_APPLIANCE_FQDN"])
        service.configured = bool(service.settings.get("admin_user") and service.settings.get("admin_password") and service.settings.get("realm") and service.settings.get("default_group"))
        store.save_service(service)
        if app.config["VIS_LOCAL_ADAPTERS_ENABLED"]:
            manager.restart_service(service.id)
        else:
            manager.run_health_check(service.id)
        return redirect(url_for("service_detail", service_id=service.id, oidc_config_status="OIDC Provider configuration updated", _anchor="oidc-config"))

    @app.route("/services/oidc-provider/users", methods=["POST"])
    def oidc_provider_user_create():
        service = manager.get_service("oidc-provider")
        users = _directory_items(service, "users")
        groups = _ensure_identity_groups(service, store)
        user, error = _oidc_user_from_form(service, users, groups)
        if error:
            return redirect(url_for("service_detail", service_id=service.id, oidc_user_error=error, _anchor="oidc-users"))
        user["id"] = uuid.uuid4().hex
        users.insert(0, user)
        _save_directory_items(service, store, users=users)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, oidc_user_status="OIDC user created", _anchor="oidc-users"))

    @app.route("/services/oidc-provider/users/<user_id>", methods=["POST"])
    def oidc_provider_user_update(user_id):
        service = manager.get_service("oidc-provider")
        users = _directory_items(service, "users")
        groups = _ensure_identity_groups(service, store)
        if not any(user.get("id") == user_id for user in users):
            abort(404)
        user, error = _oidc_user_from_form(service, users, groups, current_id=user_id)
        if error:
            return redirect(url_for("service_detail", service_id=service.id, oidc_user_error=error, _anchor="oidc-users"))
        user["id"] = user_id
        for index, existing in enumerate(users):
            if existing.get("id") == user_id:
                if not user["password"]:
                    user["password"] = existing.get("password", "")
                users[index] = user
                break
        _save_directory_items(service, store, users=users)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, oidc_user_status="OIDC user updated", _anchor="oidc-users"))

    @app.route("/services/oidc-provider/users/<user_id>/delete", methods=["POST"])
    def oidc_provider_user_delete(user_id):
        service = manager.get_service("oidc-provider")
        users = [user for user in _directory_items(service, "users") if user.get("id") != user_id]
        groups = _directory_items(service, "groups")
        for group in groups:
            group["members"] = [member for member in group.get("members", []) if member != user_id]
        _save_directory_items(service, store, users=users, groups=groups)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, oidc_user_status="OIDC user removed", _anchor="oidc-users"))

    @app.route("/services/oidc-provider/clients", methods=["POST"])
    def oidc_provider_client_create():
        service = manager.get_service("oidc-provider")
        clients = _oidc_clients(service)
        client, error_message = _oidc_client_from_form(clients)
        if error_message:
            return redirect(url_for("service_detail", service_id=service.id, oidc_client_error=error_message, _anchor="oidc-clients"))
        client["id"] = uuid.uuid4().hex
        try:
            client = _sync_oidc_client_backend(app, manager, service, client)
        except OSError as err:
            return redirect(url_for("service_detail", service_id=service.id, oidc_client_error=str(err), _anchor="oidc-clients"))
        clients.insert(0, client)
        _save_oidc_clients(service, store, clients)
        return redirect(url_for("service_detail", service_id=service.id, oidc_client_status="OIDC client {} created".format(client["client_id"]), _anchor="oidc-clients"))

    @app.route("/services/oidc-provider/clients/<client_id>", methods=["POST"])
    def oidc_provider_client_update(client_id):
        service = manager.get_service("oidc-provider")
        clients = _oidc_clients(service)
        existing = next((client for client in clients if client.get("id") == client_id), None)
        if not existing:
            abort(404)
        client, error_message = _oidc_client_from_form(clients, current_id=client_id)
        if error_message:
            return redirect(url_for("service_detail", service_id=service.id, oidc_client_error=error_message, _anchor="oidc-clients"))
        client["id"] = client_id
        client["keycloak_id"] = existing.get("keycloak_id", "")
        client["client_secret"] = existing.get("client_secret", "")
        try:
            client = _sync_oidc_client_backend(app, manager, service, client)
        except OSError as err:
            return redirect(url_for("service_detail", service_id=service.id, oidc_client_error=str(err), _anchor="oidc-clients"))
        for index, item in enumerate(clients):
            if item.get("id") == client_id:
                clients[index] = client
                break
        _save_oidc_clients(service, store, clients)
        return redirect(url_for("service_detail", service_id=service.id, oidc_client_status="OIDC client {} updated".format(client["client_id"]), _anchor="oidc-clients"))

    @app.route("/services/oidc-provider/clients/<client_id>/delete", methods=["POST"])
    def oidc_provider_client_delete(client_id):
        service = manager.get_service("oidc-provider")
        clients = _oidc_clients(service)
        existing = next((client for client in clients if client.get("id") == client_id), None)
        if not existing:
            abort(404)
        if app.config["VIS_LOCAL_ADAPTERS_ENABLED"]:
            try:
                manager.adapter_for(service.id).delete_oidc_client(existing)
            except OSError as err:
                return redirect(url_for("service_detail", service_id=service.id, oidc_client_error=str(err), _anchor="oidc-clients"))
        _save_oidc_clients(service, store, [client for client in clients if client.get("id") != client_id])
        return redirect(url_for("service_detail", service_id=service.id, oidc_client_status="OIDC client removed", _anchor="oidc-clients"))

    @app.route("/services/oidc-provider/groups", methods=["POST"])
    def oidc_provider_group_create():
        service = manager.get_service("oidc-provider")
        groups = _ensure_identity_groups(service, store)
        group, error = _directory_group_from_form(groups)
        if error:
            return redirect(url_for("service_detail", service_id=service.id, oidc_group_error=error, _anchor="oidc-groups"))
        group["id"] = uuid.uuid4().hex
        groups.insert(0, group)
        _save_directory_items(service, store, groups=groups)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, oidc_group_status="OIDC group created", _anchor="oidc-groups"))

    @app.route("/services/oidc-provider/groups/<group_id>/members", methods=["POST"])
    def oidc_provider_group_members(group_id):
        service = manager.get_service("oidc-provider")
        users = _directory_items(service, "users")
        valid_user_ids = {user.get("id") for user in users}
        selected = [user_id for user_id in request.form.getlist("members") if user_id in valid_user_ids]
        groups = _ensure_identity_groups(service, store)
        for group in groups:
            if group.get("id") == group_id:
                group["members"] = selected
                _save_directory_items(service, store, groups=groups)
                refresh_service_backend(service.id)
                return redirect(url_for("service_detail", service_id=service.id, oidc_group_status="OIDC group membership updated", _anchor="oidc-groups"))
        abort(404)

    @app.route("/services/oidc-provider/groups/<group_id>/delete", methods=["POST"])
    def oidc_provider_group_delete(group_id):
        service = manager.get_service("oidc-provider")
        groups = _ensure_identity_groups(service, store)
        target = next((group for group in groups if group.get("id") == group_id), None)
        if not target:
            abort(404)
        if _is_standard_identity_group(target):
            return redirect(url_for("service_detail", service_id=service.id, oidc_group_error="Standard OIDC groups cannot be removed", _anchor="oidc-groups"))
        groups = [group for group in groups if group.get("id") != group_id]
        _save_directory_items(service, store, groups=groups)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, oidc_group_status="OIDC group removed", _anchor="oidc-groups"))

    @app.route("/services/directory-identity-provider/users", methods=["POST"])
    @app.route("/services/ldap-provider/users", methods=["POST"])
    def ldap_provider_user_create():
        service = manager.get_service("ldap-provider")
        users = _directory_items(service, "users")
        groups = _ensure_identity_groups(service, store)
        user, error = _directory_user_from_form(users, groups)
        if error:
            return redirect(url_for("service_detail", service_id=service.id, ldap_user_error=error, _anchor="ldap-users"))
        user["id"] = uuid.uuid4().hex
        users.insert(0, user)
        _save_directory_items(service, store, users=users)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, ldap_user_status="LDAP user created", _anchor="ldap-users"))

    @app.route("/services/directory-identity-provider/users/<user_id>", methods=["POST"])
    @app.route("/services/ldap-provider/users/<user_id>", methods=["POST"])
    def ldap_provider_user_update(user_id):
        service = manager.get_service("ldap-provider")
        users = _directory_items(service, "users")
        groups = _ensure_identity_groups(service, store)
        if not any(user.get("id") == user_id for user in users):
            abort(404)
        user, error = _directory_user_from_form(users, groups, current_id=user_id)
        if error:
            return redirect(url_for("service_detail", service_id=service.id, ldap_user_error=error, _anchor="ldap-users"))
        user["id"] = user_id
        for index, existing in enumerate(users):
            if existing.get("id") == user_id:
                if not user["password"]:
                    user["password"] = existing.get("password", "")
                users[index] = user
                break
        _save_directory_items(service, store, users=users)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, ldap_user_status="LDAP user updated", _anchor="ldap-users"))

    @app.route("/services/directory-identity-provider/users/<user_id>/delete", methods=["POST"])
    @app.route("/services/ldap-provider/users/<user_id>/delete", methods=["POST"])
    def ldap_provider_user_delete(user_id):
        service = manager.get_service("ldap-provider")
        users = [user for user in _directory_items(service, "users") if user.get("id") != user_id]
        groups = _ensure_identity_groups(service, store)
        for group in groups:
            group["members"] = [member for member in group.get("members", []) if member != user_id]
        _save_directory_items(service, store, users=users, groups=groups)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, ldap_user_status="LDAP user removed", _anchor="ldap-users"))

    @app.route("/services/directory-identity-provider/groups", methods=["POST"])
    @app.route("/services/ldap-provider/groups", methods=["POST"])
    def ldap_provider_group_create():
        service = manager.get_service("ldap-provider")
        groups = _ensure_identity_groups(service, store)
        group, error = _directory_group_from_form(groups)
        if error:
            return redirect(url_for("service_detail", service_id=service.id, ldap_group_error=error, _anchor="ldap-groups"))
        group["id"] = uuid.uuid4().hex
        groups.insert(0, group)
        _save_directory_items(service, store, groups=groups)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, ldap_group_status="LDAP group created", _anchor="ldap-groups"))

    @app.route("/services/directory-identity-provider/groups/<group_id>/members", methods=["POST"])
    @app.route("/services/ldap-provider/groups/<group_id>/members", methods=["POST"])
    def ldap_provider_group_members(group_id):
        service = manager.get_service("ldap-provider")
        users = _directory_items(service, "users")
        valid_user_ids = {user.get("id") for user in users}
        selected = [user_id for user_id in request.form.getlist("members") if user_id in valid_user_ids]
        groups = _ensure_identity_groups(service, store)
        for group in groups:
            if group.get("id") == group_id:
                group["members"] = selected
                _save_directory_items(service, store, groups=groups)
                refresh_service_backend(service.id)
                return redirect(url_for("service_detail", service_id=service.id, ldap_group_status="Group membership updated", _anchor="ldap-groups"))
        abort(404)

    @app.route("/services/directory-identity-provider/groups/<group_id>/delete", methods=["POST"])
    @app.route("/services/ldap-provider/groups/<group_id>/delete", methods=["POST"])
    def ldap_provider_group_delete(group_id):
        service = manager.get_service("ldap-provider")
        groups = _ensure_identity_groups(service, store)
        target = next((group for group in groups if group.get("id") == group_id), None)
        if not target:
            abort(404)
        if _is_standard_identity_group(target):
            return redirect(url_for("service_detail", service_id=service.id, ldap_group_error="Standard LDAP groups cannot be removed", _anchor="ldap-groups"))
        groups = [group for group in groups if group.get("id") != group_id]
        _save_directory_items(service, store, groups=groups)
        refresh_service_backend(service.id)
        return redirect(url_for("service_detail", service_id=service.id, ldap_group_status="LDAP group removed", _anchor="ldap-groups"))

    @app.route("/services/unbound-dns/entries", methods=["POST"])
    def dns_entry_create():
        service = manager.get_service("unbound-dns")
        entries = _dns_entries(service)
        entry, error = _dns_entry_from_form(service, entries)
        if error:
            return redirect(url_for("service_detail", service_id="unbound-dns", dns_entry_error=error, _anchor="dns-entries"))
        entry["id"] = uuid.uuid4().hex
        entries.insert(0, entry)
        _save_dns_entries(service, entries, store)
        refresh_service_backend("unbound-dns")
        return redirect(url_for("service_detail", service_id="unbound-dns", dns_entry_status="DNS entry added", _anchor="dns-entries"))

    @app.route("/services/unbound-dns/config", methods=["POST"])
    def dns_config():
        service = manager.get_service("unbound-dns")
        domain, error = _dns_domain_from_form()
        if error:
            return redirect(url_for("service_detail", service_id="unbound-dns", dns_config_error=error, _anchor="dns-config"))
        entries = _dns_entries(service)
        current_domain = str(service.settings.get("domain", "")).strip(".").lower()
        if entries and current_domain and domain != current_domain:
            return redirect(url_for("service_detail", service_id="unbound-dns", dns_config_error="Remove DNS entries before changing the DNS domain.", _anchor="dns-config"))
        try:
            default_ttl = int(request.form.get("default_ttl", service.settings.get("default_ttl", 3600)))
        except (TypeError, ValueError):
            return redirect(url_for("service_detail", service_id="unbound-dns", dns_config_error="Default TTL must be a whole number of seconds.", _anchor="dns-config"))
        if default_ttl < 60 or default_ttl > 86400:
            return redirect(url_for("service_detail", service_id="unbound-dns", dns_config_error="Default TTL must be between 60 and 86400 seconds.", _anchor="dns-config"))
        upstream_enabled = request.form.get("forward_upstream_enabled") == "on"
        upstream_servers, upstream_error = _dns_forward_upstream_servers_from_form(upstream_enabled)
        if upstream_error:
            return redirect(url_for("service_detail", service_id="unbound-dns", dns_config_error=upstream_error, _anchor="dns-config"))
        disable_dnssec = request.form.get("disable_dnssec") == "on"
        service.settings["domain"] = domain
        service.settings["default_ttl"] = default_ttl
        service.settings["disable_dnssec"] = disable_dnssec
        service.settings["forward_upstream_enabled"] = upstream_enabled
        service.settings["forward_upstream_servers"] = upstream_servers
        service.configured = bool(domain)
        store.save_service(service)
        refresh_service_backend("unbound-dns")
        return redirect(url_for("service_detail", service_id="unbound-dns", dns_config_status="DNS configuration updated", _anchor="dns-config"))

    @app.route("/services/unbound-dns/entries/<entry_id>", methods=["POST"])
    def dns_entry_update(entry_id):
        service = manager.get_service("unbound-dns")
        entries = _dns_entries(service)
        existing = next((entry for entry in entries if entry.get("id") == entry_id), None)
        if not existing:
            abort(404)
        entry, error = _dns_entry_from_form(service, entries, current_id=entry_id)
        if error:
            return redirect(url_for("service_detail", service_id="unbound-dns", dns_entry_error=error, _anchor="dns-entries"))
        entry["id"] = entry_id
        for index, item in enumerate(entries):
            if item.get("id") == entry_id:
                entries[index] = entry
                break
        _save_dns_entries(service, entries, store)
        refresh_service_backend("unbound-dns")
        return redirect(url_for("service_detail", service_id="unbound-dns", dns_entry_status="DNS entry updated", _anchor="dns-entries"))

    @app.route("/services/unbound-dns/entries/<entry_id>/delete", methods=["POST"])
    def dns_entry_delete(entry_id):
        service = manager.get_service("unbound-dns")
        entries = _dns_entries(service)
        filtered = [entry for entry in entries if entry.get("id") != entry_id]
        if len(filtered) == len(entries):
            abort(404)
        _save_dns_entries(service, filtered, store)
        refresh_service_backend("unbound-dns")
        return redirect(url_for("service_detail", service_id="unbound-dns", dns_entry_status="DNS entry removed", _anchor="dns-entries"))

    @app.route("/services/time-server/config", methods=["POST"])
    def time_server_config():
        service = manager.get_service("time-server")
        settings, error = _time_settings_from_form()
        if error:
            return redirect(url_for("service_detail", service_id="time-server", time_config_error=error, _anchor="time-config"))
        service.settings.update(settings)
        service.configured = True
        store.save_service(service)
        refresh_service_backend("time-server")
        return redirect(url_for("service_detail", service_id="time-server", time_config_status="NTP Server configuration updated", _anchor="time-config"))

    @app.route("/services/dhcp-server/config", methods=["POST"])
    def dhcp_server_config():
        service = manager.get_service("dhcp-server")
        settings, error = _dhcp_settings_from_form()
        if error:
            return redirect(url_for("service_detail", service_id="dhcp-server", dhcp_config_error=error, _anchor="dhcp-config"))
        service.settings.update(settings)
        service.configured = True
        store.save_service(service)
        refresh_service_backend("dhcp-server")
        return redirect(url_for("service_detail", service_id="dhcp-server", dhcp_config_status="DHCP Server configuration updated", _anchor="dhcp-config"))

    @app.route("/services/kms-service/config", methods=["POST"])
    def kms_service_config():
        service = manager.get_service("kms-service")
        settings, error = _kms_settings_from_form()
        if error:
            return redirect(url_for("service_detail", service_id="kms-service", kms_config_error=error, _anchor="kms-config"))
        try:
            _ensure_shared_tls(app)
        except OSError as err:
            return redirect(url_for("service_detail", service_id="kms-service", kms_config_error=str(err), _anchor="kms-config"))
        _apply_shared_tls_settings(service)
        service.settings.update(settings)
        service.configured = True
        store.save_service(service)
        refresh_service_backend("kms-service")
        return redirect(url_for("service_detail", service_id="kms-service", kms_config_status="Key Management Service configuration updated", _anchor="kms-config"))

    @app.route("/services/web-depot/files/mkdir", methods=["POST"])
    def depot_mkdir():
        service = manager.get_service("web-depot")
        current = request.form.get("path", "")
        name = request.form.get("name", "").strip()
        try:
            _file_manager(service).mkdir(current, name)
            return redirect(url_for("service_detail", service_id="web-depot", path=current, _anchor="repository-files"))
        except (FileExistsError, FileNotFoundError, ValueError, OSError) as err:
            return redirect(url_for("service_detail", service_id="web-depot", path=current, file_error=str(err), _anchor="repository-files"))

    @app.route("/services/web-depot/files/delete", methods=["POST"])
    def depot_delete():
        service = manager.get_service("web-depot")
        target = request.form.get("target", "")
        current = request.form.get("path", "")
        try:
            _file_manager(service).delete(target)
            return redirect(url_for("service_detail", service_id="web-depot", path=current, _anchor="repository-files"))
        except (FileNotFoundError, ValueError, OSError) as err:
            return redirect(url_for("service_detail", service_id="web-depot", path=current, file_error=str(err), _anchor="repository-files"))

    @app.route("/services/sftp-backup/files/mkdir", methods=["POST"])
    def sftp_mkdir():
        service = manager.get_service("sftp-backup")
        current = request.form.get("path", "")
        name = request.form.get("name", "").strip()
        try:
            _sftp_file_manager(service).mkdir(current, name)
            return redirect(url_for("service_detail", service_id="sftp-backup", path=current, _anchor="repository-files"))
        except (FileExistsError, FileNotFoundError, ValueError, OSError) as err:
            return redirect(url_for("service_detail", service_id="sftp-backup", path=current, file_error=str(err), _anchor="repository-files"))

    @app.route("/services/sftp-backup/files/delete", methods=["POST"])
    def sftp_delete():
        service = manager.get_service("sftp-backup")
        target = request.form.get("target", "")
        current = request.form.get("path", "")
        try:
            _sftp_file_manager(service).delete(target)
            return redirect(url_for("service_detail", service_id="sftp-backup", path=current, _anchor="repository-files"))
        except (FileNotFoundError, ValueError, OSError) as err:
            return redirect(url_for("service_detail", service_id="sftp-backup", path=current, file_error=str(err), _anchor="repository-files"))

    @app.route("/api/services/<service_id>/files")
    def repository_files_api(service_id):
        if service_id not in ("web-depot", "sftp-backup"):
            abort(404)
        try:
            service = manager.get_service(service_id)
            listing = _file_manager(service).list_dir(request.args.get("path", ""))
        except KeyError:
            abort(404)
        except (FileNotFoundError, NotADirectoryError, ValueError) as err:
            return jsonify({"ok": False, "message": str(err)}), 400
        listing["ok"] = True
        return jsonify(listing)

    @app.route("/services/<service_id>/files/upload", methods=["POST"])
    def repository_upload(service_id):
        if service_id != "web-depot":
            abort(404)
        service = manager.get_service(service_id)
        previous_tempdir = tempfile.tempdir
        try:
            _prepare_upload_temp_dir(service)
            upload = request.files.get("file")
        except RequestEntityTooLarge:
            return jsonify(
                {
                    "ok": False,
                    "error": "upload_too_large",
                    "message": "Upload failed because the request is larger than the appliance allows.",
                }
            ), 413
        except OSError as err:
            payload, status = _upload_os_error(err)
            return jsonify(payload), status
        finally:
            tempfile.tempdir = previous_tempdir
        if not upload:
            return jsonify({"ok": False, "error": "missing_file", "message": "Upload file is required"}), 400

        current = request.form.get("path", "")
        relative_file_path = request.form.get("relative_path") or upload.filename
        overwrite = request.form.get("overwrite", "").lower() in ("1", "true", "yes", "on")
        try:
            saved = _file_manager(service).save_upload(current, relative_file_path, upload, overwrite=overwrite)
        except FileExistsError as err:
            return jsonify({"ok": False, "error": "exists", "path": str(err), "message": "File already exists"}), 409
        except (FileNotFoundError, IsADirectoryError, ValueError, OSError) as err:
            if isinstance(err, OSError):
                payload, status = _upload_os_error(err)
                return jsonify(payload), status
            return jsonify({"ok": False, "error": "upload_failed", "message": str(err)}), 400

        return jsonify({"ok": True, "file": saved})

    @app.route("/services/<service_id>/files/upload-chunk", methods=["POST"])
    def repository_upload_chunk(service_id):
        if service_id != "web-depot":
            abort(404)
        service = manager.get_service(service_id)
        previous_tempdir = tempfile.tempdir
        temp_dir = None
        try:
            temp_dir = _prepare_upload_temp_dir(service)
            chunk = request.files.get("chunk")
        except RequestEntityTooLarge:
            return jsonify(
                {
                    "ok": False,
                    "error": "upload_too_large",
                    "message": "Upload failed because the request chunk is larger than the appliance allows.",
                }
            ), 413
        except OSError as err:
            payload, status = _upload_os_error(err)
            return jsonify(payload), status
        finally:
            tempfile.tempdir = previous_tempdir
        if not chunk:
            return jsonify({"ok": False, "error": "missing_chunk", "message": "Upload chunk is required"}), 400

        try:
            saved = _file_manager(service).save_upload_chunk(
                request.form.get("path", ""),
                request.form.get("relative_path") or chunk.filename,
                request.form.get("upload_id", ""),
                chunk.stream,
                _form_int("chunk_index"),
                _form_int("total_chunks"),
                _form_int("total_size"),
                _form_int("offset"),
                overwrite=request.form.get("overwrite", "").lower() in ("1", "true", "yes", "on"),
                temp_root=os.path.join(temp_dir, "chunks") if temp_dir else None,
            )
        except FileExistsError as err:
            return jsonify({"ok": False, "error": "exists", "path": str(err), "message": "File already exists"}), 409
        except (FileNotFoundError, IsADirectoryError, ValueError, OSError) as err:
            if isinstance(err, OSError):
                payload, status = _upload_os_error(err)
                return jsonify(payload), status
            return jsonify({"ok": False, "error": "upload_failed", "message": str(err)}), 400

        return jsonify({"ok": True, "complete": saved.get("complete", False), "file": saved})

    @app.route("/services/sftp-backup/password", methods=["POST"])
    def sftp_password():
        service = manager.get_service("sftp-backup")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        existing_password = str(service.settings.get("password", ""))
        username_changed = username != str(service.settings.get("user", ""))
        if not username:
            return redirect(url_for("service_detail", service_id="sftp-backup", password_error="Username is required", _anchor="sftp-credentials"))
        if not _valid_local_username(username):
            return redirect(url_for("service_detail", service_id="sftp-backup", password_error="Username must be a valid local account name", _anchor="sftp-credentials"))
        if not password:
            if username_changed or not existing_password:
                return redirect(url_for("service_detail", service_id="sftp-backup", password_error="Password is required", _anchor="sftp-credentials"))
            password = existing_password
            confirm = existing_password
        if password != confirm:
            return redirect(url_for("service_detail", service_id="sftp-backup", password_error="Passwords do not match", _anchor="sftp-credentials"))

        if app.config["VIS_LOCAL_ADAPTERS_ENABLED"]:
            try:
                _configure_local_sftp(username, password, service)
            except OSError as err:
                return redirect(
                    url_for(
                        "service_detail",
                        service_id="sftp-backup",
                        password_error=str(err),
                        _anchor="sftp-credentials",
                    )
                )

        service.settings["user"] = username
        service.settings["password"] = password
        service.endpoint = _sftp_endpoint(service, app.config["VIS_APPLIANCE_FQDN"])
        store.save_service(service)
        manager.run_health_check("sftp-backup")
        return redirect(url_for("service_detail", service_id="sftp-backup", password_status="SFTP credentials updated", _anchor="sftp-credentials"))

    @app.route("/api/services")
    def services_api():
        _normalize_service_endpoints(manager, store, app.config["VIS_APPLIANCE_FQDN"])
        return jsonify({"services": [service.to_dict() for service in manager.list_services()]})

    @app.route("/api/services/<service_id>")
    def service_api(service_id):
        try:
            service = manager.get_service(service_id)
        except KeyError:
            abort(404)
        _ensure_service_endpoint(service, store, app.config["VIS_APPLIANCE_FQDN"])
        return jsonify(service.to_dict())

    return app


def _sftp_file_manager(service):
    return RepositoryFileManager(service.filesystem_root, owner=str(service.settings.get("user", "")))


def _file_manager(service):
    if service.id == "sftp-backup":
        return _sftp_file_manager(service)
    return RepositoryFileManager(service.filesystem_root)


def _prepare_upload_temp_dir(service):
    temp_dir = os.environ.get("VIS_UPLOAD_TMP_DIR") or os.path.join(service.filesystem_root, ".vis-upload-tmp")
    os.makedirs(temp_dir, mode=0o750, exist_ok=True)
    tempfile.tempdir = temp_dir
    return temp_dir


def _form_int(name):
    try:
        return int(request.form.get(name, ""))
    except (TypeError, ValueError):
        raise ValueError("{} must be an integer".format(name))


def _upload_os_error(err):
    if getattr(err, "errno", None) == errno.ENOSPC:
        return {
            "ok": False,
            "error": "insufficient_storage",
            "message": (
                "Upload failed because the appliance ran out of staging space. "
                "Large Depot uploads are staged on the Depot filesystem; verify free space and retry."
            ),
        }, 507
    return {
        "ok": False,
        "error": "upload_failed",
        "message": "Upload failed: {}".format(str(err) or err.__class__.__name__),
    }, 400


def _detect_primary_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def _detect_primary_interface():
    try:
        result = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, check=False)
    except OSError:
        return ""
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            index = parts.index("dev")
            if index + 1 < len(parts):
                return parts[index + 1]
    return ""


def _update_endpoint(service, fqdn):
    service.endpoint = _service_endpoint(service, fqdn)


def _service_endpoint(service, fqdn):
    if service.id == "sftp-backup":
        return _sftp_endpoint(service, fqdn)
    if service.id == "ldap-provider":
        return _ldap_provider_endpoint(service, fqdn)
    if service.id == "unbound-dns":
        port = int(service.settings.get("port", 53))
        port_suffix = "" if port == 53 else ":{}".format(port)
        return "dns://{}{}".format(fqdn, port_suffix)
    if service.id == "time-server":
        port = int(service.settings.get("port", 123))
        port_suffix = "" if port == 123 else ":{}".format(port)
        return "ntp://{}{}".format(fqdn, port_suffix)
    if service.id == "dhcp-server":
        port = int(service.settings.get("port", 67))
        port_suffix = "" if port == 67 else ":{}".format(port)
        return "dhcp://{}{}".format(fqdn, port_suffix)
    if service.id == "kms-service":
        port = int(service.settings.get("port", 5696))
        port_suffix = "" if port == 5696 else ":{}".format(port)
        return "kmip://{}{}".format(fqdn, port_suffix)
    protocol = str(service.settings.get("protocol", "http")).lower()
    port = int(service.settings.get("port", 8443 if protocol == "https" else 8081))
    default_port = 443 if protocol == "https" else 80
    port_suffix = "" if port == default_port else ":{}".format(port)
    path = service.settings.get("path", "/")
    if not str(path).startswith("/"):
        path = "/{}".format(path)
    return "{}://{}{}{}".format(protocol, fqdn, port_suffix, path)


def _update_ldap_provider_endpoint(service, fqdn):
    service.endpoint = _ldap_provider_endpoint(service, fqdn)


def _ldap_provider_endpoint(service, fqdn):
    protocol = str(service.settings.get("protocol", "ldap")).lower()
    port = int(service.settings.get("port", 636 if protocol == "ldaps" else 389))
    default_port = 636 if protocol == "ldaps" else 389
    port_suffix = "" if port == default_port else ":{}".format(port)
    return "{}://{}{}".format(protocol, fqdn, port_suffix)


def _sftp_endpoint(service, fqdn):
    username = str(service.settings.get("user", "")).strip()
    user_prefix = "{}@".format(username) if username else ""
    port = int(service.settings.get("port", 22))
    port_suffix = "" if port == 22 else ":{}".format(port)
    return "sftp://{}{}{}{}".format(user_prefix, fqdn, port_suffix, service.settings.get("path", "/backup"))


def _ensure_service_endpoint(service, store, fqdn):
    endpoint = _service_endpoint(service, fqdn)
    if service.endpoint != endpoint:
        service.endpoint = endpoint
        store.save_service(service)
    return service


def _normalize_service_endpoints(manager, store, fqdn):
    for service in manager.list_services():
        _ensure_service_endpoint(service, store, fqdn)


def _dns_entries(service):
    entries = service.settings.get("entries", [])
    if not isinstance(entries, list):
        return []
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        item.setdefault("id", uuid.uuid4().hex)
        item["name"] = str(item.get("name", "")).rstrip(".")
        item["address"] = str(item.get("address", ""))
        try:
            item["ttl"] = int(item.get("ttl", service.settings.get("default_ttl", 3600)))
        except (TypeError, ValueError):
            item["ttl"] = int(service.settings.get("default_ttl", 3600))
        normalized.append(item)
    return normalized


def _time_settings_from_form():
    mode = request.form.get("mode", "ntp").strip()
    if mode not in ("ntp", "ntp_ptp"):
        return None, "NTP Server mode must be NTP only or NTP + PTP."
    listen_address = "0.0.0.0"
    allowed_clients, error = _line_list_from_form("allowed_clients", validate_network=True)
    if error:
        return None, error
    upstream_sources, error = _line_list_from_form("upstream_sources")
    if error:
        return None, error
    local_fallback_enabled = request.form.get("local_fallback_enabled") == "on"
    if not upstream_sources and not local_fallback_enabled:
        return None, "Add at least one upstream NTP source or enable local clock fallback."
    try:
        fallback_stratum = int(request.form.get("fallback_stratum", "10"))
    except (TypeError, ValueError):
        return None, "Fallback Stratum must be a whole number."
    if fallback_stratum < 1 or fallback_stratum > 15:
        return None, "Fallback Stratum must be between 1 and 15."
    ptp_enabled = request.form.get("ptp_enabled") == "on" or mode == "ntp_ptp"
    ptp_interface = request.form.get("ptp_interface", "").strip()
    ptp_profile = "default"
    ptp_transport = request.form.get("ptp_transport", "udp4").strip()
    ptp_timestamping = request.form.get("ptp_timestamping", "auto").strip()
    try:
        ptp_domain = int(request.form.get("ptp_domain", "0"))
    except (TypeError, ValueError):
        return None, "PTP Domain must be a whole number."
    if ptp_domain < 0 or ptp_domain > 127:
        return None, "PTP Domain must be between 0 and 127."
    if ptp_enabled and not ptp_interface:
        return None, "Select a PTP interface before enabling PTP."
    if ptp_transport not in ("udp4", "l2"):
        return None, "PTP Transport must be UDP over IPv4 or Layer 2 Ethernet."
    if ptp_timestamping not in ("auto", "software", "hardware"):
        return None, "PTP Timestamping must be auto, software, or hardware."
    return {
        "protocol": "ntp",
        "port": 123,
        "path": "",
        "mode": "ntp_ptp" if ptp_enabled else "ntp",
        "listen_address": listen_address,
        "allowed_clients": allowed_clients,
        "upstream_sources": upstream_sources,
        "local_fallback_enabled": local_fallback_enabled,
        "fallback_stratum": fallback_stratum,
        "ptp_enabled": ptp_enabled,
        "ptp_interface": ptp_interface,
        "ptp_profile": ptp_profile,
        "ptp_domain": ptp_domain,
        "ptp_transport": ptp_transport,
        "ptp_timestamping": ptp_timestamping,
    }, None


def _dhcp_settings_from_form():
    service = current_app.config["service_manager"].get_service("dhcp-server") if "service_manager" in current_app.config else None
    interface = (request.form.get("interface", "") or (service.settings.get("interface", "") if service else "") or _detect_primary_interface() or "ens160").strip()
    if not interface:
        return None, "Network interface is required."
    if not re.match(r"^[A-Za-z0-9_.:-]+$", interface):
        return None, "Network interface contains unsupported characters."
    subnet_text = request.form.get("subnet_cidr", "").strip()
    try:
        subnet = ipaddress.ip_network(subnet_text, strict=False)
    except ValueError:
        return None, "Subnet CIDR must be valid, such as 172.30.0.0/24."
    pool_start, error = _ip_from_form("pool_start", "Pool Start")
    if error:
        return None, error
    pool_end, error = _ip_from_form("pool_end", "Pool End")
    if error:
        return None, error
    if pool_start not in subnet:
        return None, "Pool Start must be inside {}.".format(subnet)
    if pool_end not in subnet:
        return None, "Pool End must be inside {}.".format(subnet)
    if int(pool_start) > int(pool_end):
        return None, "Pool Start must be lower than or equal to Pool End."
    gateway_text = request.form.get("gateway", "").strip()
    gateway = ""
    if gateway_text:
        try:
            gateway_ip = ipaddress.ip_address(gateway_text)
        except ValueError:
            return None, "Gateway must be a valid IP address."
        if gateway_ip not in subnet:
            return None, "Gateway must be inside {}.".format(subnet)
        gateway = str(gateway_ip)
    dns_servers = []
    for value in _lines(request.form.get("dns_servers", "")):
        try:
            dns_servers.append(str(ipaddress.ip_address(value)))
        except ValueError:
            return None, "{} is not a valid DNS server IP address.".format(value)
    domain = request.form.get("domain", "").strip().strip(".").lower()
    if domain and not re.match(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$", domain):
        return None, "Domain Name must be a valid DNS domain."
    try:
        default_lease_time = int(request.form.get("default_lease_time", "3600"))
        max_lease_time = int(request.form.get("max_lease_time", "7200"))
    except (TypeError, ValueError):
        return None, "Lease times must be whole numbers."
    if default_lease_time < 60 or default_lease_time > 604800:
        return None, "Default lease time must be between 60 and 604800 seconds."
    if max_lease_time < default_lease_time or max_lease_time > 604800:
        return None, "Max lease time must be greater than default lease time and no more than 604800 seconds."
    reservations, error = _dhcp_reservations_from_form(request.form.get("reservations", ""), subnet)
    if error:
        return None, error
    return {
        "protocol": "dhcp",
        "port": 67,
        "path": "",
        "interface": interface,
        "subnet_cidr": str(subnet),
        "pool_start": str(pool_start),
        "pool_end": str(pool_end),
        "gateway": gateway,
        "dns_servers": dns_servers,
        "domain": domain,
        "reservations": reservations,
        "default_lease_time": default_lease_time,
        "max_lease_time": max_lease_time,
        "authoritative": request.form.get("authoritative") == "on",
    }, None


def _kms_settings_from_form():
    try:
        port = int(request.form.get("port", "5696"))
    except (TypeError, ValueError):
        return None, "KMIP port must be a whole number."
    if port < 1 or port > 65535:
        return None, "KMIP port must be between 1 and 65535."
    return {
        "protocol": "kmip",
        "port": port,
        "path": "",
        "provider": "pykmip",
        "listen_address": "0.0.0.0",
        "database_path": "/opt/vis/data/kms/pykmip.db",
        "config_path": "/opt/vis/config/kms/server.conf",
        "tls_enabled": True,
        "tls_mode": "shared",
    }, None


def _ip_from_form(name, label):
    value = request.form.get(name, "").strip()
    try:
        return ipaddress.ip_address(value), None
    except ValueError:
        return None, "{} must be a valid IP address.".format(label)


def _dhcp_reservations_from_form(raw, subnet):
    reservations = []
    seen = set()
    for line in str(raw or "").splitlines():
        value = line.strip()
        if not value:
            continue
        parts = [part.strip() for part in value.split(",")]
        if len(parts) < 2 or len(parts) > 3:
            return None, "Reservations must use MAC,IP,hostname format."
        mac, ip_text = parts[0], parts[1]
        hostname = parts[2] if len(parts) == 3 else ""
        if not re.match(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$", mac):
            return None, "{} is not a valid MAC address.".format(mac)
        try:
            ip_value = ipaddress.ip_address(ip_text)
        except ValueError:
            return None, "{} is not a valid reservation IP address.".format(ip_text)
        if ip_value not in subnet:
            return None, "Reservation IP {} must be inside {}.".format(ip_value, subnet)
        if hostname and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", hostname):
            return None, "{} is not a valid reservation hostname.".format(hostname)
        key = (mac.lower(), str(ip_value))
        if key in seen:
            return None, "Duplicate reservation {} {}.".format(mac, ip_value)
        seen.add(key)
        reservations.append({"mac": mac.lower(), "ip": str(ip_value), "hostname": hostname})
    return reservations, None


def _line_list_from_form(name, validate_network=False):
    raw = request.form.get(name, "")
    values = []
    for line in raw.splitlines():
        value = line.strip()
        if not value:
            continue
        if validate_network:
            try:
                value = str(ipaddress.ip_network(value, strict=False))
            except ValueError:
                return None, "{} is not a valid client network.".format(value)
        elif not re.match(r"^[A-Za-z0-9_.:-]+$", value):
            return None, "{} contains unsupported characters.".format(value)
        values.append(value)
    return values, None


def _dns_entry_from_form(service, entries, current_id=None):
    domain = str(service.settings.get("domain", "")).strip(".").lower()
    if not domain:
        return None, "Configure a DNS domain before adding DNS entries."
    raw_name = request.form.get("name", "").strip()
    raw_address = request.form.get("address", "").strip()
    raw_ttl = request.form.get("ttl", str(service.settings.get("default_ttl", 3600))).strip()
    name, name_error = _normalize_dns_name(raw_name, domain)
    if name_error:
        return None, name_error
    try:
        address = str(ipaddress.ip_address(raw_address))
    except ValueError:
        return None, "{} is not a valid IP address.".format(raw_address or "Address")
    try:
        ttl = int(raw_ttl)
    except ValueError:
        return None, "TTL must be a whole number of seconds."
    if ttl < 60 or ttl > 86400:
        return None, "TTL must be between 60 and 86400 seconds."

    for entry in entries:
        if current_id and entry.get("id") == current_id:
            continue
        if str(entry.get("name", "")).lower().rstrip(".") == name.lower():
            return None, "{} already exists. Edit the existing DNS entry or choose a different name.".format(name)
        if str(entry.get("address", "")) == address:
            return None, "{} is already used by {}. Choose a different address or edit the existing DNS entry.".format(address, entry.get("name", "another DNS entry"))

    return {"name": name, "address": address, "ttl": ttl}, ""


def _dns_domain_from_form():
    raw_domain = request.form.get("domain", "").strip()
    if not raw_domain:
        return "", "DNS domain is required before adding DNS entries."
    return _normalize_dns_domain(raw_domain)


def _dns_forward_upstream_servers_from_form(enabled):
    servers = []
    for value in _lines(request.form.get("forward_upstream_servers", "")):
        try:
            servers.append(str(ipaddress.ip_address(value)))
        except ValueError:
            return None, "{} is not a valid upstream DNS server IP address.".format(value)
    if enabled and not servers:
        return None, "Add at least one upstream DNS server or disable Forward Upstream DNS."
    return servers, ""


def _normalize_dns_domain(raw_domain):
    domain = raw_domain.rstrip(".").lower()
    if len(domain) > 253:
        return "", "DNS domain is too long."
    if "." not in domain:
        return "", "DNS domain must include at least two labels, such as williamlam.local."
    label_pattern = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
    labels = domain.split(".")
    if any(label_pattern.match(label) is None for label in labels):
        return "", "DNS domain must use DNS-safe labels with letters, numbers, and dashes."
    return domain, ""


def _normalize_dns_name(raw_name, domain):
    if not raw_name:
        return "", "Name is required."
    name = raw_name.rstrip(".").lower()
    if len(name) > 253:
        return "", "Name is too long for DNS."
    label_pattern = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
    labels = name.split(".")
    if any(label_pattern.match(label) is None for label in labels):
        return "", "Name must use DNS-safe labels with letters, numbers, and dashes."
    domain = (domain or "").strip(".").lower()
    if "." not in name and domain:
        name = "{}.{}".format(name, domain)
    elif domain and name != domain and not name.endswith(".{}".format(domain)):
        return "", "Name must be within the configured DNS domain {}.".format(domain)
    return name, ""


def _save_dns_entries(service, entries, store):
    service.settings["entries"] = entries
    service.configured = bool(service.settings.get("domain"))
    service.health_status = "disabled" if service.configured and not service.enabled else service.health_status
    store.save_service(service)


def _directory_items(service, key):
    items = service.settings.get(key, [])
    if not isinstance(items, list):
        return []
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        entry.setdefault("id", uuid.uuid4().hex)
        if key == "users":
            entry.setdefault("uid", "")
            entry.setdefault("display_name", "")
            entry.setdefault("email", "")
            entry.setdefault("password", "")
            entry.setdefault("disabled", False)
            groups = entry.get("groups", [])
            entry["groups"] = groups if isinstance(groups, list) else []
        if key == "groups":
            entry.setdefault("name", "")
            entry.setdefault("description", "")
            members = entry.get("members", [])
            entry["members"] = members if isinstance(members, list) else []
        normalized.append(entry)
    return normalized


def _ensure_identity_groups(service, store):
    groups = _directory_items(service, "groups")
    existing = {str(group.get("name", "")).lower(): group for group in groups}
    changed = False
    for default_group in DEFAULT_IDENTITY_GROUPS:
        name = str(default_group.get("name", "")).strip()
        if not name or name.lower() in existing:
            continue
        groups.append(
            {
                "id": str(default_group.get("id") or uuid.uuid4().hex),
                "name": name,
                "description": str(default_group.get("description", "")),
                "members": [],
            }
        )
        changed = True
    if changed:
        _save_directory_items(service, store, groups=groups)
        groups = _directory_items(service, "groups")
    return groups


def _is_standard_identity_group(group):
    standard_names = {str(item.get("name", "")).lower() for item in DEFAULT_IDENTITY_GROUPS}
    return str(group.get("name", "")).lower() in standard_names


def _oidc_clients(service):
    clients = service.settings.get("oidc_clients", [])
    if not isinstance(clients, list):
        return []
    normalized = []
    for client in clients:
        if not isinstance(client, dict):
            continue
        entry = dict(client)
        entry.setdefault("id", uuid.uuid4().hex)
        entry.setdefault("client_id", "")
        entry.setdefault("redirect_url", "")
        entry.setdefault("client_secret", "")
        entry.setdefault("keycloak_id", "")
        normalized.append(entry)
    return normalized


def _oidc_client_from_form(clients, current_id=None):
    client_id = request.form.get("client_id", "").strip()
    redirect_url = request.form.get("redirect_url", "").strip()
    if not re.match(r"^[A-Za-z0-9._:-]{2,128}$", client_id):
        return None, "Client ID must use letters, numbers, dot, dash, underscore, or colon."
    if not _valid_redirect_url(redirect_url):
        return None, "Redirect URL must be an http or https URL."
    for client in clients:
        if current_id and client.get("id") == current_id:
            continue
        if str(client.get("client_id", "")).lower() == client_id.lower():
            return None, "OIDC client {} already exists.".format(client_id)
    return {"client_id": client_id, "redirect_url": redirect_url}, ""


def _save_oidc_clients(service, store, clients):
    service.settings["oidc_clients"] = clients
    store.save_service(service)


def _sync_oidc_client_backend(app, manager, service, client):
    if app.config["VIS_LOCAL_ADAPTERS_ENABLED"]:
        return manager.adapter_for(service.id).ensure_oidc_client(client)
    updated = dict(client)
    updated.setdefault("keycloak_id", "")
    updated["client_secret"] = updated.get("client_secret") or uuid.uuid4().hex
    return updated


def _oidc_discovery_endpoint(service, fqdn):
    endpoint = _service_endpoint(service, fqdn).rstrip("/")
    realm = str(service.settings.get("realm", "VCF")).strip() or "VCF"
    return "{}/realms/{}/.well-known/openid-configuration".format(endpoint, realm)


def _oidc_user_from_form(service, users, groups, current_id=None):
    username = request.form.get("username", "").strip().lower()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    disabled = request.form.get("disabled") == "on"
    group_ids = set(request.form.getlist("groups"))
    valid_group_ids = {group.get("id") for group in groups}
    if not _valid_directory_uid(username):
        return None, "Username must start with a letter and use letters, numbers, dot, dash, or underscore."
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None, "Email address is not valid."
    if not current_id and not password:
        return None, "Password is required for new OIDC users."
    for user in users:
        if current_id and user.get("id") == current_id:
            continue
        if str(user.get("username", "")).lower() == username:
            return None, "OIDC user {} already exists".format(username)
    selected = sorted(group_ids.intersection(valid_group_ids))
    if not selected:
        default_name = str(service.settings.get("default_group", "vcf-admins")).lower()
        default_group = next((group for group in groups if str(group.get("name", "")).lower() == default_name), None)
        if default_group:
            selected = [default_group.get("id")]
    return {
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "password": password,
        "disabled": disabled,
        "groups": selected,
    }, ""


def _directory_user_from_form(users, groups, current_id=None):
    existing_user = next((user for user in users if current_id and user.get("id") == current_id), {})
    uid = request.form.get("uid", "").strip().lower()
    display_name = request.form.get("display_name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    disabled = request.form.get("disabled") == "on"
    group_ids = set(request.form.getlist("groups"))
    valid_group_ids = {group.get("id") for group in groups}
    if not _valid_directory_uid(uid):
        return None, "Username must start with a letter and use letters, numbers, dot, dash, or underscore."
    if not display_name:
        return None, "Display name is required."
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None, "Email address is not valid."
    if not current_id and not password:
        return None, "Password is required for new LDAP users."
    for user in users:
        if current_id and user.get("id") == current_id:
            continue
        if str(user.get("uid", "")).lower() == uid:
            return None, "{} already exists.".format(uid)
    user = {
        "uid": uid,
        "display_name": display_name,
        "email": email,
        "password": password,
        "disabled": disabled,
        "groups": sorted(group_ids.intersection(valid_group_ids)),
    }
    if existing_user.get("entry_uuid"):
        user["entry_uuid"] = existing_user.get("entry_uuid")
    return user, ""


def _directory_group_from_form(groups):
    name = request.form.get("name", "").strip().lower()
    description = request.form.get("description", "").strip()
    if not _valid_directory_uid(name):
        return None, "Group name must start with a letter and use letters, numbers, dot, dash, or underscore."
    for group in groups:
        if str(group.get("name", "")).lower() == name:
            return None, "{} already exists.".format(name)
    return {"name": name, "description": description, "members": []}, ""


def _save_directory_items(service, store, users=None, groups=None):
    if users is not None:
        service.settings["users"] = users
    if groups is not None:
        service.settings["groups"] = groups
    users = _directory_items(service, "users")
    groups = _directory_items(service, "groups")
    group_members = {group.get("id"): set(group.get("members", [])) for group in groups}
    for user in users:
        memberships = set(user.get("groups", []))
        memberships.update(group_id for group_id, members in group_members.items() if user.get("id") in members)
        user["groups"] = sorted(memberships)
    for group in groups:
        members = set(group.get("members", []))
        members.update(user.get("id") for user in users if group.get("id") in user.get("groups", []))
        group["members"] = sorted(member for member in members if member)
    service.settings["users"] = users
    service.settings["groups"] = groups
    store.save_service(service)


def _valid_directory_uid(value):
    return re.match(r"^[a-z][a-z0-9._-]{0,63}$", value or "") is not None


def _valid_dn(value):
    if not value or len(value) > 255:
        return False
    parts = [part.strip() for part in value.split(",")]
    if not parts:
        return False
    attr_pattern = re.compile(r"^(dc|cn|ou|uid|o)=[A-Za-z0-9_. -]+$")
    return all(attr_pattern.match(part) is not None for part in parts)


def _valid_base_dn(value):
    if not _valid_dn(value):
        return False
    parts = [part.strip().lower() for part in value.split(",")]
    return len(parts) >= 2 and all(part.startswith("dc=") for part in parts)


def _valid_redirect_url(value):
    if not value or len(value) > 512:
        return False
    if re.search(r"\s", value):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _export_config_profile(manager, fqdn, appliance_ip):
    return {
        "schema": "vis.config.profile/v1",
        "exported_at": utc_now(),
        "appliance": {
            "fqdn": fqdn,
            "ip": appliance_ip,
        },
        "warning": "This profile includes service credentials and tokens. Store it securely before sharing.",
        "services": [_service_export_payload(service) for service in manager.list_services()],
    }


def _service_export_payload(service):
    return {
        "id": service.id,
        "name": service.name,
        "enabled": service.enabled,
        "configured": service.configured,
        "endpoint": service.endpoint,
        "filesystem_root": service.filesystem_root,
        "settings": service.settings,
    }


def _import_config_profile(store, manager, profile, fqdn):
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object.")
    if profile.get("schema") not in ("vis.config.profile/v1", None):
        raise ValueError("Unsupported VIS config profile schema.")
    services = profile.get("services")
    if not isinstance(services, list) or not services:
        raise ValueError("Profile must include a non-empty services list.")
    known = {service.id for service in manager.list_services()}
    imported = []
    for service_payload in services:
        if not isinstance(service_payload, dict):
            raise ValueError("Each service profile entry must be a JSON object.")
        service_id = str(service_payload.get("id", "")).strip()
        if service_id not in known:
            raise ValueError("Unknown service id in profile: {}".format(service_id or "<missing>"))
        settings = service_payload.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("{} settings must be a JSON object.".format(service_id))
        service = manager.get_service(service_id)
        merged_settings = dict(service.settings)
        merged_settings.update(settings)
        service.settings = merged_settings
        service.enabled = bool(service_payload.get("enabled", service.enabled))
        service.configured = bool(service_payload.get("configured", service.configured))
        filesystem_root = str(service_payload.get("filesystem_root", "")).strip()
        if filesystem_root.startswith("/"):
            service.filesystem_root = filesystem_root
        service.health_status = "disabled" if service.configured and not service.enabled else "needs_configuration"
        service.last_validation_result = ValidationResult(False, "Imported configuration; run health check to validate", utc_now())
        service.last_health_check_time = None
        _ensure_service_endpoint(service, store, fqdn)
        store.save_service(service)
        imported.append(service.id)
    return imported


def _valid_local_username(username):
    return re.match(r"^[a-z_][a-z0-9_-]{0,31}$", username) is not None


def _valid_app_username(username):
    return re.match(r"^[A-Za-z0-9_-]{3,32}$", username) is not None


def _run_root_command(command, **kwargs):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, **kwargs)
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or result.stdout.strip() or "Command failed: {}".format(" ".join(command)))
    return result


def _depot_config_dir(app):
    return Path(app.config.get("VIS_DEPOT_CONFIG_DIR") or os.environ.get("VIS_DEPOT_CONFIG_DIR", "/opt/vis/config/depot"))


def _vcfdt_available():
    return shutil.which("vcf-download-tool") is not None


def _vcfdt_system_id(app):
    config_dir = _depot_config_dir(app)
    system_id_path = config_dir / "vcfdt-system-id"
    if system_id_path.is_file():
        value = system_id_path.read_text(encoding="utf-8").strip()
        parsed = _parse_vcfdt_system_id(value)
        return parsed or value
    try:
        config_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        result = subprocess.run(
            ["vcf-download-tool", "configuration", "generate", "--software-depot-id", "--force"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    system_id = _parse_vcfdt_system_id(result.stdout) or result.stdout.strip()
    if system_id:
        system_id_path.write_text(system_id + "\n", encoding="utf-8")
        os.chmod(str(system_id_path), 0o600)
    return system_id


def _parse_vcfdt_system_id(output):
    match = re.search(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", output or "")
    return match.group(0) if match else ""


def _install_vcfdt_archive(app, upload):
    filename = secure_filename(upload.filename or "")
    if not re.match(r"^vcf-download-tool-.+\.tar\.gz$", filename):
        raise OSError("Upload a Broadcom VCF Download Tool archive named vcf-download-tool-*.tar.gz.")
    config_dir = _depot_config_dir(app)
    upload_dir = config_dir / "uploads"
    upload_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    archive_path = upload_dir / "{}-{}".format(uuid.uuid4().hex, filename)
    upload.save(str(archive_path))
    os.chmod(str(archive_path), 0o600)
    try:
        return _install_vcfdt_archive_from_path(app, archive_path)
    finally:
        try:
            archive_path.unlink()
        except FileNotFoundError:
            pass


def _install_vcfdt_archive_from_path(app, archive_path):
    install_root = Path(app.config.get("VCF_DOWNLOAD_TOOL_INSTALL_ROOT") or os.environ.get("VCF_DOWNLOAD_TOOL_INSTALL_ROOT", "/usr/local/lib/vcf-download-tool"))
    bin_dir = Path(app.config.get("VCF_DOWNLOAD_TOOL_BIN_DIR") or os.environ.get("VCF_DOWNLOAD_TOOL_BIN_DIR", "/usr/local/bin"))
    profile_path = Path(app.config.get("VCF_DOWNLOAD_TOOL_PROFILE_PATH") or os.environ.get("VCF_DOWNLOAD_TOOL_PROFILE_PATH", "/etc/profile.d/vis-download-tool.sh"))
    config_dir = _depot_config_dir(app)
    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = Path(tmp)
        result = subprocess.run(
            ["tar", "-xzf", str(archive_path), "-C", str(extract_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or "Unable to extract VCF Download Tool archive.")
        candidates = list(extract_dir.glob("**/bin/vcf-download-tool")) or list(extract_dir.glob("**/vcf-download-tool"))
        candidates = [candidate for candidate in candidates if candidate.is_file()]
        if not candidates:
            raise OSError("Unable to find vcf-download-tool executable in archive.")
        tool_path = candidates[0]
        relative_tool_path = tool_path.relative_to(extract_dir)
        install_root.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        bin_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        if install_root.exists():
            shutil.rmtree(install_root)
        install_root.mkdir(mode=0o755, parents=True, exist_ok=True)
        for entry in extract_dir.iterdir():
            destination = install_root / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination, symlinks=True)
            else:
                shutil.copy2(entry, destination, follow_symlinks=False)
        installed_tool = install_root / relative_tool_path
        installed_tool.chmod(installed_tool.stat().st_mode | 0o111)
        link_path = bin_dir / "vcf-download-tool"
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(installed_tool)
    profile_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    profile_path.write_text('export PATH="/usr/local/bin:$PATH"\n', encoding="utf-8")
    os.chmod(str(profile_path), 0o644)
    telemetry_path = install_root / "conf/obtu_telemetry/obtu-telemetry.properties"
    if telemetry_path.is_file():
        text = telemetry_path.read_text(encoding="utf-8")
        if re.search(r"^obtu\.telemetry\.ceip=", text, flags=re.MULTILINE):
            text = re.sub(r"^obtu\.telemetry\.ceip=.*$", "obtu.telemetry.ceip=ENABLE", text, flags=re.MULTILINE)
        else:
            text = text.rstrip() + "\nobtu.telemetry.ceip=ENABLE\n"
        telemetry_path.write_text(text, encoding="utf-8")
    telemetry_flag_path = install_root / "conf/telemetry/telemetry.flag"
    telemetry_flag_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    telemetry_flag_path.write_text("obtu.telemetry.config=ENABLE\n", encoding="utf-8")
    os.chmod(str(telemetry_flag_path), 0o644)
    config_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    result = subprocess.run(
        ["vcf-download-tool", "configuration", "generate", "--software-depot-id", "--force"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or result.stdout.strip() or "Unable to generate VCFDT System ID.")
    system_id = _parse_vcfdt_system_id(result.stdout) or result.stdout.strip()
    if not system_id:
        raise OSError("Unable to parse VCFDT System ID from vcf-download-tool output.")
    system_id_path = config_dir / "vcfdt-system-id"
    system_id_path.write_text(system_id + "\n", encoding="utf-8")
    os.chmod(str(system_id_path), 0o600)
    return system_id


def _write_depot_download_credential(app, download_mode, secret):
    config_dir = _depot_config_dir(app)
    config_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    filename = "activation-code"
    credential_path = config_dir / filename
    temp_path = config_dir / "{}.tmp".format(filename)
    temp_path.write_text(secret.strip() + "\n", encoding="utf-8")
    os.chmod(str(temp_path), 0o600)
    temp_path.replace(credential_path)
    os.chmod(str(credential_path), 0o600)
    return str(credential_path)


def _verify_depot_download_credential(download_mode, credential_path, depot_store):
    command = [
        "vcf-download-tool",
        "metadata",
        "download",
        "--depot-store={}".format(depot_store),
        "--depot-download-activation-code-file={}".format(credential_path),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=900,
        )
    except FileNotFoundError:
        raise OSError("vcf-download-tool is not installed on this appliance.")
    except subprocess.TimeoutExpired:
        raise OSError("VCF Download Tool metadata validation timed out.")
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "VCF Download Tool metadata validation failed."
        raise OSError(_friendly_vcfdt_error(message))
    prod_dir = Path(depot_store) / "PROD"
    if not prod_dir.exists():
        raise OSError("VCF Download Tool completed but did not create {}.".format(prod_dir))
    return utc_now_text()


def _friendly_vcfdt_error(message):
    compact = " ".join((message or "").split())
    if len(compact) > 500:
        compact = compact[:497] + "..."
    return "VCF Download Tool validation failed: {}".format(compact or "Unknown error")


ESX_PLATFORM_IDS = (
    "embeddedEsx-6.7-INTL",
    "embeddedEsx-7.0-INTL",
    "embeddedEsx-8.0-INTL",
    "embeddedEsx-9.0-INTL",
    "embeddedEsx-9.1-INTL",
    "esxio-8.0-INTL",
    "esxio-9.0-INTL",
    "esxio-9.1-INTL",
    "armEsx-9.1-INTL",
)


def _depot_download_paths(app):
    state_dir = Path(app.config.get("VIS_STATE_DIR") or os.environ.get("VIS_STATE_DIR", "/opt/vis/state"))
    return {
        "dir": state_dir,
        "state": state_dir / "depot-download-job.json",
        "log": state_dir / "depot-download-job.log",
    }


def _depot_download_job(app):
    paths = _depot_download_paths(app)
    state = _read_depot_download_state(paths["state"])
    if state.get("status") == "running" and state.get("pid") and not _pid_running(int(state["pid"])):
        state.update(
            {
                "status": "failed",
                "message": "Download process stopped unexpectedly.",
                "completed_at": utc_now_text(),
            }
        )
        _write_depot_download_state(paths["state"], state)
    return state


def _start_depot_binary_download(app, service, form):
    current_job = _depot_download_job(app)
    if current_job.get("status") == "running":
        raise OSError("A Software Depot download is already running.")
    if service.settings.get("download_mode") != "activation_code":
        raise OSError("Configure and validate an Activation Code before downloading binaries.")
    credential_path = service.settings.get("download_credential_path", "")
    if not credential_path or not Path(credential_path).is_file():
        raise OSError("The saved depot credential file is missing. Save Download Configuration again.")

    sku = form.get("sku", "").strip().upper()
    if sku not in ("VVF", "VCF"):
        raise OSError("SKU must be VVF or VCF.")
    version = form.get("vcf_version", "").strip()
    if not re.match(r"^\d+\.\d+(?:\.\d+)?(?:\.\d+)?$", version):
        raise OSError("Version must use x.y, x.y.z, or x.y.z.a format, such as 9.1, 9.1.0, or 9.1.0.0.")
    selected_types = [item.upper() for item in form.getlist("download_type")]
    selected_types = [item for item in selected_types if item in ("INSTALL", "UPGRADE", "ESX_PATCHES")]
    if not selected_types:
        raise OSError("Select Install, Upgrade, ESX Patches, or a combination.")
    binary_types = [item for item in selected_types if item in ("INSTALL", "UPGRADE")]
    if binary_types and not re.match(r"^\d+\.\d+\.\d+(?:\.\d+)?$", version):
        raise OSError("Install and Upgrade downloads require x.y.z or x.y.z.a version format, such as 9.1.0 or 9.1.0.0.")
    include_dayn = {
        "INSTALL": form.get("include_dayn_install") == "on",
        "UPGRADE": form.get("include_dayn_upgrade") == "on",
    }
    commands = []
    for download_type in binary_types:
        command = [
            "vcf-download-tool",
            "binaries",
            "download",
            "--depot-store=/opt/vis/data/depot",
            "--depot-download-activation-code-file={}".format(credential_path),
            "--sku={}".format(sku),
            "--vcf-version={}".format(version),
            "--type={}".format(download_type),
        ]
        if not include_dayn.get(download_type):
            command.append("--automated-install")
        commands.append(command)
    if "ESX_PATCHES" in selected_types:
        esx_config_path = _write_esx_user_config(version)
        commands.append(
            [
                "vcf-download-tool",
                "esx",
                "download",
                "--depot-store=/opt/vis/data/depot/",
                "--depot-download-activation-code-file={}".format(credential_path),
            ]
        )
    else:
        esx_config_path = ""

    paths = _depot_download_paths(app)
    paths["dir"].mkdir(mode=0o750, parents=True, exist_ok=True)
    state = {
        "id": str(uuid.uuid4()),
        "status": "running",
        "message": "Starting Software Depot binary download.",
        "pid": None,
        "sku": sku,
        "vcf_version": version,
        "types": selected_types,
        "include_dayn": {key.lower(): value for key, value in include_dayn.items() if key in binary_types},
        "commands": commands,
        "esx_config_path": esx_config_path,
        "log_path": str(paths["log"]),
        "started_at": utc_now_text(),
        "completed_at": "",
        "return_code": None,
    }
    _write_depot_download_state(paths["state"], state)
    worker_payload = {
        "state_path": str(paths["state"]),
        "log_path": str(paths["log"]),
        "state": state,
        "commands": commands,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _depot_download_worker_code(), json.dumps(worker_payload)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    state["pid"] = process.pid
    state["message"] = "Software Depot binary download is running."
    _write_depot_download_state(paths["state"], state)
    return state


def _write_esx_user_config(version, path="/usr/local/lib/vcf-download-tool/conf/esxUserConfig.json"):
    desired = "embeddedEsx-{}-INTL".format(_esx_major_minor(version))
    disabled = [platform for platform in ESX_PLATFORM_IDS if platform != desired]
    config_path = Path(path)
    config_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temp_path = config_path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump({"disabledPlatforms": disabled}, handle, indent=2)
        handle.write("\n")
    os.replace(str(temp_path), str(config_path))
    return str(config_path)


def _esx_major_minor(version):
    parts = version.split(".")
    return ".".join(parts[:2])


def _cancel_depot_binary_download(app):
    paths = _depot_download_paths(app)
    state = _depot_download_job(app)
    if state.get("status") != "running":
        raise OSError("There is no running Software Depot download to cancel.")
    pid = state.get("pid")
    cancel_warning = ""
    if pid:
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            cancel_warning = "Software depot download was already stopped. VIS marked the job as cancelled."
        except OSError as err:
            raise OSError("Unable to cancel Software Depot download: {}".format(err))
    else:
        cancel_warning = "Software depot download had no active process identifier. VIS marked the job as cancelled."

    state.update(
        {
            "status": "cancelled",
            "message": "Software Depot binary download was cancelled.",
            "cancel_warning": cancel_warning,
            "completed_at": utc_now_text(),
            "return_code": -signal.SIGTERM,
        }
    )
    _write_depot_download_state(paths["state"], state)
    return state


def _read_depot_download_state(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return {"status": "idle", "message": "No Software Depot download has been started."}


def _write_depot_download_state(path, state):
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with open(str(temp_path), "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(str(temp_path), str(path))


def _pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _depot_download_worker_code():
    return r'''
import datetime
import json
import os
import subprocess
import sys

payload = json.loads(sys.argv[1])
state_path = payload["state_path"]
log_path = payload["log_path"]
state = payload["state"]

def now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def write_state(update):
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            current = json.load(handle)
    except (FileNotFoundError, ValueError):
        current = dict(state)
    current.update(update)
    temp_path = state_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=2, sort_keys=True)
    os.replace(temp_path, state_path)

def command_label(command):
    if command[:3] == ["vcf-download-tool", "esx", "download"]:
        return "ESX_PATCHES"
    return next((part.split("=", 1)[1] for part in command if part.startswith("--type=")), "UNKNOWN")

def extract_failure_message(log_path, fallback):
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-140:]
    except OSError:
        return fallback
    markers = ("ERROR:", "REMEDY:", "ERROR ", "Caused by:", "Unauthorized", "not entitled", "invalid", "failed")
    found = []
    for line in lines:
        stripped = " ".join(line.strip().split())
        if stripped and any(marker.lower() in stripped.lower() for marker in markers):
            found.append(stripped)
    if not found:
        return fallback
    message = " ".join(found[-4:])
    return message[:700] + ("..." if len(message) > 700 else "")

os.makedirs(os.path.dirname(log_path), exist_ok=True)
return_code = 0
for command in payload["commands"]:
    download_type = command_label(command)
    task_name = "ESX patches" if download_type == "ESX_PATCHES" else "{} binaries".format(download_type)
    write_state({"status": "running", "message": "Downloading {}.".format(task_name)})
    with open(log_path, "ab") as log:
        log.write(("\n\n[{}] Running: {}\n".format(now(), " ".join(command))).encode("utf-8"))
        log.flush()
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    return_code = result.returncode
    if return_code != 0:
        detail = extract_failure_message(log_path, "Review the log for details.")
        write_state({
            "status": "failed",
            "message": "Software Depot download failed while downloading {}. {}".format(task_name, detail),
            "completed_at": now(),
            "return_code": return_code,
        })
        sys.exit(return_code)

write_state({
    "status": "succeeded",
    "message": "Software Depot binary download completed.",
    "completed_at": now(),
    "return_code": return_code,
})
'''


def utc_now_text():
    from .models import utc_now

    return utc_now()


def _configure_local_sftp(username, password, service):
    chroot = "/opt/vis/data/sftp"
    backup_dir = service.filesystem_root
    _run_root_command(["install", "-d", "-o", "root", "-g", "root", "-m", "755", chroot])
    if subprocess.run(["id", username], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode != 0:
        _run_root_command(["useradd", "--home-dir", "/backup", "--shell", "/usr/sbin/nologin", "--no-create-home", username])
    _run_root_command(["usermod", "--home", "/backup", "--shell", "/usr/sbin/nologin", username])
    _run_root_command(["install", "-d", "-o", username, "-g", username, "-m", "750", backup_dir])
    _run_root_command(["chpasswd"], input="{}:{}\n".format(username, password))
    os.makedirs("/etc/ssh/sshd_config.d", exist_ok=True)
    Path("/etc/ssh/sshd_config.d/99-vis-sftp.conf").write_text(
        "\n".join(
            [
                "Match User {}".format(username),
                "    ChrootDirectory {}".format(chroot),
                "    ForceCommand internal-sftp -d /backup",
                "    PasswordAuthentication yes",
                "    AllowTcpForwarding no",
                "    X11Forwarding no",
                "    PermitTunnel no",
                "",
            ]
        )
    )
    _run_root_command(["sshd", "-t"])
    subprocess.run(["systemctl", "reload", "ssh"], check=False)


TLS_SERVICE_IDS = ("web-depot", "harbor-registry", "ldap-provider", "oidc-provider", "kms-service")


def _shared_tls_paths():
    tls_dir = Path("/opt/vis/config/tls")
    return {
        "dir": tls_dir,
        "ca_key": tls_dir / "rootCA.key",
        "ca_pem": tls_dir / "rootCA.pem",
        "server_key": tls_dir / "server.key",
        "server_csr": tls_dir / "server.csr",
        "server_crt": tls_dir / "server.crt",
        "san_conf": tls_dir / "server-san.cnf",
        "full_pem": tls_dir / "vis-full.pem",
    }


def _apply_shared_tls_settings(service):
    paths = _shared_tls_paths()
    service.settings.update(
        {
            "tls_mode": "shared",
            "tls_ca_path": str(paths["ca_pem"]),
            "tls_cert_path": str(paths["server_crt"]),
            "tls_key_path": str(paths["server_key"]),
            "tls_full_pem_path": str(paths["full_pem"]),
        }
    )


def _ensure_shared_tls(app):
    paths = _shared_tls_paths()
    required = (paths["ca_pem"], paths["server_crt"], paths["server_key"], paths["full_pem"])
    if all(path.is_file() for path in required):
        return
    _generate_shared_tls(app.config["VIS_APPLIANCE_FQDN"], app.config["VIS_APPLIANCE_IP"])


def _apply_shared_tls_to_services(manager, store):
    for service_id in TLS_SERVICE_IDS:
        try:
            service = manager.get_service(service_id)
        except KeyError:
            continue
        _apply_shared_tls_settings(service)
        store.save_service(service)


def _write_harbor_config(service, fqdn):
    harbor_yml = Path("/opt/vis/harbor/harbor.yml")
    if not harbor_yml.is_file():
        raise OSError("Harbor configuration file is missing at {}".format(harbor_yml))
    harbor_yml.write_text(_render_harbor_config(harbor_yml.read_text(), service, fqdn))
    prepare = subprocess.run(
        ["/opt/vis/harbor/prepare"],
        cwd="/opt/vis/harbor",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if prepare.returncode != 0:
        raise OSError(prepare.stderr.strip() or prepare.stdout.strip() or "Unable to prepare Harbor configuration")
    compose_file = Path("/opt/vis/harbor/docker-compose.yml")
    if not compose_file.is_file():
        install = subprocess.run(
            ["/opt/vis/harbor/install.sh"],
            cwd="/opt/vis/harbor",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            raise OSError(install.stderr.strip() or install.stdout.strip() or "Unable to install Harbor")
    _ensure_harbor_unit()


def _render_harbor_config(source, service, fqdn):
    tls_enabled = bool(service.settings.get("tls_enabled") or service.settings.get("protocol") == "https")
    protocol = "https" if tls_enabled else "http"
    port = int(service.settings.get("port", 9443 if tls_enabled else 9080))
    admin_password = str(service.settings.get("admin_password", ""))
    external_url = "{}://{}:{}/".format(protocol, fqdn, port)
    data_volume = service.filesystem_root
    lines = source.splitlines()
    output = []
    skip_https_block = False
    in_http_block = False
    in_https_block = False
    replaced_external_url = False
    for line in lines:
        stripped = line.strip()
        if line and not line.startswith(" ") and not line.startswith("#"):
            in_http_block = stripped.startswith("http:")
            if not stripped.startswith("https:"):
                in_https_block = False
        if stripped.startswith("https:"):
            in_http_block = False
            in_https_block = True
            if not tls_enabled:
                skip_https_block = True
                continue
        if skip_https_block:
            if line and not line.startswith(" ") and not line.startswith("#"):
                skip_https_block = False
                in_https_block = False
            else:
                continue
        if stripped.startswith("hostname:"):
            output.append("hostname: {}".format(fqdn))
        elif in_http_block and stripped.startswith("port:"):
            output.append("  port: 9080")
        elif in_https_block and stripped.startswith("port:"):
            output.append("  port: {}".format(port))
        elif in_https_block and stripped.startswith("certificate:"):
            output.append("  certificate: {}".format(service.settings.get("tls_cert_path", "/opt/vis/config/tls/server.crt")))
        elif in_https_block and stripped.startswith("private_key:"):
            output.append("  private_key: {}".format(service.settings.get("tls_key_path", "/opt/vis/config/tls/server.key")))
        elif stripped.startswith("harbor_admin_password:") and admin_password:
            output.append("harbor_admin_password: {}".format(admin_password))
        elif stripped.startswith("data_volume:"):
            output.append("data_volume: {}".format(data_volume))
        elif stripped.startswith("external_url:") or stripped.startswith("# external_url:"):
            output.append("external_url: {}".format(external_url.rstrip("/")))
            replaced_external_url = True
        else:
            output.append(line)
    if tls_enabled and not any(line.strip().startswith("https:") for line in output):
        output.extend(
            [
                "https:",
                "  port: {}".format(port),
                "  certificate: {}".format(service.settings.get("tls_cert_path", "/opt/vis/config/tls/server.crt")),
                "  private_key: {}".format(service.settings.get("tls_key_path", "/opt/vis/config/tls/server.key")),
            ]
        )
    if not replaced_external_url:
        output.append("external_url: {}".format(external_url.rstrip("/")))
    return "\n".join(output) + "\n"


def _ensure_harbor_unit():
    unit = Path("/etc/systemd/system/vis-harbor.service")
    unit.write_text(
        """[Unit]
Description=VIS Harbor container registry
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/vis/harbor
ExecStart=/usr/bin/docker compose -f /opt/vis/harbor/docker-compose.yml up -d
ExecStop=/usr/bin/docker compose -f /opt/vis/harbor/docker-compose.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
"""
    )
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def _tls_service_status(manager):
    services = []
    for service_id in TLS_SERVICE_IDS:
        try:
            service = manager.get_service(service_id)
        except KeyError:
            continue
        services.append(
            {
                "id": service.id,
                "name": service.name,
                "enabled": bool(service.settings.get("tls_enabled") or service.settings.get("protocol") == "https"),
                "protocol": service.settings.get("protocol", ""),
                "mode": service.settings.get("tls_mode", "shared"),
                "cert_path": service.settings.get("tls_cert_path", ""),
                "ca_path": service.settings.get("tls_ca_path", ""),
                "endpoint": service.endpoint,
                "port": service.settings.get("port", ""),
            }
        )
    return services


def _certificate_status():
    paths = _shared_tls_paths()
    cert_exists = paths["server_crt"].is_file()
    key_exists = paths["server_key"].is_file()
    ca_exists = paths["ca_pem"].is_file()
    full_exists = paths["full_pem"].is_file()
    return {
        "configured": cert_exists and key_exists,
        "ca_configured": ca_exists,
        "full_pem_available": full_exists,
        "mode": "Shared VIS Certificate",
        "ca_path": str(paths["ca_pem"]),
        "cert_path": str(paths["server_crt"]),
        "key_path": str(paths["server_key"]),
        "full_pem_path": str(paths["full_pem"]),
        "san_config": str(paths["san_conf"]),
        "subject": _openssl_value(paths["server_crt"], ["openssl", "x509", "-noout", "-subject", "-in", str(paths["server_crt"])]),
        "issuer": _openssl_value(paths["server_crt"], ["openssl", "x509", "-noout", "-issuer", "-in", str(paths["server_crt"])]),
        "expires": _openssl_value(paths["server_crt"], ["openssl", "x509", "-noout", "-enddate", "-in", str(paths["server_crt"])]),
        "fingerprint": _openssl_value(paths["server_crt"], ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(paths["server_crt"])]),
        "sans": _openssl_value(paths["server_crt"], ["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(paths["server_crt"])]),
        "issued_count": len(_issued_certificates()),
    }


def _issued_certificates_dir():
    return _shared_tls_paths()["dir"] / "issued"


def _certificate_safe_name(name):
    name = secure_filename(name or "")
    name = name.strip(".-_")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$", name or ""):
        raise OSError("Certificate name must be 2-64 characters using letters, numbers, dots, dashes, or underscores.")
    return name


def _issued_certificate_dir(name):
    safe_name = _certificate_safe_name(name)
    issued_dir = _issued_certificates_dir().resolve()
    cert_dir = (issued_dir / safe_name).resolve()
    if issued_dir not in cert_dir.parents:
        raise OSError("Certificate path escapes issued certificate directory.")
    return cert_dir


def _issued_certificates():
    issued_dir = _issued_certificates_dir()
    if not issued_dir.is_dir():
        return []
    certificates = []
    for cert_dir in sorted(path for path in issued_dir.iterdir() if path.is_dir()):
        cert_path = cert_dir / "certificate.pem"
        metadata_path = cert_dir / "metadata.json"
        metadata = {}
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metadata = {}
        certificates.append(
            {
                "name": cert_dir.name,
                "common_name": metadata.get("common_name", cert_dir.name),
                "san_dns": metadata.get("san_dns", []),
                "san_ips": metadata.get("san_ips", []),
                "created_at": metadata.get("created_at", ""),
                "days": metadata.get("days", ""),
                "expires": _openssl_value(cert_path, ["openssl", "x509", "-noout", "-enddate", "-in", str(cert_path)]),
                "fingerprint": _openssl_value(cert_path, ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(cert_path)]),
            }
        )
    return certificates


def _issue_certificate(name, common_name, san_dns_text, san_ips_text, days):
    paths = _shared_tls_paths()
    if not paths["ca_pem"].is_file() or not paths["ca_key"].is_file():
        raise OSError("Initialize the VIS Certificate Authority before issuing certificates.")
    safe_name = _certificate_safe_name(name)
    common_name = (common_name or safe_name).strip()
    if not common_name or len(common_name) > 253:
        raise OSError("Common Name is required and must be 253 characters or fewer.")
    if days < 1 or days > 3650:
        raise OSError("Validity days must be between 1 and 3650.")
    san_dns = _certificate_dns_entries(san_dns_text)
    san_ips = _certificate_ip_entries(san_ips_text)
    if common_name not in san_dns and not _looks_like_ip(common_name):
        san_dns.insert(0, common_name)
    if _looks_like_ip(common_name) and common_name not in san_ips:
        san_ips.insert(0, common_name)

    cert_dir = _issued_certificate_dir(safe_name)
    temp_dir = Path(tempfile.mkdtemp(prefix="vis-issued-cert-"))
    try:
        temp_key = temp_dir / "private-key.pem"
        temp_csr = temp_dir / "request.csr"
        temp_crt = temp_dir / "certificate.pem"
        temp_conf = temp_dir / "san.cnf"
        san_entries = ["DNS:{}".format(value) for value in san_dns] + ["IP:{}".format(value) for value in san_ips]
        temp_conf.write_text(
            "[req]\ndistinguished_name=req_distinguished_name\n[req_distinguished_name]\n[v3_req]\nsubjectAltName={}\n".format(
                ",".join(san_entries)
            ),
            encoding="utf-8",
        )
        commands = [
            ["openssl", "genrsa", "-out", str(temp_key), "2048"],
            ["openssl", "req", "-new", "-key", str(temp_key), "-subj", "/CN={}".format(common_name), "-out", str(temp_csr)],
            ["openssl", "x509", "-req", "-in", str(temp_csr), "-CA", str(paths["ca_pem"]), "-CAkey", str(paths["ca_key"]), "-CAcreateserial", "-out", str(temp_crt), "-days", str(days), "-sha256", "-extfile", str(temp_conf), "-extensions", "v3_req"],
        ]
        for command in commands:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if result.returncode != 0:
                raise OSError(result.stderr.strip() or "Unable to issue certificate")
        cert_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(temp_key), str(cert_dir / "private-key.pem"))
        shutil.copyfile(str(temp_crt), str(cert_dir / "certificate.pem"))
        with (cert_dir / "full-chain.pem").open("w", encoding="utf-8") as handle:
            handle.write(temp_crt.read_text(encoding="utf-8"))
            handle.write("\n")
            handle.write(paths["ca_pem"].read_text(encoding="utf-8"))
        (cert_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "name": safe_name,
                    "common_name": common_name,
                    "san_dns": san_dns,
                    "san_ips": san_ips,
                    "days": days,
                    "created_at": utc_now(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.chmod(str(cert_dir / "private-key.pem"), 0o600)
        os.chmod(str(cert_dir / "certificate.pem"), 0o644)
        os.chmod(str(cert_dir / "full-chain.pem"), 0o644)
    finally:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
    return {"name": safe_name, "common_name": common_name, "san_dns": san_dns, "san_ips": san_ips}


def _certificate_dns_entries(raw):
    entries = []
    for value in _lines(raw):
        value = value.rstrip(".")
        if not re.match(r"^[A-Za-z0-9*][A-Za-z0-9*_.-]{0,252}$", value):
            raise OSError("{} is not a valid DNS SAN entry.".format(value))
        if value not in entries:
            entries.append(value)
    return entries


def _certificate_ip_entries(raw):
    entries = []
    for value in _lines(raw):
        try:
            ipaddress.ip_address(value)
        except ValueError:
            raise OSError("{} is not a valid IP SAN entry.".format(value))
        if value not in entries:
            entries.append(value)
    return entries


def _lines(raw):
    return [line.strip() for line in str(raw or "").replace(",", "\n").splitlines() if line.strip()]


def _looks_like_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _openssl_value(path, command):
    if not path.is_file():
        return "Not configured"
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        return result.stderr.strip() or "Unable to inspect certificate"
    value = result.stdout.strip()
    for prefix in ("subject=", "issuer=", "notAfter=", "sha256 Fingerprint="):
        if value.startswith(prefix):
            return value[len(prefix):]
    value = value.replace("X509v3 Subject Alternative Name:", "").strip()
    return value


def _log_targets(manager):
    targets = {
        "vis-web": {
            "id": "vis-web",
            "name": "VIS Web UI",
            "description": "Management application logs.",
            "units": ["vis-web.service"],
            "files": [],
        }
    }
    service_units = {
        "web-depot": ["vis-depot.service"],
        "sftp-backup": ["ssh.service"],
        "harbor-registry": ["vis-harbor.service", "docker.service", "containerd.service"],
        "ldap-provider": ["vis-ldap.service", "slapd.service"],
        "oidc-provider": ["vis-identity.service", "docker.service", "containerd.service"],
        "unbound-dns": ["unbound.service"],
        "time-server": ["chrony.service", "vis-ptp4l.service"],
        "dhcp-server": ["vis-dhcp.service"],
        "kms-service": ["vis-kms.service"],
    }
    for service in manager.list_services():
        targets[service.id] = {
            "id": service.id,
            "name": service.name,
            "description": service.description,
            "units": service_units.get(service.id, []),
            "files": _log_files_for_service(service.id),
        }
    return targets


def _log_files_for_service(service_id):
    if service_id == "web-depot":
        return [
            "/opt/vis/state/depot-download-job.log",
            "/usr/local/lib/vcf-download-tool/log/vdt.log",
        ]
    return []


def _read_logs(target, lines):
    sections = []
    units = target.get("units", [])
    files = target.get("files", [])
    if units:
        sections.append("== Journal: {} ==\n{}".format(", ".join(units), _read_journal(units, lines)))
    for path in files:
        sections.append("== File: {} ==\n{}".format(path, _read_log_file(path, lines)))
    if sections:
        return "\n\n".join(sections)
    return "No journal unit or log file has been assigned for this service yet."


def _read_journal(units, lines):
    if not units:
        return "No journal unit has been assigned for this service yet."
    command = ["journalctl", "--no-pager", "--utc", "-n", str(lines)]
    for unit in units:
        command.extend(["-u", unit])
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Unable to read journal logs."
        return message
    return result.stdout.strip() or "No log entries found for selected service."


def _read_log_file(path, lines):
    log_path = Path(path)
    if not log_path.exists():
        return "Log file does not exist yet."
    if not log_path.is_file():
        return "Log path is not a regular file."
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.readlines()
    except OSError as err:
        return "Unable to read log file: {}".format(err)
    return "".join(content[-lines:]).strip() or "Log file is empty."


def _filter_log_text(log_text, query="", level="all"):
    query = (query or "").strip().lower()
    level = (level or "all").strip().lower()
    if not query and level == "all":
        return log_text
    markers = {
        "error": ("error", "failed", "failure", "critical", "panic", "exception"),
        "warning": ("warn", "warning", "deprecated"),
        "info": ("info", "started", "stopped", "accepted", "listening", "running"),
    }
    filtered = []
    for line in log_text.splitlines():
        lowered = line.lower()
        if query and query not in lowered:
            continue
        if level != "all" and not any(marker in lowered for marker in markers.get(level, ())):
            continue
        filtered.append(line)
    if filtered:
        return "\n".join(filtered)
    return "No log entries matched the selected filters."


def _system_health(manager):
    services = manager.list_services()
    storage_services = [
        ("root", "Disk 1", "Operating System", "/", 40, "#607d8b"),
        ("web-depot", "Disk 2", "Software Depot", _storage_mount(services, "web-depot"), 200, "#49afd9"),
        ("sftp-backup", "Disk 3", "SFTP Backup", _storage_mount(services, "sftp-backup"), 15, "#80c45c"),
        ("harbor-registry", "Disk 4", "Container Registry", _storage_mount(services, "harbor-registry"), 60, "#a98df2"),
        ("unbound-dns", "Disk 5", "DNS Server", _storage_mount(services, "unbound-dns"), 2, "#f0b84f"),
        ("identity-providers", "Disk 6", "Identity Providers", "/opt/vis/data/identity", 2, "#5ad1c8"),
    ]
    return {
        "cpu": _cpu_health(),
        "memory": _memory_health(),
        "storage": [_storage_health(*item) for item in storage_services],
    }


def _service_root(services, service_id):
    for service in services:
        if service.id == service_id:
            return service.filesystem_root
    return ""


def _storage_mount(services, service_id):
    overrides = {
        "sftp-backup": "/opt/vis/data/sftp",
    }
    return overrides.get(service_id, _service_root(services, service_id))


def _cpu_health():
    load_1, load_5, load_15 = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    cores = os.cpu_count() or 1
    percent = min(100, round((load_1 / cores) * 100))
    return {
        "percent": percent,
        "cores": cores,
        "load_1": "{:.2f}".format(load_1),
        "load_5": "{:.2f}".format(load_5),
        "load_15": "{:.2f}".format(load_15),
    }


def _memory_health():
    meminfo = _read_meminfo()
    total = meminfo.get("MemTotal", 0)
    available = meminfo.get("MemAvailable", 0)
    cached = meminfo.get("Cached", 0) + meminfo.get("Buffers", 0)
    used = max(0, total - available)
    percent = round((used / total) * 100) if total else 0
    cache_percent = round((cached / total) * 100) if total else 0
    available_percent = max(0, 100 - percent)
    return {
        "percent": percent,
        "cache_percent": cache_percent,
        "available_percent": available_percent,
        "total": _format_bytes(total * 1024),
        "used": _format_bytes(used * 1024),
        "cache": _format_bytes(cached * 1024),
        "available": _format_bytes(available * 1024),
    }


def _read_meminfo():
    values = {}
    try:
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
    except (FileNotFoundError, ValueError, OSError):
        return values
    return values


def _storage_health(service_id, disk_label, name, mount, vmdk_capacity_gib, color):
    try:
        usage = shutil.disk_usage(mount)
        total = usage.total
        free = usage.free
        used = usage.used
        present = True
    except (FileNotFoundError, OSError):
        total = free = used = 0
        present = False
    percent = round((used / total) * 100) if total else 0
    if percent >= 90:
        status = "Critical"
        status_class = "bad"
    elif percent >= 75:
        status = "Watch"
        status_class = "warn"
    elif present:
        status = "OK"
        status_class = "good"
    else:
        status = "Missing"
        status_class = "bad"
    detected_capacity_bytes = _mounted_disk_capacity_bytes(mount)
    capacity_bytes = detected_capacity_bytes or int(vmdk_capacity_gib * 1024 * 1024 * 1024)
    capacity_gib = round(capacity_bytes / (1024 * 1024 * 1024), 1) if capacity_bytes else vmdk_capacity_gib
    return {
        "id": service_id,
        "disk_label": disk_label,
        "name": name,
        "mount": mount,
        "vmdk_capacity_gib": capacity_gib,
        "vmdk_capacity": "{} GiB VMDK".format(_format_gib(capacity_gib)),
        "vmdk_capacity_source": "detected" if detected_capacity_bytes else "configured",
        "color": color,
        "present": present,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "total": _format_bytes(total),
        "used": _format_bytes(used),
        "free": _format_bytes(free),
        "percent": percent,
        "free_percent": max(0, 100 - percent),
        "status": status,
        "status_class": status_class,
    }


def _mounted_disk_capacity_bytes(mountpoint):
    source = _findmnt_source(mountpoint)
    if not source:
        return 0
    source_type = _lsblk_value(source, "TYPE")
    if source_type == "lvm":
        pv_devices = _lvm_pv_devices(source)
        capacities = [_device_parent_capacity_bytes(device) for device in pv_devices]
        return max(capacities) if capacities else 0
    return _device_parent_capacity_bytes(source)


def _findmnt_source(mountpoint):
    try:
        result = subprocess.run(
            ["findmnt", "-no", "SOURCE", "-T", mountpoint],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def _device_parent_capacity_bytes(device):
    device_type = _lsblk_value(device, "TYPE")
    if device_type == "part":
        parent = _lsblk_value(device, "PKNAME")
        if parent:
            return _lsblk_size_bytes("/dev/{}".format(parent))
    return _lsblk_size_bytes(device)


def _lsblk_size_bytes(device):
    try:
        result = subprocess.run(["lsblk", "-b", "-dn", "-o", "SIZE", device], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError:
        return 0
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    try:
        return int(result.stdout.strip().splitlines()[0].strip())
    except ValueError:
        return 0


def _format_gib(value):
    numeric = float(value or 0)
    if numeric.is_integer():
        return str(int(numeric))
    return "{:.1f}".format(numeric)


def _expand_filesystem(mountpoint):
    if os.geteuid() != 0:
        return {"ok": False, "message": "Filesystem expansion requires the VIS web service to run as root."}
    findmnt = subprocess.run(
        ["findmnt", "-no", "SOURCE,FSTYPE", "-T", mountpoint],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if findmnt.returncode != 0 or not findmnt.stdout.strip():
        return {"ok": False, "message": "Unable to identify mounted filesystem for {}.".format(mountpoint)}
    parts = findmnt.stdout.strip().split()
    source = parts[0]
    fstype = parts[1] if len(parts) > 1 else ""

    output = []
    source_type = _lsblk_value(source, "TYPE")
    if source_type == "lvm":
        return _expand_lvm_filesystem(source, fstype, mountpoint)

    grow_result = _grow_partition_for_device(source)
    if grow_result:
        output.extend(grow_result)
    if fstype in ("ext2", "ext3", "ext4"):
        output.append(_run_expansion_command(["resize2fs", source]))
    elif fstype == "xfs":
        output.append(_run_expansion_command(["xfs_growfs", mountpoint]))
    elif fstype == "btrfs":
        output.append(_run_expansion_command(["btrfs", "filesystem", "resize", "max", mountpoint]))
    else:
        return {"ok": False, "message": "Unsupported filesystem type {} for {}.".format(fstype or "unknown", mountpoint)}
    return {"ok": True, "message": "Filesystem refreshed for {}. {}".format(mountpoint, " | ".join(output))}


def _expand_lvm_filesystem(source, fstype, mountpoint):
    output = []
    pv_devices = _lvm_pv_devices(source)
    if not pv_devices:
        return {"ok": False, "message": "Unable to identify LVM physical volume for {}.".format(mountpoint)}
    for pv_device in pv_devices:
        grow_result = _grow_partition_for_device(pv_device)
        if grow_result:
            output.extend(grow_result)
        output.append(_run_expansion_command(["pvresize", pv_device]))
    if fstype not in ("ext2", "ext3", "ext4", "xfs"):
        return {"ok": False, "message": "Unsupported filesystem type {} for {}.".format(fstype or "unknown", mountpoint)}
    output.append(_run_expansion_command(["lvextend", "-r", "-l", "+100%FREE", source], tolerate_no_change=True))
    return {"ok": True, "message": "Filesystem refreshed for {}. {}".format(mountpoint, " | ".join(output))}


def _run_expansion_command(command, tolerate_no_change=False):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    message = result.stdout.strip() or result.stderr.strip() or "completed"
    summary = "{}: {}".format(" ".join(command), message)
    if result.returncode != 0:
        no_change_markers = ("NOCHANGE", "matches existing size", "not larger than existing size", "Insufficient free space")
        if tolerate_no_change and any(marker in message for marker in no_change_markers):
            return summary
        raise OSError("Expansion failed. {}".format(summary))
    return summary


def _lsblk_value(device, column):
    try:
        result = subprocess.run(["lsblk", "-no", column, device], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def _grow_partition_for_device(device):
    parent = _lsblk_value(device, "PKNAME")
    part_number = _lsblk_value(device, "PARTN")
    if not parent or not part_number:
        return []
    _rescan_disk(parent)
    return [_run_expansion_command(["growpart", "/dev/{}".format(parent), part_number], tolerate_no_change=True)]


def _rescan_disk(disk_name):
    rescan_path = Path("/sys/class/block") / disk_name / "device" / "rescan"
    try:
        rescan_path.write_text("1\n")
    except OSError:
        pass


def _lvm_pv_devices(source):
    result = subprocess.run(["lvs", "--noheadings", "-o", "devices", source], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        return []
    devices = []
    for token in result.stdout.replace(",", " ").split():
        match = re.match(r"(/dev/[^\s(]+)", token)
        if match and match.group(1) not in devices:
            devices.append(match.group(1))
    return devices


def _format_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return "{} {}".format(int(value), unit)
            return "{:.1f} {}".format(value, unit)
        value /= 1024


def _update_paths(app):
    state_dir = Path(app.config.get("VIS_STATE_DIR") or os.environ.get("VIS_STATE_DIR", "/opt/vis/state"))
    return {
        "state_dir": state_dir,
        "status": state_dir / "vis-update-status.json",
        "log": state_dir / "vis-update.log",
        "script": Path(app.config.get("VIS_UPDATE_SCRIPT") or os.environ.get("VIS_UPDATE_SCRIPT", "/usr/local/sbin/vis-update")),
        "offline_script": Path(app.config.get("VIS_OFFLINE_UPDATE_SCRIPT") or os.environ.get("VIS_OFFLINE_UPDATE_SCRIPT", "/usr/local/sbin/vis-offline-update")),
        "signing_key": Path(app.config.get("VIS_UPDATE_PUBLIC_KEY") or os.environ.get("VIS_UPDATE_PUBLIC_KEY", "/etc/vis/update-signing.pub")),
        "upload_dir": state_dir / "update-uploads",
    }


def _read_update_status(app):
    paths = _update_paths(app)
    default = {
        "state": "idle",
        "message": "No update has been run on this appliance.",
        "repo_url": app.config.get("VIS_UPDATE_REPO_URL") or os.environ.get("VIS_UPDATE_REPO_URL", "https://github.com/lamw/vcf-infrastructure-service-appliance.git"),
        "branch": app.config.get("VIS_UPDATE_BRANCH") or os.environ.get("VIS_UPDATE_BRANCH", "main"),
        "commit": "",
        "updated_at": "",
    }
    try:
        with paths["status"].open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return default
    default.update({key: str(payload.get(key, default[key]) or "") for key in default})
    return default


def _read_update_log(app, max_lines=120):
    paths = _update_paths(app)
    try:
        lines = paths["log"].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def _valid_update_repo_url(value):
    if not value:
        return False
    if value.startswith(("https://github.com/", "git@github.com:")):
        return True
    if value.startswith(("file://", "/")):
        return True
    return False


def _valid_git_ref(value):
    return bool(re.match(r"^[A-Za-z0-9._/-]{1,128}$", value or ""))


def _stage_offline_update_uploads(app, archive, checksum, signature):
    paths = _update_paths(app)
    paths["upload_dir"].mkdir(mode=0o700, parents=True, exist_ok=True)
    batch_dir = Path(tempfile.mkdtemp(prefix="offline-", dir=str(paths["upload_dir"])))
    files = []
    for upload, expected_suffix, label in (
        (archive, ".zip", "release ZIP archive"),
        (checksum, ".sha256", "release SHA256 file"),
        (signature, ".sig", "release signature file"),
    ):
        filename = secure_filename(upload.filename or "")
        if not filename:
            raise OSError("Offline update {} has an invalid filename.".format(label))
        if not filename.lower().endswith(expected_suffix):
            raise OSError("Offline update {} must use a {} file.".format(label, expected_suffix))
        target = batch_dir / filename
        upload.save(str(target))
        target.chmod(0o600)
        files.append(target)
    return tuple(files)


def _start_update(app, repo_url, branch):
    paths = _update_paths(app)
    if not paths["script"].exists():
        raise OSError("VIS update script is not installed at {}.".format(paths["script"]))
    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    command = [str(paths["script"]), "--repo-url", repo_url, "--branch", branch]
    env = os.environ.copy()
    env.update(
        {
            "VIS_UPDATE_REPO_URL": repo_url,
            "VIS_UPDATE_BRANCH": branch,
            "VIS_UPDATE_STATE_DIR": str(paths["state_dir"]),
            "VIS_UPDATE_LOG_FILE": str(paths["log"]),
            "VIS_UPDATE_STATUS_FILE": str(paths["status"]),
        }
    )
    _launch_update_command(app, "vis-update", command, env)


def _start_offline_update(app, archive_path, checksum_path, signature_path):
    paths = _update_paths(app)
    if not paths["offline_script"].exists():
        raise OSError("VIS offline update script is not installed at {}.".format(paths["offline_script"]))
    if not paths["signing_key"].exists():
        raise OSError("VIS update signing public key is not installed at {}.".format(paths["signing_key"]))
    paths["state_dir"].mkdir(parents=True, exist_ok=True)
    command = [
        str(paths["offline_script"]),
        "--archive",
        str(archive_path),
        "--sha256",
        str(checksum_path),
        "--signature",
        str(signature_path),
        "--public-key",
        str(paths["signing_key"]),
    ]
    env = os.environ.copy()
    env.update(
        {
            "VIS_UPDATE_STATE_DIR": str(paths["state_dir"]),
            "VIS_UPDATE_LOG_FILE": str(paths["log"]),
            "VIS_UPDATE_STATUS_FILE": str(paths["status"]),
            "VIS_UPDATE_PUBLIC_KEY": str(paths["signing_key"]),
        }
    )
    _launch_update_command(app, "vis-offline-update", command, env)


def _launch_update_command(app, unit_prefix, command, env):
    if not app.config.get("TESTING") and shutil.which("systemd-run"):
        run_env = {
            key: env[key]
            for key in (
                "VIS_UPDATE_REPO_URL",
                "VIS_UPDATE_BRANCH",
                "VIS_UPDATE_STATE_DIR",
                "VIS_UPDATE_LOG_FILE",
                "VIS_UPDATE_STATUS_FILE",
                "VIS_UPDATE_PUBLIC_KEY",
            )
            if key in env
        }
        unit_name = "{}-{}".format(unit_prefix, uuid.uuid4().hex[:8])
        systemd_command = [
            "systemd-run",
            "--unit={}".format(unit_name),
            "--collect",
            "--property=Type=exec",
        ]
        for key, value in sorted(run_env.items()):
            systemd_command.append("--setenv={}={}".format(key, value))
        systemd_command.extend(command)
        result = subprocess.run(systemd_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise OSError(detail or "Unable to start VIS update job with systemd-run.")
        return

    with open(os.devnull, "wb") as devnull:
        process = subprocess.Popen(command, stdout=devnull, stderr=devnull, env=env, start_new_session=True, close_fds=True)
    if app.config.get("TESTING"):
        process.wait(timeout=5)


def _generate_shared_tls(fqdn, appliance_ip):
    paths = _shared_tls_paths()
    paths["dir"].mkdir(parents=True, exist_ok=True)
    san_entries = ["DNS:{}".format(fqdn)]
    if appliance_ip:
        san_entries.append("IP:{}".format(appliance_ip))
    paths["san_conf"].write_text(
        "[req]\ndistinguished_name=req_distinguished_name\n[req_distinguished_name]\n[v3_req]\nsubjectAltName={}\n".format(
            ",".join(san_entries)
        )
    )
    commands = [
        ["openssl", "genrsa", "-out", str(paths["ca_key"]), "4096"],
        ["openssl", "req", "-x509", "-new", "-nodes", "-key", str(paths["ca_key"]), "-sha256", "-days", "3650", "-subj", "/CN=VIS Root CA", "-out", str(paths["ca_pem"])],
        ["openssl", "genrsa", "-out", str(paths["server_key"]), "2048"],
        ["openssl", "req", "-new", "-key", str(paths["server_key"]), "-subj", "/CN={}".format(fqdn), "-out", str(paths["server_csr"])],
        ["openssl", "x509", "-req", "-in", str(paths["server_csr"]), "-CA", str(paths["ca_pem"]), "-CAkey", str(paths["ca_key"]), "-CAcreateserial", "-out", str(paths["server_crt"]), "-days", "825", "-sha256", "-extfile", str(paths["san_conf"]), "-extensions", "v3_req"],
    ]
    for command in commands:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or "Unable to generate TLS certificate")
    with paths["full_pem"].open("w") as handle:
        handle.write(paths["server_crt"].read_text())
        handle.write("\n")
        handle.write(paths["ca_pem"].read_text())
    paths["ca_key"].chmod(0o600)
    paths["server_key"].chmod(0o600)


def _upload_shared_tls(cert, key, ca):
    paths = _shared_tls_paths()
    paths["dir"].mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="vis-tls-upload-"))
    temp_cert = temp_dir / "server.crt"
    temp_key = temp_dir / "server.key"
    temp_ca = temp_dir / "rootCA.pem"
    cert.save(str(temp_cert))
    key.save(str(temp_key))
    if ca and ca.filename:
        ca.save(str(temp_ca))
    else:
        temp_ca.write_text("")
    try:
        _validate_certificate_upload(temp_cert, temp_key, temp_ca)
        shutil.copyfile(str(temp_cert), str(paths["server_crt"]))
        shutil.copyfile(str(temp_key), str(paths["server_key"]))
        shutil.copyfile(str(temp_ca), str(paths["ca_pem"]))
    finally:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
    with paths["full_pem"].open("w") as handle:
        handle.write(paths["server_crt"].read_text())
        if paths["ca_pem"].stat().st_size > 0:
            handle.write("\n")
            handle.write(paths["ca_pem"].read_text())
    paths["server_key"].chmod(0o600)


def _validate_certificate_upload(cert_path, key_path, ca_path):
    checks = [
        (["openssl", "x509", "-in", str(cert_path), "-noout"], "Uploaded certificate is not a valid PEM certificate."),
        (["openssl", "pkey", "-in", str(key_path), "-noout"], "Uploaded private key is not a valid PEM private key."),
    ]
    if ca_path.stat().st_size > 0:
        checks.append((["openssl", "x509", "-in", str(ca_path), "-noout"], "Uploaded root CA is not a valid PEM certificate."))
    for command, message in checks:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode != 0:
            raise OSError(message)
    cert_modulus = _openssl_digest(["openssl", "x509", "-noout", "-modulus", "-in", str(cert_path)])
    key_modulus = _openssl_digest(["openssl", "rsa", "-noout", "-modulus", "-in", str(key_path)])
    if cert_modulus and key_modulus and cert_modulus != key_modulus:
        raise OSError("Uploaded certificate and private key do not match.")


def _openssl_digest(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
