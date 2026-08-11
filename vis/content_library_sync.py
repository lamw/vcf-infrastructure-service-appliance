from asyncio import run

from vis.content_library import initialize_content_library_fs
from vis.content_library.dataclasses import ContentLibraryConfig
from vis.content_library.sync import run_sync


async def main():
    config = ContentLibraryConfig.from_env()
    initialize_content_library_fs(config)

    await run_sync(config)


if __name__ == "__main__":
    run(main())
