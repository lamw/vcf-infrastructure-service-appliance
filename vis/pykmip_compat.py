"""Compatibility launcher for PyKMIP on newer Python runtimes."""

from __future__ import annotations

import ssl


def _install_ssl_wrap_socket_compat() -> None:
    if hasattr(ssl, "wrap_socket"):
        return

    def wrap_socket(
        sock,
        keyfile=None,
        certfile=None,
        server_side=False,
        cert_reqs=ssl.CERT_NONE,
        ssl_version=ssl.PROTOCOL_TLS,
        ca_certs=None,
        do_handshake_on_connect=True,
        suppress_ragged_eofs=True,
        ciphers=None,
    ):
        context = ssl.SSLContext(ssl_version)
        context.verify_mode = cert_reqs
        if ca_certs:
            context.load_verify_locations(ca_certs)
        if certfile or keyfile:
            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        if ciphers:
            context.set_ciphers(ciphers)
        return context.wrap_socket(
            sock,
            server_side=server_side,
            do_handshake_on_connect=do_handshake_on_connect,
            suppress_ragged_eofs=suppress_ragged_eofs,
        )

    ssl.wrap_socket = wrap_socket


def main() -> int:
    _install_ssl_wrap_socket_compat()
    from kmip.services.server.server import main as pykmip_main

    return pykmip_main()


if __name__ == "__main__":
    raise SystemExit(main())
