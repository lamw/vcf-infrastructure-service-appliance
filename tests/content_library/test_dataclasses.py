import unittest
from pathlib import Path

from vis.content_library.dataclasses import ContentLibraryItemsList, ContentLibraryConfig

testdata = Path(__file__).parent / "testdata"


class DataclassTest(unittest.TestCase):
    def test_items_marshalling(self):
        json_file = testdata / "items.json"
        item_list = ContentLibraryItemsList.from_json(json_file.read_text())  # ty: ignore[unresolved-attribute]

        self.assertEqual(len(item_list.items), 126)

    def test_from_env(self):
        env = {
            "VIS_CONTENT_LIB_ROOT": "/usr/local/../bin",
            "VIS_CONTENT_LIB_HOST": "0.0.0.0",
            "VIS_CONTENT_LIB_PORT": "12345",
            "VIS_CONTENT_LIB_PARALLEL_SOURCE_SYNC": "n",
        }

        cfg = ContentLibraryConfig.from_env(env)
        self.assertEqual(f"{cfg.root.resolve()}", "/usr/bin")
        self.assertEqual(cfg.port, 12345)
        self.assertTrue(cfg.auto_source_sync_enabled)
        self.assertFalse(cfg.parallel_source_sync)
