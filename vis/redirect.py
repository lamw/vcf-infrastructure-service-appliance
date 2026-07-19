import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class VISRedirectHandler(BaseHTTPRequestHandler):
    server_version = "VISRedirect/1.0"

    def do_GET(self):
        self._redirect()

    def do_HEAD(self):
        self._redirect()

    def _redirect(self):
        target_port = os.environ.get("VIS_TARGET_PORT", "8080")
        host = self.headers.get("Host", "")
        hostname = host.split(":", 1)[0] if host else "localhost"
        location = f"http://{hostname}:{target_port}{self.path}"

        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def main():
    listen_port = int(os.environ.get("VIS_REDIRECT_PORT", "80"))
    server = ThreadingHTTPServer(("0.0.0.0", listen_port), VISRedirectHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
