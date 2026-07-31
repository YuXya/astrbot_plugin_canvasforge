from __future__ import annotations

import unittest

from tests.astrbot_stubs import install_astrbot_stubs


install_astrbot_stubs()

from canvasforge.avatar import AvatarResolver


class _Event:
    message_obj = None

    def get_sender_id(self) -> str:
        return "10001"

    def get_self_id(self) -> str:
        return "20002"

    def get_group_id(self) -> str:
        return "30003"

    def get_sender_name(self) -> str:
        return "发送者"

    def get_messages(self) -> list[object]:
        return []


class AvatarTargetTests(unittest.TestCase):
    def test_repeated_avatar_identity_is_stably_deduplicated(self) -> None:
        resolver = object.__new__(AvatarResolver)

        targets = resolver.plan(_Event(), ["sender", "sender", "bot"])

        self.assertEqual(["sender", "bot"], [item.selector for item in targets])
        self.assertEqual(["10001", "20002"], [item.user_id for item in targets])

    def test_omitted_avatar_targets_means_no_avatar_references(self) -> None:
        resolver = object.__new__(AvatarResolver)

        self.assertEqual([], resolver.plan(_Event(), None))
        self.assertEqual([], resolver.plan(_Event(), []))


if __name__ == "__main__":
    unittest.main()
