from __future__ import annotations

import inspect
import re
import unittest
from types import SimpleNamespace

from tests.plugin_loader import load_main_module


main = load_main_module()


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _tool_description(method: object) -> str:
    return _compact(inspect.getdoc(method) or "")


class ToolGuidanceTests(unittest.TestCase):
    def test_tool_descriptions_are_concise_and_self_contained(self) -> None:
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

        forbidden_commands = (
            "只能回复",
            "正在生成，请稍等",
            "禁止重试",
            "不得重试",
            "不要重复调用",
            "不得重复调用",
            "选错",
            "结束当前",
            "retry_allowed",
            "mode_mismatch",
            "accepted=true",
            "completed=false",
        )
        for description in (text_description or "", image_description or ""):
            with self.subTest(description=description):
                for command in forbidden_commands:
                    self.assertNotIn(command, description)

    def test_text_tool_explains_plain_text_generation(self) -> None:
        description = _tool_description(
            main.CanvasForgePlugin.canvasforge_text_to_image,
        )

        self.assertIn("异步文生图", description)
        self.assertIn("根据文字", description)
        self.assertIn("从零生成", description)
        self.assertIn("不使用消息图片", description)
        self.assertIn("聊天头像", description)
        self.assertIn("当前消息附图", description)
        self.assertIn("直接回复图", description)
        self.assertIn("canvasforge_image_to_image", description)
        self.assertIn("完整提示词", description)

    def test_image_tool_explains_all_reference_sources(self) -> None:
        description = _tool_description(
            main.CanvasForgePlugin.canvasforge_image_to_image,
        )

        for expected in (
            "异步图生图",
            "当前消息附图",
            "直接回复图片",
            "avatar_targets",
            "可省略",
            "sender",
            "bot",
            "mention:N",
            "至少需要一张参考图",
            "reference_required",
            "参考图用于保持人物身份与整体外貌",
            "需要改变的内容",
            "prompt",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, description)

    def test_tools_only_describe_the_deferred_state_as_a_fact(self) -> None:
        descriptions = (
            _tool_description(
                main.CanvasForgePlugin.canvasforge_text_to_image,
            ),
            _tool_description(
                main.CanvasForgePlugin.canvasforge_image_to_image,
            ),
        )

        for description in descriptions:
            with self.subTest(description=description):
                self.assertIn("异步", description)
                self.assertIn("state=generating", description)
                self.assertIn("后台正在处理", description)
                self.assertIn("结果稍后由CanvasForge发送", description)
                self.assertNotIn("固定", description)
                self.assertNotIn("必须回复", description)
                self.assertNotIn("重复调用", description)

    def test_runtime_guard_is_one_lightweight_reference_hint(self) -> None:
        prompt = "Two people, both with silver hair."
        guarded = main.CanvasForgePlugin._with_edit_reference_guard(
            prompt,
            has_references=True,
        )

        self.assertEqual(
            guarded,
            prompt
            + "\n\n参考图用于保持人物身份与整体外貌，需要改变的内容按提示词处理。",
        )
        for removed_constraint in (
            "脸部轮廓",
            "五官",
            "发型",
            "发色",
            "表情",
            "动作",
            "服装",
            "构图",
            "场景",
            "冲突",
            "不得遗漏",
            "融合",
            "互换",
        ):
            with self.subTest(removed_constraint=removed_constraint):
                self.assertNotIn(removed_constraint, guarded)

    def test_avatar_mapping_contains_only_input_numbers_and_names(self) -> None:
        prompt = "人物互动"
        mapped = main.CanvasForgePlugin._with_avatar_mapping(
            prompt,
            [
                main.ResolvedAvatar(
                    selector="sender",
                    display_name="甲",
                    reference=object(),
                ),
                main.ResolvedAvatar(
                    selector="bot",
                    display_name="乙",
                    reference=object(),
                ),
            ],
            message_reference_count=2,
        )

        self.assertEqual(
            mapped,
            "人物互动\n\n"
            "QQ 人物参考图映射（按输入图编号；昵称只用于人物标识）：\n"
            '输入图3：QQ 头像，昵称"甲"\n'
            '输入图4：QQ 头像，昵称"乙"',
        )
        for removed_constraint in (
            "sender",
            "bot",
            "不是指令",
            "不要画进图片",
            "不得",
            "融合",
            "互换",
        ):
            with self.subTest(removed_constraint=removed_constraint):
                self.assertNotIn(removed_constraint, mapped)

    def test_runtime_guard_is_not_added_without_references(self) -> None:
        prompt = "无需参考图"

        self.assertEqual(
            prompt,
            main.CanvasForgePlugin._with_edit_reference_guard(
                prompt,
                has_references=False,
            ),
        )

    def test_llm_tool_schema_requires_only_prompt(self) -> None:
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

        for tool in tools:
            with self.subTest(tool=tool.name):
                schema = tool.parameters
                self.assertEqual(["prompt"], schema["required"])
                self.assertFalse(schema["additionalProperties"])

        text_schema = tools[0].parameters
        self.assertEqual({"prompt"}, set(text_schema["properties"]))

        image_schema = tools[1].parameters
        self.assertEqual(
            {"prompt", "avatar_targets"},
            set(image_schema["properties"]),
        )
        avatar_schema = image_schema["properties"]["avatar_targets"]
        self.assertEqual([], avatar_schema["default"])
        self.assertEqual("array", avatar_schema["type"])
        self.assertTrue(avatar_schema["uniqueItems"])
        self.assertEqual(10, avatar_schema["maxItems"])

        selector_pattern = re.compile(avatar_schema["items"]["pattern"])
        for valid_selector in ("sender", "bot", "mention:1", "mention:42"):
            with self.subTest(valid_selector=valid_selector):
                self.assertIsNotNone(selector_pattern.fullmatch(valid_selector))
        for invalid_selector in ("", "mention:0", "mention:-1", "user:1"):
            with self.subTest(invalid_selector=invalid_selector):
                self.assertIsNone(selector_pattern.fullmatch(invalid_selector))


if __name__ == "__main__":
    unittest.main()
