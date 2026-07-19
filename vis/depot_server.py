import base64
import os
import ssl
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class DepotRequestHandler(SimpleHTTPRequestHandler):
    def do_AUTHHEAD(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="VIS Software Depot"')
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self.do_AUTHHEAD()
            self.wfile.write(b"Authentication required")
            return
        super().do_GET()

    def do_HEAD(self):
        if not self._authorized():
            self.do_AUTHHEAD()
            return
        super().do_HEAD()

    def _authorized(self):
        username = os.environ.get("VIS_DEPOT_AUTH_USER", "")
        password = os.environ.get("VIS_DEPOT_AUTH_PASSWORD", "")
        if not username:
            return True
        expected = "Basic " + base64.b64encode("{}:{}".format(username, password).encode()).decode()
        return self.headers.get("Authorization") == expected


def main():
    root = os.environ.get("VIS_DEPOT_ROOT", "/opt/vis/data/depot")
    host = os.environ.get("VIS_DEPOT_HOST", "0.0.0.0")
    port = int(os.environ.get("VIS_DEPOT_PORT", "8081"))
    protocol = os.environ.get("VIS_DEPOT_PROTOCOL", "http").lower()
    os.chdir(root)

    httpd = ThreadingHTTPServer((host, port), DepotRequestHandler)
    if protocol == "https":
        certfile = os.environ.get("VIS_DEPOT_TLS_CERT")
        keyfile = os.environ.get("VIS_DEPOT_TLS_KEY")
        if not certfile or not keyfile:
            raise SystemExit("VIS_DEPOT_TLS_CERT and VIS_DEPOT_TLS_KEY are required for HTTPS")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

    httpd.serve_forever()


if __name__ == "__main__":
    main()
