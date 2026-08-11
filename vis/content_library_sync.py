from vis.content_library.logging import new_sync_logger, initialize_clean_logging
from asyncio import run

from vis.content_library import initialize_content_library_fs
from vis.content_library.dataclasses import ContentLibraryConfig
from vis.content_library.sync import run_sync


async def main():
    initialize_clean_logging()
    logger = new_sync_logger()
    config = ContentLibraryConfig.from_env()
    initialize_content_library_fs(config)

    try:
        await run_sync(config, logger)
    except BaseException as e:
        logger.error(f"error running sync", exc_info=e)


if __name__ == "__main__":
    run(main())
