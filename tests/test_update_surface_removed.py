from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpdateSurfaceRemovalTests(unittest.TestCase):
    def test_custom_updater_module_and_http_routes_are_removed(self) -> None:
        self.assertFalse(
            (ROOT / "canvasforge" / "update.py").exists(),
            "the retired CanvasForge updater module must be deleted",
        )

        web_api = (ROOT / "canvasforge" / "web_api.py").read_text(
            encoding="utf-8",
        )
        for route in ("/update/check", "/update/apply", "/update/status"):
            self.assertNotIn(route, web_api)
        self.assertNotIn("UpdateCoordinator", web_api)

    def test_page_has_no_update_controls_or_update_requests(self) -> None:
        index = (ROOT / "pages" / "canvasforge" / "index.html").read_text(
            encoding="utf-8",
        )
        app = (ROOT / "pages" / "canvasforge" / "app.js").read_text(
            encoding="utf-8",
        )
        styles = (ROOT / "pages" / "canvasforge" / "style.css").read_text(
            encoding="utf-8",
        )

        for retired_text in (
            "/update/check",
            "/update/apply",
            "/update/status",
            "update-check",
            "update-apply",
            "update-card",
        ):
            self.assertNotIn(retired_text, index)
            self.assertNotIn(retired_text, app)
            self.assertNotIn(retired_text, styles)

    def test_updater_only_dependencies_are_removed(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(
            encoding="utf-8",
        )
        package_names = {
            re.split(r"[<>=!~\[]", line.strip(), maxsplit=1)[0].lower()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertNotIn("packaging", package_names)
        self.assertNotIn("pyyaml", package_names)

    def test_release_version_and_native_update_documentation(self) -> None:
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('PLUGIN_VERSION = "v0.1.7"', main)
        self.assertRegex(metadata, r"(?m)^version:\s*v0\.1\.7\s*$")
        self.assertIn("v0.1.7", readme)
        self.assertIn(
            "https://github.com/YuXya/astrbot_plugin_canvasforge",
            readme,
        )
        self.assertIn("AstrBot", readme)
        self.assertIn("插件管理", readme)

    def test_plugin_owns_background_handoff_instead_of_native_tool_flag(self) -> None:
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("is_background_task", main)


if __name__ == "__main__":
    unittest.main()
