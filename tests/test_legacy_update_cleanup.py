from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tests.plugin_loader import load_main_module


main = load_main_module()


class FakeContext:
    def register_web_api(self, *_args) -> None:
        pass


class LegacyUpdateCleanupTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, context: FakeContext, data_root: Path):
        plugin = main.CanvasForgePlugin(context, {})
        plugin._data_root = data_root
        return plugin

    @staticmethod
    def seed_files(root: Path) -> tuple[Path, Path, Path, Path]:
        status = root / "update-status.json"
        temporary = root / "update-status.json.tmp"
        config = root / "config.json"
        cached = root / "cache" / "kept-image.png"
        cached.parent.mkdir(parents=True)
        status.write_text("old status", encoding="utf-8")
        temporary.write_text("old temporary status", encoding="utf-8")
        config.write_text("keep configuration", encoding="utf-8")
        cached.write_bytes(b"keep cached image")
        return status, temporary, config, cached

    async def test_cleanup_removes_only_two_fixed_status_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, temporary, config, cached = self.seed_files(root)
            context = FakeContext()
            runtime: dict[str, object] = {"schema": 1}
            admission: dict[str, object] = {"schema": 2}
            setattr(context, main._LEGACY_UPDATE_CONTEXT_KEY, runtime)
            setattr(context, main._LEGACY_ADMISSION_CONTEXT_KEY, admission)
            plugin = self.make_plugin(context, root)

            await plugin._start_legacy_update_cleanup()

            self.assertFalse(status.exists())
            self.assertFalse(temporary.exists())
            self.assertEqual(
                "keep configuration",
                config.read_text(encoding="utf-8"),
            )
            self.assertEqual(b"keep cached image", cached.read_bytes())
            self.assertFalse(
                hasattr(context, main._LEGACY_UPDATE_CONTEXT_KEY),
            )
            self.assertFalse(
                hasattr(context, main._LEGACY_ADMISSION_CONTEXT_KEY),
            )

    async def test_live_legacy_updater_finishes_before_files_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, temporary, config, cached = self.seed_files(root)
            context = FakeContext()
            started = asyncio.Event()
            release = asyncio.Event()

            async def legacy_writer() -> None:
                started.set()
                await release.wait()
                # Model the old updater's terminal status write. Cleanup must
                # happen after this write or the obsolete file would return.
                status.write_text("terminal legacy status", encoding="utf-8")

            update_task = asyncio.create_task(legacy_writer())
            await started.wait()
            runtime = {
                "schema": 1,
                "update_task": update_task,
            }
            setattr(context, main._LEGACY_UPDATE_CONTEXT_KEY, runtime)
            plugin = self.make_plugin(context, root)

            await plugin._start_legacy_update_cleanup()
            watcher = plugin._legacy_cleanup_task
            self.assertIsNotNone(watcher)
            self.assertTrue(status.exists())
            self.assertTrue(temporary.exists())

            release.set()
            await update_task
            await asyncio.wait_for(watcher, timeout=1)

            self.assertFalse(status.exists())
            self.assertFalse(temporary.exists())
            self.assertTrue(config.exists())
            self.assertTrue(cached.exists())
            self.assertFalse(
                hasattr(context, main._LEGACY_UPDATE_CONTEXT_KEY),
            )

    async def test_cleanup_waits_when_new_plugin_loads_inside_old_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, temporary, config, cached = self.seed_files(root)
            context = FakeContext()
            runtime: dict[str, object] = {"schema": 1}
            setattr(context, main._LEGACY_UPDATE_CONTEXT_KEY, runtime)
            plugin = self.make_plugin(context, root)

            async def load_new_plugin() -> asyncio.Task[None]:
                runtime["update_task"] = asyncio.current_task()
                await plugin._start_legacy_update_cleanup()
                watcher = plugin._legacy_cleanup_task
                self.assertIsNotNone(watcher)
                self.assertTrue(status.exists())
                status.write_text("old updater finished", encoding="utf-8")
                return watcher

            old_update_task = asyncio.create_task(load_new_plugin())
            watcher = await old_update_task
            runtime["update_task"] = None
            await asyncio.wait_for(watcher, timeout=1)

            self.assertFalse(status.exists())
            self.assertFalse(temporary.exists())
            self.assertTrue(config.exists())
            self.assertTrue(cached.exists())


if __name__ == "__main__":
    unittest.main()
