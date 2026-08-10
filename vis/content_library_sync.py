from sys import argv, stdout
import os
import logging
from vis.content_library import initialize_content_library_fs
from vis.content_library.dataclasses import ContentLibraryConfig
from vis.content_library.sync import SyncManager 


if __name__ == "__main__":
    config = ContentLibraryConfig.from_env()
    initialize_content_library_fs(config)

    main_file = os.path.realpath(argv[0]) if argv[0] else "__sync__"

    logging.basicConfig(stream=stdout, level=logging.DEBUG)
    logging.info("starting sync manager")

    manager = SyncManager(config)

    cmd = "sync"
    if len(argv) > 1:
        match argv[1]:
            case x if x in ["sync", "stats"]:
                cmd = x
            case _:
                raise SystemExit("if a command is passed it must be 'sync' or 'stats'")

    if cmd == "sync":
        manager.run()
    else:
        manager.print_sync_stats()
