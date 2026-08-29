from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from mystic.types import SkillContext, CallOriginContext, OperationalContext, PersonContext, SelfContext, ToolCall
from mystic.skills import (
    build_self_context,
    build_skill_tool_schema,
    build_tools_for_context,
    execute_cognitive_skill,
    execute_tool_calls,
    get_default_skills_dir,
    get_registry,
    init_skills,
    load_handler_module,
    reset_registry,
)
from tests.python_helpers import TempAppHome, seed_core_files


class SkillsFrameworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        reset_registry()
        self.registry = init_skills()

    def tearDown(self) -> None:
        reset_registry()
        self.temp_home.__exit__(None, None, None)

    def test_init_skills_loads_root_skill_directory(self) -> None:
        self.assertEqual(get_default_skills_dir(), Path.cwd() / "skills")
        self.assertEqual(len(self.registry), 48)
        self.assertIn("chat", self.registry)
        self.assertIn("read-facts", self.registry)
        self.assertIn("read-setup", self.registry)
        self.assertIn("read-twilio-numbers", self.registry)
        self.assertIn("read-dashboard", self.registry)
        self.assertIn("read-calendar", self.registry)
        self.assertIn("recall-self", self.registry)
        self.assertIn("check-availability", self.registry)
        self.assertIn("find-open-slots", self.registry)
        self.assertIn("manage-appointment", self.registry)
        self.assertIn("take-message", self.registry)
        self.assertIn("send-email", self.registry)
        self.assertIn("transfer-call", self.registry)
        self.assertIn("hold-call", self.registry)
        self.assertIn("send-dtmf", self.registry)
        self.assertIn("warm-transfer-call", self.registry)
        self.assertIn("design-dashboard", self.registry)
        self.assertIn("write-twilio-credentials", self.registry)
        self.assertIn("write-twilio-number", self.registry)
        self.assertEqual(self.registry["edit-soul"].kind, "cognitive")
        self.assertEqual(self.registry["take-message"].modality, ("voice", "text"))
        assert self.registry["take-message"].parameters is not None
        self.assertEqual(self.registry["take-message"].parameters.required, ("content",))
        self.assertEqual(self.registry["design-dashboard"].modality, ("text",))
        self.assertTrue(self.registry["write-action"].has_handler)
        self.assertIsNone(self.registry["chat"].modality)

    def test_get_registry_requires_initialization(self) -> None:
        reset_registry()
        with self.assertRaisesRegex(RuntimeError, "Skills not initialized"):
            get_registry()

    def test_loader_skips_malformed_skill(self) -> None:
        bad_dir = self.home / "broken-skills"
        (bad_dir / "bad-skill").mkdir(parents=True)
        (bad_dir / "bad-skill" / "SKILL.md").write_text("# missing frontmatter\n", encoding="utf-8")
        registry = init_skills(bad_dir)
        self.assertEqual(registry, {})

    def test_build_self_context_renders_prompt_template(self) -> None:
        skill = self.registry["extract-facts"]
        prompt = build_self_context(
            SelfContext(
                person=PersonContext(
                    name="Alice",
                    summary="A longtime client.",
                    facts=["Prefers email updates"],
                ),
                actions=["Follow up on renewal"],
                call_origin=CallOriginContext(
                    direction="outbound",
                    audience="owner",
                    channel="phone",
                    modality="voice",
                    action_intent="Discuss renewal terms",
                ),
                tool_context="The caller sounded rushed.",
            ),
            skill,
        )
        self.assertIn("Alice", prompt)
        self.assertIn("Prefers email updates", prompt)
        self.assertIn("Discuss renewal terms", prompt)
        self.assertIn("The caller sounded rushed.", prompt)
        self.assertIn("Return JSON:", prompt)

    def test_build_self_context_uses_soul_as_data_for_edit_soul(self) -> None:
        skill = self.registry["edit-soul"]
        prompt = build_self_context(SelfContext(), skill)
        self.assertIn("Current SOUL.md", prompt)
        self.assertIn("Test Soul", prompt)

    async def test_execute_cognitive_skill_uses_default_llm_when_no_handler(self) -> None:
        with patch("mystic.skills.invoke_agent", new=AsyncMock(return_value='{"summary":"ok"}')) as invoke:
            result = await execute_cognitive_skill(
                "summarize-call",
                SelfContext(person=PersonContext(name="Alice", summary=None, facts=[])),
                "Transcript",
            )

        self.assertEqual(result, '{"summary":"ok"}')
        invoke.assert_awaited_once()
        await_args = invoke.await_args
        self.assertIsNotNone(await_args)
        assert await_args is not None
        args = await_args.args
        self.assertEqual(args[0], "summarize-call")
        self.assertIn("Alice", args[1])
        self.assertEqual(args[2], "Transcript")

    async def test_execute_cognitive_skill_uses_handler_override(self) -> None:
        module = load_handler_module(self.registry["edit-soul"])
        with patch.object(module, "invoke_agent", new=AsyncMock(return_value="# Updated Soul")):
            result = await execute_cognitive_skill("edit-soul", SelfContext(), "Be warmer.")

        self.assertEqual(result, "Updated SOUL.md. Previous version saved to journal.")
        self.assertEqual((self.home / "SOUL.md").read_text(encoding="utf-8"), "# Updated Soul")

    def test_build_skill_tool_schema_uses_parameters_and_gotchas(self) -> None:
        schema = build_skill_tool_schema(self.registry["design-dashboard"])

        self.assertEqual(schema["type"], "function")
        function = cast(dict[str, object], schema["function"])
        self.assertEqual(function["name"], "design-dashboard")
        self.assertIn("Gotchas:", cast(str, function["description"]))
        parameters = cast(dict[str, object], function["parameters"])
        self.assertEqual(parameters["required"], ["file"])
        properties = cast(dict[str, object], parameters["properties"])
        self.assertIn("instructions", properties)

    def test_build_tools_for_context_filters_by_audience_and_modality(self) -> None:
        owner_text = build_tools_for_context(self.registry, "owner", "text")
        owner_text_names = {cast(dict[str, object], tool["function"])["name"] for tool in owner_text}
        self.assertIn("design-dashboard", owner_text_names)
        self.assertIn("read-dashboard", owner_text_names)
        self.assertIn("read-twilio-numbers", owner_text_names)
        self.assertIn("write-twilio-number", owner_text_names)
        self.assertNotIn("transfer-call", owner_text_names)

        public_voice = build_tools_for_context(self.registry, "public", "voice")
        public_voice_names = {cast(dict[str, object], tool["function"])["name"] for tool in public_voice}
        self.assertIn("chat", public_voice_names)
        self.assertIn("transfer-call", public_voice_names)
        self.assertIn("hold-call", public_voice_names)
        self.assertNotIn("design-dashboard", public_voice_names)
        self.assertNotIn("read-soul", public_voice_names)

        public_text = build_tools_for_context(self.registry, "public", "text")
        public_text_names = {cast(dict[str, object], tool["function"])["name"] for tool in public_text}
        self.assertIn("chat", public_text_names)

    async def test_execute_tool_calls_dispatches_operational_skill_and_derives_source(self) -> None:
        skill_context = SkillContext(
            audience="public",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-123",
            person_id="person-123",
            source="mid-call",
        )
        module = load_handler_module(self.registry["write-action"])
        with patch.object(module, "execute", new=AsyncMock(return_value="ok")) as execute:
            result = await execute_tool_calls(
                None,  # type: ignore[arg-type]
                skill_context,
                [
                    ToolCall(
                        id="tool-1",
                        name="write-action",
                        arguments={"intent": "Call back", "context": "Urgent"},
                    )
                ],
            )

        self.assertEqual(result[0].result, "ok")
        await_args = execute.await_args
        self.assertIsNotNone(await_args)
        assert await_args is not None
        op_ctx = cast(OperationalContext, await_args.args[1])
        self.assertEqual(op_ctx.source, "caller")
        self.assertEqual(op_ctx.tool_context, "Urgent")

    async def test_execute_tool_calls_denies_unpermitted_skill(self) -> None:
        skill_context = SkillContext(
            audience="public",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-123",
            person_id="person-123",
            source="mid-call",
        )
        result = await execute_tool_calls(
            None,  # type: ignore[arg-type]
            skill_context,
            [ToolCall(id="tool-1", name="write-fact", arguments={"content": "secret"})],
        )
        self.assertEqual(result[0].result, "Permission denied: public cannot use write-fact.")

    async def test_execute_tool_calls_accepts_json_encoded_arguments(self) -> None:
        skill_context = SkillContext(
            audience="owner",
            direction="inbound",
            channel="cli",
            modality="text",
            call_id="call-123",
            person_id="person-123",
            source="cli",
        )
        module = load_handler_module(self.registry["read-soul"])
        with patch.object(module, "execute", new=AsyncMock(return_value="soul")):
            result = await execute_tool_calls(
                None,  # type: ignore[arg-type]
                skill_context,
                [
                    {
                        "id": "tool-1",
                        "type": "function",
                        "function": {"name": "read-soul", "arguments": json.dumps({})},
                    }
                ],
            )

        self.assertEqual(result[0].result, "soul")


if __name__ == "__main__":
    unittest.main()
