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

    def test_text_tool_description_defines_its_scope(self) -> None:
        text_description = _compact(
            inspect.getdoc(
                main.CanvasForgePlugin.canvasforge_text_to_image,
            )
            or "",
        )

        _assert_contains_one(
            self,
            text_description,
            ("根据文字从零创作", "从文字直接创作", "纯文生图"),
        )
        self.assertIn("回复", text_description)
        self.assertIn("聊天参与者", text_description)
        self.assertIn("canvasforge_image_to_image", text_description)
        self.assertIn("完整", text_description)
        self.assertIn("提示词", text_description)

    def test_image_tool_description_defines_selection_and_sources(
        self,
    ) -> None:
        image_description = _compact(
            inspect.getdoc(
                main.CanvasForgePlugin.canvasforge_image_to_image,
            )
            or "",
        )

        self.assertIn("直接回复", image_description)
        self.assertIn("聊天参与者", image_description)
        self.assertIn("canvasforge_text_to_image", image_description)
        for required_text in (
            "avatar_targets",
            "必填",
            "sender",
            "bot",
            "mention:N",
            "当前消息",
            "当前消息附图",
            "嵌套回复",
            "历史",
            "QQ号",
            "昵称",
            "URL",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, image_description)
        self.assertIn("[]", image_description)
        self.assertIn("第N个", image_description)
        self.assertIn("有效直接@", image_description)
        _assert_contains_one(
            self,
            image_description,
            ("猜测人物", "猜人物"),
        )

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
                self.assertIn("accepted=true", description)
                self.assertIn("completed=false", description)
                self.assertIn("正在生成，请稍等", description)
                self.assertIn("回复", description)
                self.assertIn("写入会话后", description)
                _assert_contains_one(
                    self,
                    description,
                    ("后续通知", "另行通知", "插件通知"),
                )

    def test_image_tool_explains_reference_identity_priority(
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
            "构图",
            "场景",
            "融合",
            "遗漏",
            "互换",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, description)

        self.assertIn("用户本轮", description)
        self.assertIn("明确要求", description)
        self.assertIn("prompt", description)

    def test_runtime_guard_applies_reference_rules_in_priority_order(
        self,
    ) -> None:
        prompt = "Two people, both with silver hair."
        guarded = main.CanvasForgePlugin._with_edit_reference_guard(
            prompt,
            has_references=True,
        )

        self.assertTrue(guarded.startswith(prompt))
        self.assertIn("身份锚点", guarded)
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
            ("以参考图为准", "参考图优先"),
        )
        self.assertIn("用户本轮", guarded)
        self.assertIn("明确要求", guarded)
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

        ordered_markers = (
            "身份锚点",
            "脸部轮廓",
            "冲突",
            "用户本轮",
            "表情",
            "不得遗漏",
        )
        positions = tuple(guarded.index(marker) for marker in ordered_markers)
        self.assertEqual(tuple(sorted(positions)), positions)

    def test_avatar_mapping_contains_only_identifying_information(
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
        self.assertIn("输入图3 = 人物参考1", mapped)
        self.assertIn("输入图4 = 人物参考2", mapped)
        self.assertIn('"发送者"', mapped)
        self.assertIn('"机器人"', mapped)
        self.assertNotIn("不是指令", mapped)
        self.assertNotIn("不要画进图片", mapped)
        self.assertNotIn("不得遗漏", mapped)
        self.assertNotIn("融合", mapped)
        self.assertNotIn("互换", mapped)

        combined = main.CanvasForgePlugin._with_edit_reference_guard(
            mapped,
            has_references=True,
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
