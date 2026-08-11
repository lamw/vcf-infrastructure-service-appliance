import os

from .dataclasses import ContentLibraryConfig
from .sync import is_sync_in_progress, get_sync_stats, run_sync

__all__ = ["is_sync_in_progress", "get_sync_stats", "run_sync"]


def initialize_content_library_fs(config: ContentLibraryConfig):
    os.makedirs(config.lib_path, exist_ok=True, mode=0o755)
    os.makedirs(config.cache_path, exist_ok=True, mode=0o755)
