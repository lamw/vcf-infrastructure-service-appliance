import json
import unittest
from pathlib import Path
from sys import stdout
from tempfile import mkdtemp

from vis.content_library import initialize_content_library_fs
from vis.content_library.dataclasses import ContentLibraryConfig
from vis.content_library.sync import sync_with_source


class ContentLibrarySyncTest(unittest.TestCase):
    def test_sync(self):
        root = Path(mkdtemp())
        config = ContentLibraryConfig(root=root, auto_source_sync_enabled=False, parallel_source_sync=True)
        initialize_content_library_fs(config)

        print(root)
        files = sync_with_source(config)
        self.assertEqual(len(files), 632)

        json.dump(files, indent=4, fp=stdout)
