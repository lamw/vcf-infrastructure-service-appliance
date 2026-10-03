"""Outbound proxy settings shared by downloads and appliance updates."""

import ipaddress
import os
import re
import shlex
import tempfile
from pathlib import Path
from urllib.parse import quote


PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "ALL_PROXY", "all_proxy")


def defaults(fqdn="", ip=""):
    return dict(enabled=False, protocol="http", server="", port=8080, username="", password="",
                no_proxy=",".join(filter(None, ("localhost", "127.0.0.1", "::1", fqdn, ip))))


def validate(settings):
    if not isinstance(settings, dict):
        raise ValueError("Outbound proxy settings must be a JSON object.")
    result = defaults()
    result.update({key: settings[key] for key in result if key in settings})
    if not isinstance(result["enabled"], bool):
        raise ValueError("Proxy enabled must be true or false.")
    if result["protocol"] not in ("http", "https"):
        raise ValueError("Proxy protocol must be HTTP or HTTPS.")
    for key in ("server", "username", "password", "no_proxy"):
        if not isinstance(result[key], str) or any(ord(c) < 32 or ord(c) == 127 for c in result[key]):
            raise ValueError("Proxy fields must be text without control characters.")
    result["server"] = result["server"].strip()
    result["no_proxy"] = ",".join(part.strip() for part in result["no_proxy"].split(",") if part.strip())
    try:
        result["port"] = int(result["port"])
    except (TypeError, ValueError):
        raise ValueError("Proxy port must be between 1 and 65535.")
    if not 1 <= result["port"] <= 65535:
        raise ValueError("Proxy port must be between 1 and 65535.")
    host = result["server"]
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.rstrip(".").split(".")):
                raise ValueError("Proxy server must be a hostname or IP address without a scheme, path, or port.")
    if result["enabled"] and not host:
        raise ValueError("Proxy server is required when the proxy is enabled.")
    if result["password"] and not result["username"]:
        raise ValueError("Enter a proxy username when specifying a password.")
    return result


def server_address(settings):
    host = settings["server"]
    return "{}:{}".format("[{}]".format(host) if ":" in host else host, settings["port"])


def environment(settings, base=None, masked=False):
    env = dict(os.environ if base is None else base)
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    if settings["enabled"]:
        auth = ""
        if settings["username"]:
            password = "********" if masked and settings["password"] else settings["password"]
            auth = quote(settings["username"], safe="") + ":" + quote(password, safe="*" if masked else "") + "@"
        endpoint = settings["protocol"] + "://" + auth + server_address(settings)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = endpoint
        env["NO_PROXY"] = env["no_proxy"] = settings["no_proxy"]
    return env


def cli_args(settings, password_path=None):
    if not settings["enabled"]:
        return []
    args = ["--proxy-server=" + server_address(settings)]
    if settings["protocol"] == "https":
        args.append("--proxy-https")
    if settings["username"]:
        args.append("--proxy-user=" + settings["username"])
        args.append("--proxy-user-password-file=" + str(password_path or "/opt/vis/config/proxy/password"))
    return args


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_environment(path, settings):
    env = environment(settings, {})
    lines = ["unset " + " ".join(PROXY_ENV_KEYS)]
    lines.extend("export {}={}".format(key, shlex.quote(value)) for key, value in sorted(env.items()))
    write_private(path, "\n".join(lines) + "\n")
    password_path = Path(path).parent / "password"
    if settings["enabled"] and settings["username"]:
        write_private(password_path, settings["password"] + "\n")
    elif password_path.exists():
        password_path.unlink()
