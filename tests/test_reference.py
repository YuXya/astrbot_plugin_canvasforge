from __future__ import annotations

import base64
import unittest
from dataclasses import FrozenInstanceError
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from PIL import Image as PillowImage

from tests.astrbot_stubs import (
    FakeImage,
    FakePlain,
    FakeReply,
    install_astrbot_stubs,
)


install_astrbot_stubs()

from canvasforge.contracts import CanvasForgeError, ErrorCode
from canvasforge.reference import ReferenceResolver, ReferenceSnapshot


def _png_source(width: int = 2, height: int = 3) -> str:
    output = BytesIO()
    PillowImage.new("RGB", (width, height), (255, 0, 128)).save(
        output,
        format="PNG",
    )
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


class _Client:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def call_action(self, *, action: str, **parameters: Any) -> Any:
        self.calls.append({"action": action, **parameters})
        return self.payload


class _Platform:
    def __init__(self, client: _Client) -> None:
        self._client = client

    @staticmethod
    def meta() -> SimpleNamespace:
        return SimpleNamespace(name="aiocqhttp")

    def get_client(self) -> _Client:
        return self._client


class _Context:
    def __init__(self, client: _Client) -> None:
        self.platform = _Platform(client)

    def get_platform_inst(self, platform_id: str) -> _Platform | None:
        return self.platform if platform_id == "platform-1" else None


class _Event:
    def __init__(
        self,
        messages: list[Any],
        *,
        raw_message: dict[str, Any] | None = None,
    ) -> None:
        self.messages = messages
        self.message_obj = SimpleNamespace(
            message=messages,
            raw_message=raw_message or {"message": []},
        )

    def get_messages(self) -> list[Any]:
        return self.messages

    @staticmethod
    def get_platform_name() -> str:
        return "aiocqhttp"

    @staticmethod
    def get_platform_id() -> str:
        return "platform-1"

    @staticmethod
    def get_self_id() -> str:
        return "10001"


class _RejectedHttpResponse:
    status = 403
    content_length = 0

    async def __aenter__(self) -> "_RejectedHttpResponse":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _RejectedHttpSession:
    def get(self, *_args: Any, **_kwargs: Any) -> _RejectedHttpResponse:
        return _RejectedHttpResponse()


class ReferenceSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_copies_portable_sources_without_refresh(self) -> None:
        client = _Client({})
        resolver = ReferenceResolver(_Context(client), object())
        source = "https://example.test/signed/image.png"
        event = _Event(
            [
                FakeReply(
                    [FakeImage(url=source, file="C:\\private\\image.png")],
                    id=42,
                ),
            ],
        )

        snapshot = await resolver.snapshot(event)

        self.assertEqual(snapshot.sources, (source,))
        self.assertEqual(snapshot.count, 1)
        self.assertEqual(snapshot.reply_message_id, 42)
        self.assertFalse(snapshot.refreshed)
        self.assertEqual(client.calls, [])
        self.assertNotIn(source, repr(snapshot))
        self.assertFalse(hasattr(snapshot, "event"))
        self.assertFalse(hasattr(snapshot, "path"))
        with self.assertRaises(FrozenInstanceError):
            snapshot.refreshed = True  # type: ignore[misc]

    async def test_snapshot_refreshes_a_local_only_source(self) -> None:
        refreshed_source = _png_source()
        client = _Client(
            {
                "data": {
                    "message": [
                        {
                            "type": "image",
                            "data": {"url": refreshed_source},
                        },
                    ],
                },
            },
        )
        resolver = ReferenceResolver(_Context(client), object())
        event = _Event(
            [FakeReply([FakeImage(file="/tmp/quoted.png")], id="77")],
        )

        snapshot = await resolver.snapshot(event)

        self.assertEqual(snapshot.sources, (refreshed_source,))
        self.assertTrue(snapshot.refreshed)
        self.assertNotIn("/tmp/quoted.png", snapshot.sources)
        self.assertEqual(
            client.calls,
            [
                {
                    "action": "get_msg",
                    "message_id": 77,
                    "self_id": 10001,
                },
            ],
        )

    async def test_snapshot_refreshes_an_ambiguous_populated_reply(self) -> None:
        refreshed_source = _png_source()
        client = _Client(
            {
                "message": [
                    {"type": "text", "data": {"text": "quoted"}},
                    {
                        "type": "image",
                        "data": {"file": refreshed_source},
                    },
                    {
                        "type": "reply",
                        "data": {"id": "nested-is-ignored"},
                    },
                ],
            },
        )
        resolver = ReferenceResolver(_Context(client), object())
        event = _Event([FakeReply([FakePlain("quoted")], id=88)])

        snapshot = await resolver.snapshot(event)

        self.assertEqual(snapshot.sources, (refreshed_source,))
        self.assertTrue(snapshot.refreshed)

    async def test_resolve_snapshot_does_not_need_the_finished_event(self) -> None:
        source = _png_source(width=4, height=5)
        client = _Client({})
        resolver = ReferenceResolver(_Context(client), object())
        event = _Event([FakeReply([FakeImage(url=source)], id=99)])
        snapshot = await resolver.snapshot(event)

        event.messages.clear()
        references = await resolver.resolve_snapshot(
            snapshot,
            max_images=3,
            max_total_bytes=1024 * 1024,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual((references[0].width, references[0].height), (4, 5))
        self.assertEqual(references[0].mime_type, "image/png")

    async def test_resolve_snapshot_enforces_count_before_download(self) -> None:
        source = _png_source()
        snapshot = ReferenceSnapshot((source, source))
        resolver = ReferenceResolver(_Context(_Client({})), object())

        with self.assertRaises(CanvasForgeError) as raised:
            await resolver.resolve_snapshot(
                snapshot,
                max_images=1,
                max_total_bytes=1024 * 1024,
            )

        self.assertEqual(raised.exception.code, ErrorCode.REFERENCE_LIMIT)

    async def test_legacy_resolve_still_refreshes_a_stale_url_once(self) -> None:
        refreshed_source = _png_source()
        client = _Client(
            {
                "message": [
                    {
                        "type": "image",
                        "data": {"url": refreshed_source},
                    },
                ],
            },
        )
        resolver = ReferenceResolver(_Context(client), _RejectedHttpSession())
        event = _Event(
            [
                FakeReply(
                    [FakeImage(url="https://example.test/expired.png")],
                    id=101,
                ),
            ],
        )

        references = await resolver.resolve(
            event,
            max_images=3,
            max_total_bytes=1024 * 1024,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["action"], "get_msg")

    async def test_background_snapshot_refreshes_a_stale_url_by_saved_id(
        self,
    ) -> None:
        refreshed_source = _png_source()
        client = _Client(
            {
                "message": [
                    {
                        "type": "image",
                        "data": {"url": refreshed_source},
                    },
                ],
            },
        )
        resolver = ReferenceResolver(_Context(client), _RejectedHttpSession())
        event = _Event(
            [
                FakeReply(
                    [FakeImage(url="https://example.test/expired.png")],
                    id=202,
                ),
            ],
        )
        snapshot = await resolver.snapshot(event)
        event.messages.clear()

        references = await resolver.resolve_snapshot(
            snapshot,
            max_images=3,
            max_total_bytes=1024 * 1024,
            event=event,
        )

        self.assertEqual(len(references), 1)
        self.assertEqual(snapshot.reply_message_id, 202)
        self.assertEqual(client.calls[0]["message_id"], 202)

    async def test_has_direct_images_remains_compatible(self) -> None:
        resolver = ReferenceResolver(_Context(_Client({})), object())
        event = _Event([FakeReply([FakeImage(url=_png_source())], id=123)])

        self.assertTrue(await resolver.has_direct_images(event))


if __name__ == "__main__":
    unittest.main()
