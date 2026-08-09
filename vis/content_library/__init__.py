import os
from datetime import datetime, timezone

from .dataclasses import ContentLibraryConfig


def initialize_content_library_fs(config: ContentLibraryConfig):
    os.makedirs(config.lib_path, exist_ok=True, mode=0o755)
    os.makedirs(config.cache_path, exist_ok=True, mode=0o755)
