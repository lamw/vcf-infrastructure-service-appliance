import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vis import proxy
from vis.web import create_app, _verify_depot_download_credential, _depot_download_worker_code


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "VIS_DB_PATH": str(self.root / "vis.db"),
                               "VIS_STATE_DIR": str(self.root / "state")})
        self.client = self.app.test_client()
        self.settings = dict(enabled=True, protocol="http", server="proxy.example.com", port=3128,
                             username="alice@example.com", password="p@ss:'$ word", no_proxy="localhost,.vcf.lab")

    def tearDown(self):
        self.temp.cleanup()

    def save(self, settings=None):
        data = dict(settings or self.settings)
        data["enabled"] = "on" if data["enabled"] else ""
        return self.client.post("/outbound-proxy", data=data)

    def test_save_persistence_export_import_and_optional_credentials(self):
        self.assertEqual(302, self.save().status_code)
        store = self.app.config["service_manager"].store
        store.initialize()
        self.assertEqual(self.settings, store.get_appliance_setting("outbound_proxy"))
        profile = json.loads(self.client.get("/config/export").get_data(as_text=True))
        self.assertEqual(self.settings, profile["appliance"]["outbound_proxy"])
        env_path = self.root / "state/proxy.env"
        self.assertEqual(0o600, stat.S_IMODE(env_path.stat().st_mode))
        output = subprocess.check_output(["bash", "-c", 'source "$1"; printf "%s" "$HTTPS_PROXY"', "bash", str(env_path)], text=True)
        self.assertEqual(proxy.environment(self.settings, {})["HTTPS_PROXY"], output)
        profile["appliance"]["outbound_proxy"].update(username="", password="")
        response = self.client.post("/config/import", data={"profile_json": json.dumps(profile)})
        self.assertEqual(302, response.status_code)
        self.assertEqual("", store.get_appliance_setting("outbound_proxy")["password"])
        self.assertNotIn("alice", env_path.read_text())
        self.assertFalse((env_path.parent / "password").exists())

    def test_invalid_fields_do_not_save_or_execute_shell(self):
        for changes in ({"server": "http://bad:80"}, {"server": "x;touch /tmp/bad"}, {"port": "65536"},
                        {"password": "line\nbreak"}, {"username": ""}):
            settings = dict(self.settings, **changes)
            self.assertEqual(200, self.save(settings).status_code)
            self.assertIsNone(self.app.config["service_manager"].store.get_appliance_setting("outbound_proxy"))
        self.assertEqual(200, self.client.get("/outbound-proxy").status_code)

    def test_direct_environment_and_https_options(self):
        direct = proxy.environment(proxy.defaults(), {"HTTP_PROXY": "bad", "all_proxy": "bad", "PATH": "test"})
        self.assertEqual({"PATH": "test"}, direct)
        settings = dict(self.settings, protocol="https", server="::1")
        args = proxy.cli_args(settings, "/private/password")
        self.assertIn("--proxy-server=[::1]:3128", args)
        self.assertIn("--proxy-https", args)
        self.assertIn("--proxy-user-password-file=/private/password", args)
        self.assertNotIn(self.settings["password"], " ".join(args))

    def test_metadata_passes_proxy_and_cleans_password_file(self):
        self.save()
        (self.root / "PROD").mkdir()
        captured = []
        def run(command, **kwargs):
            args = [arg for arg in command if arg.startswith("--proxy-user-password-file=")]
            password_file = Path(args[0].split("=", 1)[1])
            captured.append(password_file)
            self.assertEqual(self.settings["password"] + "\n", password_file.read_text())
            self.assertEqual(0o600, stat.S_IMODE(password_file.stat().st_mode))
            self.assertIn("--proxy-server=proxy.example.com:3128", command)
            self.assertEqual("localhost,.vcf.lab", kwargs["env"]["NO_PROXY"])
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch("vis.web.subprocess.run", side_effect=run):
            _verify_depot_download_credential("activation_code", "activation", str(self.root), self.app)
        self.assertFalse(captured[0].exists())

    def test_binary_and_esx_jobs_snapshot_proxy_without_password_in_argv_or_state(self):
        self.save()
        manager = self.app.config["service_manager"]
        service = manager.get_service("web-depot")
        credential = self.root / "activation"
        credential.write_text("activation")
        service.settings.update(download_mode="activation_code", download_credential_path=str(credential))
        manager.store.save_service(service)
        with patch("vis.web.subprocess.Popen") as launch, patch("vis.web._write_esx_user_config", return_value="unused"):
            launch.return_value.pid = 9999999
            response = self.client.post("/services/web-depot/download", data={"sku": "VCF", "vcf_version": "9.1.0", "download_type": ["INSTALL", "UPGRADE", "ESX_PATCHES"]})
        self.assertEqual(302, response.status_code)
        argv = launch.call_args[0][0]
        payload = json.loads(Path(argv[3]).read_text())
        self.assertNotIn(self.settings["password"], " ".join(argv))
        self.assertNotIn(self.settings["password"], json.dumps(payload["state"]))
        for command in payload["commands"]:
            self.assertIn("--proxy-server=proxy.example.com:3128", command)
        self.save(dict(self.settings, enabled=False))
        self.assertIn("proxy.example.com", payload["environment"]["HTTP_PROXY"])

    def test_worker_redacts_output_and_cleans_job_secrets(self):
        job_dir = self.root / "job"
        job_dir.mkdir()
        payload_path = job_dir / "payload.json"
        state_path = self.root / "state.json"
        log_path = self.root / "download.log"
        payload = dict(state_path=str(state_path), log_path=str(log_path), state={}, job_dir=str(job_dir),
                       commands=[[sys.executable, "-c", "import os; print(os.environ['TEST_SECRET'])"]],
                       environment=dict(os.environ, TEST_SECRET=self.settings["password"]), redact=[self.settings["password"]])
        proxy.write_private(payload_path, json.dumps(payload))
        subprocess.run([sys.executable, "-c", _depot_download_worker_code(), str(payload_path)], check=True)
        self.assertFalse(job_dir.exists())
        self.assertNotIn(self.settings["password"], log_path.read_text())
        self.assertIn("********", log_path.read_text())
        self.assertEqual("succeeded", json.loads(state_path.read_text())["status"])

    def test_update_uses_saved_proxy_environment(self):
        self.save()
        script = self.root / "update"
        script.touch()
        self.app.config["VIS_UPDATE_SCRIPT"] = str(script)
        with patch("vis.web._launch_update_command") as launch:
            response = self.client.post("/updates/run", data={"repo_url": "https://github.com/lamw/vcf-infrastructure-service-appliance.git", "branch": "main"})
        self.assertEqual(302, response.status_code)
        env = launch.call_args[0][3]
        self.assertEqual(proxy.environment(self.settings, {})["HTTPS_PROXY"], env["HTTPS_PROXY"])
        self.assertEqual(str(self.root / "state/proxy.env"), env["VIS_PROXY_ENV_FILE"])
