import atexit
import os
from asyncio import run

from vis.content_library import initialize_content_library_fs
from vis.content_library.dataclasses import ContentLibraryConfig
from vis.content_library.logging import initialize_clean_logging, new_sync_logger
from vis.content_library.sync import run_sync


async def main():
    initialize_clean_logging()
    logger = new_sync_logger()
    config = ContentLibraryConfig.from_env()
    initialize_content_library_fs(config)

    lock_file = config.root / ".sync-in-progress"
    if lock_file.exists():
        raise SystemExit("another sync already in progress")

    lock_file.touch()
    atexit.register(os.remove, lock_file)
    try:
        await run_sync(config, logger)
    except BaseException as e:
        logger.error("error running sync", exc_info=e)


if __name__ == "__main__":
    run(main())
