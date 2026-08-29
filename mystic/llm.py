"""LLM transport and JSON parsing helpers."""

from __future__ import annotations

import json
import inspect
import re
from collections.abc import AsyncGenerator, Callable, Awaitable, Mapping
from typing import Literal, cast

import httpx

from mystic.config import (
    OPENROUTER_BASE_URL,
    ResolvedBackendLLMConfig,
    ResolvedLLMConfig,
    get_backend_llm_config,
    get_intelligence_config,
    get_providers_config,
    get_trace_id,
    logger,
)
from mystic.http import RequestTransport, fetch_with_timeout

LLM_TIMEOUT_MS = 45_000
DEFAULT_LLM_MAX_TOKENS = 4096
OPENROUTER_SEARCH_MODEL = "perplexity/sonar-pro"

LLMTask = Literal[
    "extraction.facts",
    "extraction.commitments",
    "judgment.scheduler",
    "judgment.satisfaction",
    "judgment.owner_call",
    "summarization.person",
    "summarization.call",
    "editing",
    "search",
]

SKILL_TO_TASK: dict[str, LLMTask] = {
    "summarize-call": "summarization.call",
    "extract-facts": "extraction.facts",
    "extract-commitments": "extraction.commitments",
    "summarize-person": "summarization.person",
    "check-satisfaction": "judgment.satisfaction",
    "judge-schedule": "judgment.scheduler",
    "edit-soul": "editing",
    "edit-prompt": "editing",
    "design-dashboard": "editing",
    "read-search": "search",
}

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```")


def build_llm_headers(api_key: str | None, *, is_openrouter: bool = True) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if is_openrouter:
        headers["HTTP-Referer"] = "https://github.com/gitdataboxes/mystic-horizon-demo"
        headers["X-Title"] = "Mystic Horizon (demo)"
    return headers


def extract_chat_content(payload: object) -> str:
    payload_map: dict[str, object] = (
        dict(cast(Mapping[str, object], payload)) if isinstance(payload, Mapping) else {}
    )
    choices_obj: object = payload_map.get("choices")
    choices = cast(list[object], choices_obj) if isinstance(choices_obj, list) else []
    first_choice_obj: object = choices[0] if choices else None
    first_choice: dict[str, object] = (
        dict(cast(Mapping[str, object], first_choice_obj))
        if isinstance(first_choice_obj, Mapping)
        else {}
    )
    message_obj: object = first_choice.get("message")
    message: dict[str, object] = (
        dict(cast(Mapping[str, object], message_obj)) if isinstance(message_obj, Mapping) else {}
    )
    return _coerce_message_content(message.get("content", ""))


async def invoke_agent(
    task: str,
    system_prompt: str,
    data: str,
    *,
    json_mode: bool = False,
    transport: RequestTransport | None = None,
) -> str:
    llm_task = SKILL_TO_TASK.get(task, task)
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": data})
    return await _send_llm_request(
        llm_task,
        messages,
        json_mode=json_mode,
        transport=transport,
    )


async def stream_llm_tokens(
    messages: list[dict[str, str]],
    config: ResolvedLLMConfig,
    *,
    timeout_ms: int = LLM_TIMEOUT_MS,
) -> AsyncGenerator[str, None]:
    """Async generator that yields text chunks from a streaming LLM response."""
    is_openrouter = "openrouter.ai" in config.baseURL
    headers = build_llm_headers(config.apiKey, is_openrouter=is_openrouter)
    trace_id = get_trace_id()
    if trace_id:
        headers["X-Request-Id"] = trace_id
    body: dict[str, object] = {
        "model": config.model,
        "messages": messages,
        "stream": True,
        "max_tokens": DEFAULT_LLM_MAX_TOKENS,
    }
    timeout = timeout_ms / 1000
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{config.baseURL.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        ) as response:
            if not 200 <= response.status_code < 300:
                raw = await response.aread()
                raise RuntimeError(f"LLM API error ({response.status_code}): {raw.decode()}")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                    delta = event["choices"][0]["delta"].get("content") or ""
                    if delta:
                        yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue


ToolExecutor = Callable[[str, dict[str, object]], Awaitable[str]]


async def stream_llm_with_tools(
    messages: list[dict[str, object]],
    config: ResolvedLLMConfig,
    *,
    tools: list[dict[str, object]],
    execute_fn: ToolExecutor,
    on_text: Callable[[str], None | Awaitable[None]] | None = None,
    timeout_ms: int = LLM_TIMEOUT_MS,
    max_rounds: int = 10,
) -> str:
    """Streaming LLM loop with tool-call support.

    Sends messages with tool schemas, handles tool_calls in the response by
    calling *execute_fn(name, arguments)* for each, appends results, and
    loops until the model returns a text response or *max_rounds* is reached.

    Returns the final assistant text.
    """
    is_openrouter = "openrouter.ai" in config.baseURL
    headers = build_llm_headers(config.apiKey, is_openrouter=is_openrouter)
    trace_id = get_trace_id()
    if trace_id:
        headers["X-Request-Id"] = trace_id
    timeout = timeout_ms / 1000
    content_chunks: list[str] = []

    for _round in range(max_rounds):
        body: dict[str, object] = {
            "model": config.model,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "max_tokens": DEFAULT_LLM_MAX_TOKENS,
        }

        content_chunks = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{config.baseURL.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raw = await response.aread()
                    raise RuntimeError(f"LLM API error ({response.status_code}): {raw.decode()}")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                        delta = event["choices"][0]["delta"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                    # Text content
                    text = delta.get("content") or ""
                    if text:
                        content_chunks.append(text)
                        if on_text:
                            result = on_text(text)
                            if inspect.isawaitable(result):
                                await result

                    # Tool call deltas
                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        entry = tool_calls_by_index.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc_delta.get("id"):
                            entry["id"] = tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            entry["name"] = fn["name"]
                        if fn.get("arguments"):
                            entry["arguments"] += fn["arguments"]

        # If no tool calls, we're done — return text
        if not tool_calls_by_index:
            return "".join(content_chunks)

        # Build assistant message with tool_calls
        assistant_tool_calls = []
        for idx in sorted(tool_calls_by_index):
            tc = tool_calls_by_index[idx]
            assistant_tool_calls.append({
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            })

        assistant_msg: dict[str, object] = {"role": "assistant", "tool_calls": assistant_tool_calls}
        if content_chunks:
            assistant_msg["content"] = "".join(content_chunks)
        messages.append(assistant_msg)

        # Execute each tool call and append results
        for tc_msg in assistant_tool_calls:
            fn_info = cast(dict[str, str], tc_msg["function"])
            try:
                args = json.loads(fn_info["arguments"]) if fn_info["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            result = await execute_fn(fn_info["name"], args)
            messages.append({
                "role": "tool",
                "tool_call_id": cast(str, tc_msg["id"]),
                "content": result,
            })

    # Exhausted rounds — return whatever text we have
    return "".join(content_chunks)


def parse_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = _CODE_BLOCK_RE.search(raw)
    if match and match.group(1):
        return json.loads(match.group(1))

    start_obj = raw.find("{")
    start_arr = raw.find("[")
    if start_obj >= 0 and start_arr >= 0:
        start = min(start_obj, start_arr)
    else:
        start = max(start_obj, start_arr)

    if start >= 0:
        is_array = raw[start] == "["
        end = raw.rfind("]" if is_array else "}")
        if end > start:
            return json.loads(raw[start : end + 1])

    snippet = raw[:200]
    raise ValueError(f"Failed to parse JSON from LLM response: {snippet}")


async def _send_llm_request(
    task: str,
    messages: list[dict[str, str]],
    *,
    json_mode: bool,
    transport: RequestTransport | None,
) -> str:
    if task == "search":
        model = OPENROUTER_SEARCH_MODEL
        backend = _get_openrouter_search_backend()
    else:
        intelligence = get_intelligence_config()
        model = _get_model_for_task(intelligence, task)
        backend = get_backend_llm_config()
    is_search_mode = "sonar" in model.lower() or "perplexity" in model.lower()

    body: dict[str, object] = {
        "model": model,
        "messages": messages,
        "max_tokens": DEFAULT_LLM_MAX_TOKENS,
    }
    if json_mode and not is_search_mode:
        body["response_format"] = {"type": "json_object"}

    headers = build_llm_headers(backend.apiKey, is_openrouter=not backend.isCustom)
    trace_id = get_trace_id()
    if trace_id:
        headers["X-Request-Id"] = trace_id

    logger.debug(
        "llm.request",
        task=task,
        model=model,
        messageCount=len(messages),
        isCustom=backend.isCustom,
    )

    response = await fetch_with_timeout(
        f"{backend.baseURL}/chat/completions",
        method="POST",
        headers=headers,
        json_body=body,
        timeout_ms=LLM_TIMEOUT_MS,
        timeout_label=f"llm.{task}",
        transport=transport,
    )

    if not 200 <= response.status_code < 300:
        error_text = response.text
        logger.error(
            "llm.error",
            task=task,
            model=model,
            status=response.status_code,
            error=error_text,
        )
        raise RuntimeError(f"LLM API error ({response.status_code}): {error_text}")

    content = extract_chat_content(response.json())

    logger.debug("llm.response", task=task, model=model, length=len(content))
    return content


def _get_openrouter_search_backend() -> ResolvedBackendLLMConfig:
    providers = get_providers_config()
    api_key = providers.openrouter.apiKey if providers.openrouter else None
    if not api_key:
        raise ValueError("OpenRouter API key required for search LLM")
    return ResolvedBackendLLMConfig(baseURL=OPENROUTER_BASE_URL, apiKey=api_key, isCustom=False)


def _get_model_for_task(intelligence: object, task: str) -> str:
    parts = task.split(".")
    if len(parts) == 2:
        category_name, sub_key = parts
        category = getattr(intelligence, category_name, None)
        if category is not None and hasattr(category, sub_key):
            entry = getattr(category, sub_key)
            model = getattr(entry, "model", None)
            if isinstance(model, str):
                return model

    entry = getattr(intelligence, task, None)
    model = getattr(entry, "model", None) if entry is not None else None
    if isinstance(model, str):
        return model

    raise ValueError(f"No model configured for task: {task}")


def _coerce_message_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in cast(list[object], value):
            if isinstance(item, Mapping):
                item_map = cast(Mapping[str, object], item)
                text: object = item_map.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            parts.append(json.dumps(item, default=str))
        return "".join(parts)
    if value is None:
        return ""
    return json.dumps(value, default=str)
