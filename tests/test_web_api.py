from __future__ import annotations

import unittest

from tests.astrbot_stubs import FakeRequest, install_astrbot_stubs

install_astrbot_stubs()

from canvasforge.web_api import WebAPI


class FakeContext:
    def __init__(self) -> None:
        self.routes: list[tuple[str, object, list[str], str]] = []

    def register_web_api(self, path, handler, methods, description) -> None:
        self.routes.append((path, handler, methods, description))


class FakeCache:
    limit = 3


class WebAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeRequest.username = "administrator"
        self.context = FakeContext()
        self.api = WebAPI(
            self.context,
            FakeCache(),
            lambda: {"model": "custom-image-model"},
            lambda _settings: 0,
            "v0.1.7",
        )

    def test_register_exposes_no_custom_update_routes(self) -> None:
        self.api.register()
        paths = {route[0] for route in self.context.routes}

        self.assertIn("/astrbot_plugin_canvasforge/settings", paths)
        self.assertFalse(
            any("/update/" in path for path in paths),
            paths,
        )

    async def test_settings_response_keeps_plugin_version(self) -> None:
        response = await self.api.get_settings()

        self.assertEqual(200, response.status_code)
        self.assertEqual("v0.1.7", response.payload["plugin_version"])
        self.assertEqual("custom-image-model", response.payload["model"])


if __name__ == "__main__":
    unittest.main()
