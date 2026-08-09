from base64 import b64encode
from http.server import SimpleHTTPRequestHandler

from .dataclasses import ContentLibraryConfig

class ContentLibraryRequestHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        request,
        client_address,
        server,
        *,
        config,
        directory=None,
    ) -> None:
        super().__init__(request, client_address, server, directory=directory)
        self.__expected_auth_header: str | None = (
            b64encode(
                f"{config.auth_user}:{config.auth_password}".encode()
            ).decode()
            if config and config.auth_user and config.auth_password
            else None
        )

    def do_AUTHHEAD(self):
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate", 'Basic realm="vSphere Content Library"'
        )
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
        return (not self.__expected_auth_header) or (
            self.headers.get("Authorization") == self.__expected_auth_header
        )

    @classmethod
    def with_config(cls, config: ContentLibraryConfig):
        def ctor(req, addr, srv):
            return ContentLibraryRequestHandler(req, addr, srv, config=config)

        return ctor
