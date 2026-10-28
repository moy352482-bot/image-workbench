"""The retired local Qwen feature must not be shipped or mounted."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.web import create_app
from tests.http_contract import snapshot


class RetiredQwenTests(unittest.TestCase):
    def test_retired_page_and_api_routes_are_not_registered(self):
        for routes in snapshot().values():
            self.assertFalse(any("/qwen" in route for route in routes))

    def test_retired_static_assets_are_unavailable_and_navigation_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(StorageManager(StorageConfig(data_root=Path(directory) / "data")), backend="local")
            with TestClient(app, base_url="http://localhost") as client:
                for name in ("qwen.js", "qwen.css"):
                    self.assertEqual(client.get("/static/" + name).status_code, 404)
        templates = Path(__file__).resolve().parents[1] / "image_workbench/web_templates"
        for name in ("index.html", "scripts.html", "video.html"):
            self.assertNotIn("/qwen-test", (templates / name).read_text(encoding="utf-8"))
