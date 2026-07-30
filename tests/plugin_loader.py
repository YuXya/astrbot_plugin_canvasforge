"""Import the AstrBot plugin entry point as a package in local tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

from tests.astrbot_stubs import install_astrbot_stubs


ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "_canvasforge_plugin_under_test"
_MODULE_NAME = f"{_PACKAGE_NAME}.main"


def load_main_module() -> ModuleType:
    install_astrbot_stubs()
    loaded = sys.modules.get(_MODULE_NAME)
    if loaded is not None:
        return loaded

    package = types.ModuleType(_PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    package.__package__ = _PACKAGE_NAME
    sys.modules[_PACKAGE_NAME] = package

    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME,
        ROOT / "main.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not create a module spec for main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module
