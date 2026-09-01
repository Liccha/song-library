import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "source_asset_preflight.py"


class SourceAssetPreflightTest(unittest.TestCase):
    def load_module(self):
        specification = importlib.util.spec_from_file_location("source_asset_preflight", SCRIPT)
        module = importlib.util.module_from_spec(specification)
        assert specification.loader is not None
        specification.loader.exec_module(module)
        return module

    def test_unmaterialized_cloud_pointer_blocks_publication(self):
        preflight = self.load_module()
        rows = [{
            "id": "1287",
            "image_path": "cloud-object:mobile-library/assets/image/1287/a.jpg",
            "audio_path": "",
        }]
        with self.assertRaisesRegex(RuntimeError, "1287:image_path"):
            preflight.validate_source_assets(rows, Path("C:/cache"))

    def test_missing_managed_cache_file_blocks_publication(self):
        preflight = self.load_module()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            missing = cache / "image" / "1287" / "a.jpg"
            rows = [{"id": "1287", "image_path": str(missing), "audio_path": ""}]
            with self.assertRaisesRegex(RuntimeError, "1287:image_path"):
                preflight.validate_source_assets(rows, cache)

    def test_complete_managed_assets_pass(self):
        preflight = self.load_module()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            image = cache / "image" / "1287" / "a.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            rows = [{"id": "1287", "image_path": str(image), "audio_path": ""}]
            preflight.validate_source_assets(rows, cache)


if __name__ == "__main__":
    unittest.main()
