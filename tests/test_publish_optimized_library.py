import importlib.util
import json
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "publish_optimized_library.py"
SPEC = importlib.util.spec_from_file_location("publish_optimized_library", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeClient:
    def __init__(self, manifest=None, existing=()):
        self.manifest = manifest
        self.existing = set(existing)
        self.get_calls = 0
        self.head_calls = 0

    def get(self, key, allow_not_found=False):
        self.get_calls += 1
        if self.manifest is None:
            return None
        return json.dumps(self.manifest).encode("utf-8")

    def exists(self, key):
        self.head_calls += 1
        return key in self.existing


class PublishedAssetIndexTest(unittest.TestCase):
    def test_mutable_compatibility_index_is_not_refetched_every_minute(self):
        self.assertGreaterEqual(MODULE.MUTABLE_INDEX_MAX_AGE_SECONDS, 3600)

    def test_release_pointer_stays_small_and_fresh(self):
        self.assertLessEqual(MODULE.RELEASE_POINTER_MAX_AGE_SECONDS, 300)

    def test_cloud_index_replaces_per_asset_head_scan(self):
        keys = [f"assets/covers/{index}.webp" for index in range(2500)]
        client = FakeClient({"schema": 1, "keys": keys})
        result = MODULE.resolve_published_keys(client, keys)
        self.assertEqual(result, set(keys))
        self.assertEqual(client.get_calls, 1)
        self.assertEqual(client.head_calls, 0)

    def test_missing_index_migrates_with_one_head_per_asset(self):
        keys = ["assets/covers/a.webp", "assets/previews/b.mp3"]
        client = FakeClient(None, existing=[keys[0]])
        result = MODULE.resolve_published_keys(client, keys)
        self.assertEqual(result, {keys[0]})
        self.assertEqual(client.get_calls, 1)
        self.assertEqual(client.head_calls, 2)


if __name__ == "__main__":
    unittest.main()
