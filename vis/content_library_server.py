from vis.content_library import initialize_content_library_fs
import logging
from vis.content_library.sync import get_sync_stats, sync_with_source
import os
import ssl
from http.server import ThreadingHTTPServer

from vis.content_library.dataclasses import ContentLibraryConfig
from vis.content_library.server import ContentLibraryRequestHandler

if __name__ == "__main__":
    config = ContentLibraryConfig.from_env()
    initialize_content_library_fs(config)

    stats = get_sync_stats(config)
    if stats.total_sync_count == 0:
        logging.info("content library has never been synchronized, attempting now")
        sync_with_source(config)

    os.chdir(config.lib_path)
    httpd = ThreadingHTTPServer(
        (config.host, config.port),
        ContentLibraryRequestHandler.with_config(config),
    )
    if config.protocol == "https":
        if not (config.tls_cert and config.tls_key):
            raise SystemExit(
                "VIS_CONTENT_LIB_TLS_CERT and VIS_CONTENT_LIB_TLS_KEY are required for HTTPS"
            )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=config.tls_cert, keyfile=config.tls_key
        )
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

    httpd.serve_forever()
