"""Skill discovery, routing, self-context assembly, and tool schemas."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, cast

from mystic.llm import invoke_agent
from mystic.config import emit_event, logger, read_identity_raw, read_soul
from mystic.types import (
    Audience,
    CognitiveHandler,
    ContextDimension,
    FactSource,
    InvokeSource,
    Modality,
    OperationalContext,
    OperationalHandler,
    SelfContext,
    SkillContext,
    SkillKind,
    SkillMetadata,
    SkillParameters,
    SkillRegistry,
    ToolCall,
    ToolResult,
    derive_source,
)

# ── loader ────────────────────────────────────────────────────────────────────

VALID_KINDS = frozenset(("cognitive", "operational"))
VALID_INVOKE = frozenset(("owner", "public", "pipeline", "scheduler"))
VALID_MODALITY = frozenset(("voice", "text"))
VALID_CONTEXT = frozenset(
    ("identity", "soul", "person", "actions", "call-origin", "recent-calls", "transcript")
)
_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---(?:\s*\n)?")
_KEY_RE = re.compile(r"^([\w][\w-]*)\s*:\s*(.*)")

_registry: SkillRegistry | None = None


def get_default_skills_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "skills"


def init_skills(skills_dir: str | Path | None = None) -> SkillRegistry:
    global _registry
    _registry = discover_skills(skills_dir or get_default_skills_dir())
    return _registry


def get_registry() -> SkillRegistry:
    if _registry is None:
        raise RuntimeError("Skills not initialized — call init_skills first")
    return _registry


def get_skill(registry: SkillRegistry, name: str) -> SkillMetadata | None:
    return registry.get(name)


def reset_registry() -> None:
    global _registry
    _registry = None


def discover_skills(skills_dir: str | Path) -> SkillRegistry:
    skills_path = Path(skills_dir)
    registry: SkillRegistry = {}
    if not skills_path.exists():
        logger.warn("skills.discovery.no-dir", skillsDir=str(skills_path))
        return registry

    for entry in sorted(skills_path.iterdir()):
        if not entry.is_dir():
            continue
        skill_md_path = entry / "SKILL.md"
        if not skill_md_path.exists():
            continue
        try:
            skill = parse_skill_md(skill_md_path, entry, entry.name)
        except Exception as exc:
            logger.warn("skills.discovery.skip", dir=entry.name, error=str(exc))
            continue
        if skill is not None:
            registry[skill.name] = skill

    logger.info(
        "skills.loaded",
        count=len(registry),
        cognitive=sum(1 for skill in registry.values() if skill.kind == "cognitive"),
        operational=sum(1 for skill in registry.values() if skill.kind == "operational"),
    )
    return registry


def parse_skill_md(file_path: str | Path, dir_path: str | Path, dir_name: str) -> SkillMetadata | None:
    raw = Path(file_path).read_text(encoding="utf-8")
    frontmatter, body = _extract_frontmatter(raw)
    parsed = parse_frontmatter(frontmatter)

    name = parsed.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("Missing 'name' field")
    if name != dir_name:
        raise ValueError(f"Name mismatch: frontmatter '{name}' vs directory '{dir_name}'")

    description = parsed.get("description")
    if not isinstance(description, str) or not description:
        raise ValueError("Missing 'description' field")

    metadata_obj = parsed.get("metadata")
    metadata = cast(dict[str, object], metadata_obj) if isinstance(metadata_obj, dict) else {}

    kind_obj = metadata.get("kind")
    kind = kind_obj if isinstance(kind_obj, str) else None
    if not isinstance(kind, str) or kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind: {kind}")

    invoke = _parse_invoke(metadata.get("invoke"))
    context = _parse_context(metadata.get("context")) if kind == "cognitive" else None
    output_format_obj = metadata.get("output-format")
    output_format = output_format_obj
    if output_format is not None and not isinstance(output_format, str):
        output_format = str(output_format)
    prompt_template, gotchas = _split_skill_body(body)

    return SkillMetadata(
        name=name,
        description=description,
        kind=kind,
        invoke=invoke,
        modality=_parse_modality(metadata.get("modality")),
        context=context,
        output_format=output_format,
        parameters=_parse_parameters(metadata.get("parameters") or metadata.get("params")),
        gotchas=gotchas,
        json_mode=metadata.get("json-mode") is True,
        soul_as_data=metadata.get("soul-as-data") is True,
        prompt_template=prompt_template if kind == "cognitive" else None,
        path=Path(dir_path),
        has_handler=(Path(dir_path) / "handler.py").exists(),
    )


def parse_frontmatter(yaml_text: str) -> dict[str, Any]:
    lines = yaml_text.splitlines()
    parsed, _ = _parse_yaml_block(lines, 0, 0)
    if not isinstance(parsed, dict):
        raise ValueError("Frontmatter must contain an object")
    return parsed


def _extract_frontmatter(raw: str) -> tuple[str, str]:
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        raise ValueError("No YAML frontmatter found")
    return match.group(1), raw[match.end():]


def _parse_value(raw: str) -> Any:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(item.strip()) for item in inner.split(",")]
    return raw


def _parse_yaml_block(
    lines: list[str],
    index: int,
    indent: int,
) -> tuple[dict[str, Any] | list[Any], int]:
    current = index
    while current < len(lines):
        line = lines[current]
        if not line.strip():
            current += 1
            continue
        if _indent_level(line) < indent:
            return {}, current
        break

    if current >= len(lines):
        return {}, current

    stripped = lines[current][_indent_level(lines[current]):]
    if stripped.startswith("- "):
        return _parse_yaml_list(lines, current, indent)
    return _parse_yaml_mapping(lines, current, indent)


def _parse_yaml_mapping(
    lines: list[str],
    index: int,
    indent: int,
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    current = index
    while current < len(lines):
        raw_line = lines[current]
        if not raw_line.strip():
            current += 1
            continue

        current_indent = _indent_level(raw_line)
        if current_indent < indent:
            break
        if current_indent != indent:
            raise ValueError(f"Invalid indentation near: {raw_line.strip()}")

        line = raw_line[current_indent:]
        match = _KEY_RE.match(line)
        if match is None:
            raise ValueError(f"Invalid frontmatter line: {line}")

        key = match.group(1)
        value = match.group(2).strip()
        current += 1

        if value in {">", "|"}:
            parsed_value, current = _parse_block_scalar(lines, current, indent + 2, folded=value == ">")
            result[key] = parsed_value
            continue

        if value == "":
            nested_value, current = _parse_yaml_block(lines, current, indent + 2)
            result[key] = nested_value
            continue

        result[key] = _parse_value(value)

    return result, current


def _parse_yaml_list(
    lines: list[str],
    index: int,
    indent: int,
) -> tuple[list[Any], int]:
    result: list[Any] = []
    current = index
    while current < len(lines):
        raw_line = lines[current]
        if not raw_line.strip():
            current += 1
            continue

        current_indent = _indent_level(raw_line)
        if current_indent < indent:
            break
        if current_indent != indent:
            raise ValueError(f"Invalid indentation near: {raw_line.strip()}")

        line = raw_line[current_indent:]
        if not line.startswith("- "):
            break

        value = line[2:].strip()
        current += 1
        if value in {">", "|"}:
            parsed_value, current = _parse_block_scalar(lines, current, indent + 2, folded=value == ">")
            result.append(parsed_value)
            continue
        if value == "":
            nested_value, current = _parse_yaml_block(lines, current, indent + 2)
            result.append(nested_value)
            continue
        result.append(_parse_value(value))

    return result, current


def _parse_block_scalar(
    lines: list[str],
    index: int,
    indent: int,
    *,
    folded: bool,
) -> tuple[str, int]:
    parts: list[str] = []
    current = index
    while current < len(lines):
        raw_line = lines[current]
        if not raw_line.strip():
            parts.append("")
            current += 1
            continue
        current_indent = _indent_level(raw_line)
        if current_indent < indent:
            break
        parts.append(raw_line[indent:])
        current += 1
    if folded:
        return " ".join(part.strip() for part in parts if part.strip()), current
    return "\n".join(parts).rstrip(), current


def _indent_level(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_invoke(value: Any) -> tuple[InvokeSource, ...]:
    items = _parse_string_sequence(value)
    if items is None:
        raise ValueError("Missing 'invoke' field in metadata")
    invalid = [item for item in items if item not in VALID_INVOKE]
    if invalid:
        raise ValueError(f"Invalid invoke source: {invalid[0]}")
    return tuple(cast(InvokeSource, item) for item in items if item in VALID_INVOKE)


def _parse_context(value: Any) -> tuple[ContextDimension, ...] | None:
    if value is None:
        return None
    items = _parse_string_sequence(value)
    if items is None:
        raise ValueError("Invalid context field")
    invalid = [item for item in items if item not in VALID_CONTEXT]
    if invalid:
        raise ValueError(f"Invalid context dimension: {invalid[0]}")
    return tuple(cast(ContextDimension, item) for item in items if item in VALID_CONTEXT)


def _parse_modality(value: Any) -> tuple[Modality, ...] | None:
    if value is None:
        return None
    items = _parse_string_sequence(value)
    if items is None:
        raise ValueError("Invalid modality field")
    invalid = [item for item in items if item not in VALID_MODALITY]
    if invalid:
        raise ValueError(f"Invalid modality: {invalid[0]}")
    return tuple(cast(Modality, item) for item in items if item in VALID_MODALITY)


def _parse_parameters(value: Any) -> SkillParameters | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid parameters field")
    required = _parse_string_sequence(value.get("required")) or []
    properties_value = value.get("properties")
    if properties_value is None:
        optional = _parse_string_sequence(value.get("optional")) or []
        property_names = tuple(dict.fromkeys((*required, *optional)))
        return SkillParameters(
            required=tuple(required),
            properties={name: "" for name in property_names},
        )
    if not isinstance(properties_value, dict):
        raise ValueError("Invalid parameters.properties field")
    properties: dict[str, str] = {}
    for key, raw_description in properties_value.items():
        if not isinstance(key, str):
            raise ValueError("Invalid parameters.properties key")
        if not isinstance(raw_description, str):
            raise ValueError(f"Invalid description for parameter: {key}")
        properties[key] = raw_description.strip()
    return SkillParameters(required=tuple(required), properties=properties)


def _parse_string_sequence(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        items: list[str] = []
        for item in cast(list[object], value):
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    return None


def _split_skill_body(body: str) -> tuple[str | None, str | None]:
    stripped = body.strip()
    if not stripped:
        return None, None
    parts = re.split(r"(?mi)^##\s+Gotchas\s*$", stripped, maxsplit=1)
    prompt_template = parts[0].strip() or None
    gotchas = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return prompt_template, gotchas


# ── self_context ──────────────────────────────────────────────────────────────

SEPARATOR = "\n\n---\n\n"


def build_self_context(ctx: SelfContext, skill: SkillMetadata) -> str:
    parts: list[str] = []
    dimensions = skill.context or ()

    if "identity" in dimensions:
        identity = ctx.identity if ctx.identity is not None else _read_optional_identity()
        if identity:
            parts.append(identity)

    if "soul" in dimensions and not skill.soul_as_data:
        soul = ctx.soul if ctx.soul is not None else _read_optional_soul()
        if soul:
            parts.append(soul)

    if "person" in dimensions and ctx.person is not None:
        person_name = ctx.person.name or "the caller"
        person_parts = [f"I'm thinking about {person_name}."]
        if ctx.person.summary:
            person_parts.append(f"What I know: {ctx.person.summary}")
        if ctx.person.facts:
            person_parts.append("Things I've noticed about them:")
            person_parts.extend(f"- {fact}" for fact in ctx.person.facts)
        parts.append("\n".join(person_parts))

    if "actions" in dimensions and ctx.actions:
        action_parts = ["Things I'm responsible for with this person:"]
        action_parts.extend(f"- {action}" for action in ctx.actions)
        parts.append("\n".join(action_parts))

    if "call-origin" in dimensions and ctx.call_origin is not None:
        if ctx.call_origin.direction == "inbound":
            parts.append("They called me. This was a conversation with a caller.")
        elif ctx.call_origin.direction == "outbound":
            if ctx.call_origin.action_intent:
                parts.append(f"I called them. Reason: {ctx.call_origin.action_intent}")
            else:
                parts.append("I called them.")
        else:
            parts.append("This was a text conversation.")

    if "recent-calls" in dimensions and ctx.recent_calls:
        recent_call_parts = ["Recent conversations with this person:"]
        recent_call_parts.extend(f"- {summary}" for summary in ctx.recent_calls)
        parts.append("\n".join(recent_call_parts))

    if "transcript" in dimensions and ctx.transcript:
        parts.append(f"Transcript of the conversation:\n{ctx.transcript}")

    if ctx.tool_context:
        parts.append(f"Context from the conversation: {ctx.tool_context}")

    template_vars = _build_template_vars(ctx)
    if skill.soul_as_data:
        template_vars["currentSoul"] = ctx.soul if ctx.soul is not None else _read_optional_soul()

    if skill.prompt_template:
        from mystic.prompts import render
        rendered = render(skill.prompt_template, template_vars).strip()
        if rendered:
            parts.append(rendered)

    if skill.output_format:
        parts.append(f"Return JSON: {skill.output_format}")

    return SEPARATOR.join(part for part in parts if part)


def _build_template_vars(ctx: SelfContext) -> dict[str, object]:
    variables: dict[str, object] = {}
    if ctx.person is not None:
        variables["personName"] = ctx.person.name or "the caller"
        if ctx.person.facts:
            variables["existingFacts"] = "\n".join(f"- {fact}" for fact in ctx.person.facts)
    if ctx.actions:
        variables["pendingActions"] = "\n".join(f"- {action}" for action in ctx.actions)
    if ctx.call_origin is not None:
        variables["callDirection"] = ctx.call_origin.direction
        variables["channel"] = ctx.call_origin.channel
        variables["modality"] = ctx.call_origin.modality
        if ctx.call_origin.action_intent:
            variables["actionIntent"] = ctx.call_origin.action_intent
    if ctx.recent_calls:
        variables["recentCalls"] = "\n".join(f"- {summary}" for summary in ctx.recent_calls)
    if ctx.tool_context:
        variables["toolContext"] = ctx.tool_context
    return variables


def _read_optional_identity() -> str:
    try:
        return read_identity_raw()
    except OSError:
        return ""


def _read_optional_soul() -> str:
    try:
        return read_soul()
    except OSError:
        return ""


# ── router ────────────────────────────────────────────────────────────────────


def can_invoke(skill: SkillMetadata, source: InvokeSource) -> bool:
    return source in skill.invoke


async def execute_tool_calls(
    db: sqlite3.Connection,
    ctx: SkillContext,
    tool_calls: list[ToolCall | Mapping[str, object]],
    *,
    audio_source: object | None = None,
) -> list[ToolResult]:
    registry = get_registry()
    results: list[ToolResult] = []

    for raw_call in tool_calls:
        call_id = raw_call.id if isinstance(raw_call, ToolCall) else str(raw_call.get("id", ""))
        call_name = "unknown"
        context: str | None = None
        try:
            call = _normalize_tool_call(raw_call)
            call_name = call.name
            context = cast(str | None, call.arguments.get("context"))
            source: FactSource = derive_source(ctx.audience, call.name)
            result = await _execute_skill(
                db,
                registry,
                ctx,
                source=source,
                skill_name=call.name,
                args=call.arguments,
                context=context,
                audio_source=audio_source,
            )
        except Exception as exc:
            result = f"Error: {exc}"
            logger.error(
                "skill.error",
                toolCallId=call_id,
                skill=call_name,
                personId=ctx.person_id,
                error=result,
            )
            results.append(ToolResult(tool_call_id=call_id, result=result))
            continue

        logger.info(
            "tool.call",
            skill=call.name,
            context=context,
            resultLength=len(result),
        )
        emit_event("activity", {
            "type": "skill_executed",
            "skill": call.name,
            "tool": call.name,
        })
        results.append(ToolResult(tool_call_id=call.id, result=result))

    return results


async def execute_cognitive_skill(
    skill_name: str,
    self_context: SelfContext,
    data: str,
    params: dict[str, Any] | None = None,
) -> str:
    registry = get_registry()
    skill = get_skill(registry, skill_name)
    if skill is None:
        raise ValueError(f"Cognitive skill not found: {skill_name}")
    if skill.kind != "cognitive":
        raise ValueError(f"Skill is not cognitive: {skill_name}")

    system_prompt = build_self_context(self_context, skill)
    if skill.has_handler:
        module = _load_handler_module(skill)
        handler = cast(CognitiveHandler, getattr(module, "execute"))
        return await handler(
            system_prompt,
            data,
            params or {},
            {"jsonMode": skill.json_mode},
        )

    return await invoke_agent(skill_name, system_prompt, data, json_mode=skill.json_mode)


def load_handler_module(skill: SkillMetadata) -> ModuleType:
    return _load_handler_module(skill)


async def _execute_skill(
    db: sqlite3.Connection,
    registry: SkillRegistry,
    ctx: SkillContext,
    *,
    source: FactSource,
    skill_name: str,
    args: dict[str, Any],
    context: str | None,
    audio_source: object | None,
) -> str:
    skill = get_skill(registry, skill_name)
    if skill is None:
        return f"Unknown skill: {skill_name}"

    invoke_source = cast(InvokeSource, ctx.audience)
    if not can_invoke(skill, invoke_source):
        return f"Permission denied: {ctx.audience} cannot use {skill_name}."

    if skill.kind == "cognitive":
        return await execute_cognitive_skill(
            skill.name,
            SelfContext(tool_context=context),
            _extract_cognitive_input(args),
            dict(args),
        )

    operational_context = OperationalContext(
        audience=ctx.audience,
        call_id=ctx.call_id,
        person_id=ctx.person_id,
        source=source,
        tool_context=context,
        audio_source=audio_source,
    )
    module = _load_handler_module(skill)
    handler = cast(OperationalHandler, getattr(module, "execute"))
    return await handler(db, operational_context, dict(args))


def _extract_cognitive_input(args: Mapping[str, object]) -> str:
    for key in ("instruction", "instructions", "content", "query"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_tool_call(raw_call: ToolCall | Mapping[str, object]) -> ToolCall:
    if isinstance(raw_call, ToolCall):
        return raw_call

    function_obj: object = raw_call.get("function")
    function = function_obj
    if not isinstance(function, Mapping):
        raise ValueError("Tool call is missing function metadata")
    function_map = cast(Mapping[str, object], function)

    raw_arguments_obj: object = function_map.get("arguments", {})
    if isinstance(raw_arguments_obj, str):
        decoded = json.loads(raw_arguments_obj)
        if not isinstance(decoded, dict):
            raise ValueError("Tool call arguments must decode to an object")
        raw_arguments_obj = cast(dict[str, object], decoded)
    if not isinstance(raw_arguments_obj, Mapping):
        raise ValueError("Tool call arguments must be an object")
    arguments_map = cast(Mapping[object, object], raw_arguments_obj)

    name_obj: object = function_map.get("name")
    name = name_obj
    if not isinstance(name, str) or not name:
        raise ValueError("Tool call is missing function name")

    arguments: dict[str, object] = {}
    for key, value in arguments_map.items():
        arguments[str(key)] = value

    return ToolCall(
        id=str(raw_call.get("id", "")),
        name=name,
        arguments=arguments,
    )


# ── tool schemas ──────────────────────────────────────────────────────────────

_INTEGER_TOOL_PARAMETERS = frozenset({"days", "min_duration_minutes", "timestamp"})
_ANY_TOOL_PARAMETERS = frozenset({"value"})


def build_skill_tool_schema(skill: SkillMetadata) -> dict[str, object]:
    parameters = skill.parameters
    properties: dict[str, object] = {}
    required: list[str] = []

    if parameters is not None:
        for name, description in parameters.properties.items():
            properties[name] = _build_parameter_schema(name, description)
        for name in parameters.required:
            if name not in properties:
                properties[name] = _build_parameter_schema(name, "")
        required = list(parameters.required)

    return {
        "type": "function",
        "function": {
            "name": skill.name,
            "description": _skill_tool_description(skill),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def build_tools_for_context(
    registry: SkillRegistry,
    audience: Audience,
    modality: Modality,
) -> list[dict[str, object]]:
    invoke_source = cast(InvokeSource, audience)
    tools: list[dict[str, object]] = []
    for skill in registry.values():
        if not can_invoke(skill, invoke_source):
            continue
        if skill.modality is not None and modality not in skill.modality:
            continue
        tools.append(build_skill_tool_schema(skill))
    return tools


def _build_parameter_schema(name: str, description: str) -> dict[str, object]:
    resolved_description = description or _default_parameter_description(name)
    if name in _INTEGER_TOOL_PARAMETERS:
        return {"type": "integer", "description": resolved_description}
    if name in _ANY_TOOL_PARAMETERS:
        return {
            "description": resolved_description,
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "integer"},
                {"type": "boolean"},
                {"type": "array", "items": {}},
                {"type": "object"},
            ],
        }
    return {"type": "string", "description": resolved_description}


def _default_parameter_description(name: str) -> str:
    return f"{name.replace('_', ' ')}."


def _skill_tool_description(skill: SkillMetadata) -> str:
    description = skill.description.strip()
    if skill.gotchas:
        return f"{description} Gotchas: {skill.gotchas}"
    return description


async def execute_tool(
    db: sqlite3.Connection,
    ctx: SkillContext,
    name: str,
    arguments: dict[str, object],
    *,
    audio_source: object | None = None,
) -> str:
    """Execute a single tool call by name. Transport-agnostic entry point."""
    filtered_arguments = {
        key: value
        for key, value in arguments.items()
        if value is not None and value != ""
    }
    results = await execute_tool_calls(
        db,
        ctx,
        [
            ToolCall(
                id=f"tool-{int(time.time() * 1000)}",
                name=name,
                arguments=filtered_arguments,
            )
        ],
        audio_source=audio_source,
    )
    return results[0].result if results else ""


def _load_handler_module(skill: SkillMetadata) -> ModuleType:
    handler_path = skill.path / "handler.py"
    if not handler_path.exists():
        raise FileNotFoundError(f"Skill handler not found: {handler_path}")

    module_name = f"mystic_skill_{skill.name.replace('-', '_')}"
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None) == str(handler_path):
        return cached

    spec = importlib.util.spec_from_file_location(module_name, str(handler_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load skill handler: {handler_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
