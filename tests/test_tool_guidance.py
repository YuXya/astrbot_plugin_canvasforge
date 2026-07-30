from __future__ import annotations

import inspect
import re
import unittest

from tests.plugin_loader import load_main_module


main = load_main_module()


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


class ToolGuidanceTests(unittest.TestCase):
    def test_tool_descriptions_stay_concise(self) -> None:
        text_description = inspect.getdoc(
            main.CanvasForgePlugin.canvasforge_text_to_image,
        )
        image_description = inspect.getdoc(
            main.CanvasForgePlugin.canvasforge_image_to_image,
        )

        self.assertIsNotNone(text_description)
        self.assertIsNotNone(image_description)
        self.assertLessEqual(len(text_description or ""), 400)
        self.assertLessEqual(len(image_description or ""), 850)

    def test_tool_descriptions_keep_selection_and_prompt_duties(self) -> None:
        text_description = _compact(
            inspect.getdoc(
                main.CanvasForgePlugin.canvasforge_text_to_image,
            )
            or "",
        )
        image_description = _compact(
            inspect.getdoc(
                main.CanvasForgePlugin.canvasforge_image_to_image,
            )
            or "",
        )

        self.assertIn("聊天参与者", text_description)
        self.assertIn("canvasforge_image_to_image", text_description)
        self.assertIn("完整", text_description)
        self.assertIn("提示词", text_description)

        for required_text in (
            "avatar_targets",
            "必填",
            "sender",
            "bot",
            "mention:N",
            "完整",
            "提示词",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, image_description)

    def test_image_tool_explains_identity_reference_and_ai_choice(self) -> None:
        description = _compact(
            inspect.getdoc(
                main.CanvasForgePlugin.canvasforge_image_to_image,
            )
            or "",
        )

        for required_text in (
            "身份外貌",
            "表情",
            "视线",
            "姿势",
            "动作",
            "服装",
            "构图",
            "场景",
            "AI",
            "自行决定",
            "提示词",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, description)

        self.assertRegex(
            description,
            r"(可以|可)(保留|沿用)(或|，?也可以|，?也可)(调整|改变)",
        )
        self.assertIn("用户明确要求改变外貌", description)
        self.assertIn("用户要求为准", description)

    def test_runtime_guard_preserves_identity_without_locking_the_scene(self) -> None:
        prompt = "基础提示词"
        guarded = main.CanvasForgePlugin._with_edit_reference_guard(
            prompt,
            has_references=True,
            has_avatar_references=True,
        )

        self.assertTrue(guarded.startswith(prompt))
        self.assertIn("身份外貌", guarded)
        self.assertIn("可辨识", guarded)
        for flexible_element in (
            "表情",
            "视线",
            "姿势",
            "动作",
            "服装",
            "构图",
            "场景",
        ):
            with self.subTest(flexible_element=flexible_element):
                self.assertIn(flexible_element, guarded)
        self.assertRegex(
            _compact(guarded),
            r"(可以|可)(保留|沿用)(或|，?也可以|，?也可)(调整|改变)",
        )

        self.assertNotIn("用户明确要求的外貌变更：", guarded)
        self.assertNotIn("服装必须以参考图为准", guarded)
        self.assertNotIn("身材和服装设定", guarded)

    def test_avatar_mapping_preserves_identity_without_locking_the_scene(
        self,
    ) -> None:
        prompt = "基础提示词"
        mapped = main.CanvasForgePlugin._with_avatar_mapping(
            prompt,
            [
                main.ResolvedAvatar(
                    selector="sender",
                    display_name="测试人物",
                    reference=object(),
                ),
            ],
            reply_reference_count=1,
        )

        self.assertTrue(mapped.startswith(prompt))
        self.assertIn("人物参考图映射", mapped)
        self.assertIn("身份外貌", mapped)
        self.assertIn("可辨识", mapped)
        self.assertIn("保留或调整", mapped)
        for flexible_element in (
            "表情",
            "视线",
            "姿势",
            "动作",
            "服装",
            "构图",
            "场景",
        ):
            with self.subTest(flexible_element=flexible_element):
                self.assertIn(flexible_element, mapped)

        self.assertNotIn("用户明确要求的外貌变更：", mapped)
        self.assertNotIn("服装必须以参考图为准", mapped)
        self.assertNotIn("身材和服装设定", mapped)

    def test_runtime_guard_is_not_added_without_references(self) -> None:
        prompt = "无需参考图"

        self.assertEqual(
            prompt,
            main.CanvasForgePlugin._with_edit_reference_guard(
                prompt,
                has_references=False,
                has_avatar_references=False,
            ),
        )


if __name__ == "__main__":
    unittest.main()
