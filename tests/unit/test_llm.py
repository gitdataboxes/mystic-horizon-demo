from __future__ import annotations

import json
import unittest
from typing import Mapping, cast
from unittest.mock import AsyncMock, call, patch

from mystic.config import clear_config_cache
from mystic.http import HttpResponse
from mystic.llm import DEFAULT_LLM_MAX_TOKENS, invoke_agent, parse_json, stream_llm_with_tools
from mystic.config import ResolvedLLMConfig
from tests.python_helpers import TempAppHome, TEST_INTELLIGENCE_CONFIG, TEST_PROVIDERS_CONFIG, seed_core_files


class LLMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_home = TempAppHome()
        self.home = self.temp_home.__enter__()
        seed_core_files(self.home)
        clear_config_cache()

    def tearDown(self) -> None:
        clear_config_cache()
        self.temp_home.__exit__(None, None, None)

    async def test_invoke_agent_hardcodes_read_search_to_openrouter_sonar(self) -> None:
        captured: dict[str, object] = {}

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["payload"] = json.loads((payload or b"").decode("utf-8"))
            captured["timeout"] = timeout
            return HttpResponse(
                status_code=200,
                content=b'{"choices":[{"message":{"content":"search result"}}]}',
            )

        result = await invoke_agent(
            "read-search",
            "system prompt",
            "hello",
            json_mode=True,
            transport=transport,
        )

        self.assertEqual(result, "search result")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertGreater(float(cast(float, captured["timeout"])), 0)
        self.assertEqual(
            captured["payload"],
            {
                "model": "perplexity/sonar-pro",
                "messages": [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "hello"},
                ],
                "max_tokens": DEFAULT_LLM_MAX_TOKENS,
            },
        )
        headers = cast(dict[str, str], captured["headers"])
        self.assertEqual(headers["Authorization"], "Bearer test-openrouter-key")
        self.assertEqual(headers["HTTP-Referer"], "https://github.com/gitdataboxes/mystic-horizon-demo")
        self.assertEqual(headers["X-Title"], "Mystic Horizon (demo)")

    async def test_invoke_agent_omits_json_mode_for_sonar_models(self) -> None:
        intelligence = dict(TEST_INTELLIGENCE_CONFIG)
        intelligence["search"] = {"model": "perplexity/sonar-pro"}
        seed_core_files(self.home, intelligence=intelligence)
        clear_config_cache()

        captured_body: dict[str, object] = {}

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, url, headers, timeout
            captured_body.update(json.loads((payload or b"").decode("utf-8")))
            return HttpResponse(
                status_code=200,
                content=b'{"choices":[{"message":{"content":"ok"}}]}',
            )

        await invoke_agent("read-search", "", "question", json_mode=True, transport=transport)
        self.assertNotIn("response_format", captured_body)
        self.assertEqual(captured_body["model"], "perplexity/sonar-pro")
        self.assertEqual(captured_body["max_tokens"], DEFAULT_LLM_MAX_TOKENS)

    async def test_invoke_agent_supports_custom_backend_without_openrouter_headers(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["llm"] = {
            "backend": {
                "provider": "custom",
                "baseURL": "http://localhost:11434/v1",
                "apiKey": "local-key",
            }
        }
        seed_core_files(self.home, providers=providers)
        clear_config_cache()

        captured_headers: dict[str, str] = {}
        captured_url = ""

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, payload, timeout
            nonlocal captured_url
            captured_headers.update(dict(headers))
            captured_url = url
            return HttpResponse(
                status_code=200,
                content=b'{"choices":[{"message":{"content":"edited"}}]}',
            )

        result = await invoke_agent("editing", "sys", "user", transport=transport)

        self.assertEqual(result, "edited")
        self.assertEqual(captured_url, "http://localhost:11434/v1/chat/completions")
        self.assertEqual(captured_headers["Authorization"], "Bearer local-key")
        self.assertNotIn("HTTP-Referer", captured_headers)
        self.assertNotIn("X-Title", captured_headers)

    async def test_read_search_ignores_custom_backend(self) -> None:
        providers = dict(TEST_PROVIDERS_CONFIG)
        providers["llm"] = {
            "backend": {
                "provider": "custom",
                "baseURL": "http://localhost:11434/v1",
                "apiKey": "local-key",
            }
        }
        seed_core_files(self.home, providers=providers)
        clear_config_cache()

        captured: dict[str, object] = {}

        async def transport(
            method: str,
            url: str,
            headers: Mapping[str, str],
            payload: bytes | None,
            timeout: float,
        ) -> HttpResponse:
            del method, timeout
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["payload"] = json.loads((payload or b"").decode("utf-8"))
            return HttpResponse(
                status_code=200,
                content=b'{"choices":[{"message":{"content":"search result"}}]}',
            )

        result = await invoke_agent("read-search", "", "news", json_mode=True, transport=transport)

        self.assertEqual(result, "search result")
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(
            captured["payload"],
            {
                "model": "perplexity/sonar-pro",
                "messages": [{"role": "user", "content": "news"}],
                "max_tokens": DEFAULT_LLM_MAX_TOKENS,
            },
        )
        headers = cast(dict[str, str], captured["headers"])
        self.assertEqual(headers["Authorization"], "Bearer test-openrouter-key")
        self.assertEqual(headers["HTTP-Referer"], "https://github.com/gitdataboxes/mystic-horizon-demo")
        self.assertEqual(headers["X-Title"], "Mystic Horizon (demo)")

    def test_parse_json_accepts_code_blocks_and_wrapped_arrays(self) -> None:
        self.assertEqual(parse_json('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(parse_json("Result:\n[1, 2, 3]\nThanks."), [1, 2, 3])

    def test_parse_json_raises_on_invalid_content(self) -> None:
        with self.assertRaisesRegex(ValueError, "Failed to parse JSON"):
            parse_json("not json at all")

    async def test_stream_llm_with_tools_awaits_async_on_text_callback(self) -> None:
        class FakeStreamResponse:
            def __init__(self, lines: list[str]) -> None:
                self.lines = lines
                self.status_code = 200

            async def __aenter__(self) -> "FakeStreamResponse":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb
                return None

            async def aread(self) -> bytes:
                return b""

            async def aiter_lines(self):
                for line in self.lines:
                    yield line

        class FakeAsyncClient:
            requests: list[dict[str, object]] = []

            def __init__(self, *, timeout: float) -> None:
                self.timeout = timeout

            async def __aenter__(self) -> "FakeAsyncClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb
                return None

            def stream(self, *args, **kwargs) -> FakeStreamResponse:
                del args
                self.requests.append(dict(kwargs))
                return FakeStreamResponse([
                    'data: {"choices":[{"delta":{"content":"Hello"}}]}',
                    'data: {"choices":[{"delta":{"content":" there"}}]}',
                    "data: [DONE]",
                ])

        on_text = AsyncMock()

        with patch("mystic.llm.httpx.AsyncClient", new=FakeAsyncClient):
            result = await stream_llm_with_tools(
                [{"role": "user", "content": "hi"}],
                ResolvedLLMConfig(
                    baseURL="http://localhost:11434/v1",
                    apiKey="local-key",
                    model="test-model",
                ),
                tools=[],
                execute_fn=AsyncMock(),
                on_text=on_text,
            )

        self.assertEqual(result, "Hello there")
        self.assertEqual(
            FakeAsyncClient.requests[0]["json"]["max_tokens"],
            DEFAULT_LLM_MAX_TOKENS,
        )
        on_text.assert_has_awaits([call("Hello"), call(" there")])


if __name__ == "__main__":
    unittest.main()
