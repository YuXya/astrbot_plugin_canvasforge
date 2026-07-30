from __future__ import annotations

import inspect
import re
import unittest
from types import SimpleNamespace

from tests.plugin_loader import load_main_module


main = load_main_module()


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _assert_contains_one(
    testcase: unittest.TestCase,
    value: str,
    candidates: tuple[str, ...],
) -> None:
    testcase.assertTrue(
        any(candidate in value for candidate in candidates),
        f"{value!r} does not contain any of {candidates!r}",
    )


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

    def test_tools_explain_deferred_background_generation(
        self,
    ) -> None:
        descriptions = (
            _compact(
                inspect.getdoc(
                    main.CanvasForgePlugin.canvasforge_text_to_image,
                )
                or "",
            ),
            _compact(
                inspect.getdoc(
                    main.CanvasForgePlugin.canvasforge_image_to_image,
                )
                or "",
            ),
        )

        for description in descriptions:
            with self.subTest(description=description):
                self.assertIn("异步", description)
                self.assertIn("completed=false", description)
                self.assertIn("回复发送并写入会话后", description)

    def test_image_tool_locks_all_reference_people_identity_features(
        self,
    ) -> None:
        description = _compact(
            inspect.getdoc(
                main.CanvasForgePlugin.canvasforge_image_to_image,
            )
            or "",
        )

        for required_text in (
            "参考图",
            "脸部轮廓",
            "五官",
            "发型",
            "发色",
            "表情",
            "动作",
            "服装",
            "场景",
            "提示词",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, description)

        _assert_contains_one(
            self,
            description,
            ("所有参考图", "每张参考图", "参考图中的人物"),
        )
        self.assertIn("用户当前", description)
        self.assertIn("明确要求", description)
        self.assertIn("prompt", description)
        self.assertIn("用户本轮", description)
        _assert_contains_one(
            self,
            description,
            ("不得擅自", "不要擅自", "不能擅自", "不得按"),
        )

    def test_runtime_guard_locks_people_in_direct_reply_references(self) -> None:
        prompt = "Two people, both with silver hair."
        guarded = main.CanvasForgePlugin._with_edit_reference_guard(
            prompt,
            has_references=True,
            has_avatar_references=False,
        )

        self.assertTrue(guarded.startswith(prompt))
        for identity_feature in (
            "脸部轮廓",
            "五官",
            "发型",
            "发色",
        ):
            with self.subTest(identity_feature=identity_feature):
                self.assertIn(identity_feature, guarded)
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
        self.assertIn("冲突", guarded)
        _assert_contains_one(
            self,
            guarded,
            ("忽略", "以参考图为准"),
        )
        _assert_contains_one(
            self,
            guarded,
            ("用户当前", "当前用户"),
        )
        self.assertIn("明确要求", guarded)
        self.assertIn("主提示词明确说明", guarded)
        self.assertIn("当前用户原话", guarded)
        _assert_contains_one(
            self,
            guarded,
            (
                "所有可见人物",
                "每个可见人物",
                "参考图中的人物",
                "每张参考图中的每个人物",
            ),
        )
        _assert_contains_one(
            self,
            guarded,
            ("非人物", "不含人物"),
        )
        self.assertNotIn("输入图1 = 人物参考", guarded)

    def test_avatar_mapping_keeps_indices_and_people_separate(
        self,
    ) -> None:
        prompt = "人物互动"
        mapped = main.CanvasForgePlugin._with_avatar_mapping(
            prompt,
            [
                main.ResolvedAvatar(
                    selector="sender",
                    display_name="发送者",
                    reference=object(),
                ),
                main.ResolvedAvatar(
                    selector="bot",
                    display_name="机器人",
                    reference=object(),
                ),
            ],
            reply_reference_count=2,
        )

        self.assertTrue(mapped.startswith(prompt))
        self.assertIn("人物参考图映射", mapped)
        self.assertIn("输入图3 = 人物参考1", mapped)
        self.assertIn("输入图4 = 人物参考2", mapped)
        self.assertIn('"发送者"', mapped)
        self.assertIn('"机器人"', mapped)
        combined = main.CanvasForgePlugin._with_edit_reference_guard(
            mapped,
            has_references=True,
            has_avatar_references=True,
        )
        self.assertIn("不得", combined)
        self.assertIn("融合", combined)
        self.assertIn("互换", combined)

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

    def test_llm_tool_schema_requires_only_public_parameters(self) -> None:
        tools = [
            SimpleNamespace(
                name="canvasforge_text_to_image",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                    },
                },
            ),
            SimpleNamespace(
                name="canvasforge_image_to_image",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "avatar_targets": {"type": "array"},
                    },
                },
            ),
        ]
        manager = SimpleNamespace(func_list=tools)
        context = SimpleNamespace(
            get_llm_tool_manager=lambda: manager,
        )
        plugin = object.__new__(main.CanvasForgePlugin)
        plugin.context = context

        plugin._configure_llm_tool_schemas()

        expected = {
            "canvasforge_text_to_image": {"prompt"},
            "canvasforge_image_to_image": {
                "prompt",
                "avatar_targets",
            },
        }
        for tool in tools:
            with self.subTest(tool=tool.name):
                schema = tool.parameters
                self.assertEqual(
                    expected[tool.name],
                    set(schema["required"]),
                )
                self.assertEqual(
                    expected[tool.name],
                    set(schema["properties"]),
                )
                self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
