"""Small AstrBot stand-ins used by the standard-library test suite.

The real plugin is imported by AstrBot in production.  CI for this repository
does not install AstrBot, so tests install only the interfaces needed while
importing and exercising CanvasForge.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from typing import Any


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.background = None


class FakeRequest:
    username: str | None = "admin"
    query: dict[str, str] = {}
    _json_payload: Any = {}

    @classmethod
    async def json(cls, *, default: Any = None) -> Any:
        return cls._json_payload if cls._json_payload is not None else default


class FakeMessageChain:
    def __init__(self, chain: list[Any] | None = None) -> None:
        self.chain = list(chain or [])


class FakePlain:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeImage:
    def __init__(self, source: Any = None, **values: Any) -> None:
        self.source = source
        for key, value in values.items():
            setattr(self, key, value)

    @classmethod
    def fromBytes(cls, value: bytes) -> "FakeImage":
        return cls(value)


class FakeReply:
    def __init__(self, chain: list[Any] | None = None, **values: Any) -> None:
        self.chain = list(chain or [])
        for key, value in values.items():
            setattr(self, key, value)


class FakeAt:
    def __init__(self, qq: str = "", **values: Any) -> None:
        self.qq = qq
        for key, value in values.items():
            setattr(self, key, value)


class FakeLogger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def exception(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class FakeFilter:
    @staticmethod
    def llm_tool(*_args: Any, **_kwargs: Any):
        return lambda value: value

    @staticmethod
    def command(*_args: Any, **_kwargs: Any):
        return lambda value: value


class FakeStar:
    def __init__(self, context: Any, config: Any) -> None:
        self.context = context
        self.config = config


class FakeStarTools:
    data_dir = Path(tempfile.gettempdir()) / "canvasforge-tests"

    @classmethod
    def get_data_dir(cls, _plugin_name: str) -> Path:
        return cls.data_dir


def _register(*_args: Any, **_kwargs: Any):
    return lambda value: value


def install_astrbot_stubs() -> None:
    """Install deterministic fake AstrBot modules into ``sys.modules``."""

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.__version__ = "4.26.7"

    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.AstrBotConfig = dict
    api.logger = FakeLogger()

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = FakeMessageChain
    event.filter = FakeFilter()

    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = FakePlain
    components.Image = FakeImage
    components.Reply = FakeReply
    components.At = FakeAt

    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = FakeStar
    star.StarTools = FakeStarTools
    star.register = _register

    web = types.ModuleType("astrbot.api.web")
    web.request = FakeRequest
    web.json_response = lambda payload, status_code=200, headers=None: FakeResponse(
        payload,
        status_code=status_code,
        headers=headers,
    )
    web.error_response = lambda message, status_code=400, headers=None: FakeResponse(
        {"error": message},
        status_code=status_code,
        headers=headers,
    )
    web.file_response = lambda *args, **kwargs: FakeResponse(
        {"args": args, "kwargs": kwargs},
    )

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
        "astrbot.api.web": web,
    }
    sys.modules.update(modules)
