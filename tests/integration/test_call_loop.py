"""Call-loop integration for the refactored voice pipeline without LiveKit or hardware."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from livekit import rtc

from mystic.config import MoonshineSttConfig, PocketTtsConfig, ResolvedLLMConfig
from mystic.types import SkillContext
from mystic.voice import PipelineConfig, create_pipeline


def _silence_frame() -> rtc.AudioFrame:
    return rtc.AudioFrame(b"\x00\x00" * 320, 16_000, 1, 320)


class MockStt:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.seen_frames: list[rtc.AudioFrame] = []

    async def transcribe_frames(self, frames: Sequence[rtc.AudioFrame]) -> str:
        self.seen_frames.extend(frames)
        return self.transcript


class MockLlm:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def respond(self, prompt: str, transcript: str) -> str:
        self.calls.append((prompt, transcript))
        return self.response


class MockTts:
    def __init__(self) -> None:
        self.spoken_text: list[str] = []

    async def speak(self, text: str) -> bytes:
        self.spoken_text.append(text)
        return text.encode("utf-8")


class LoopbackAgent:
    def __init__(
        self,
        *,
        instructions: str,
        tools: list[object],
        stt: MockStt,
        tts: MockTts,
        llm: MockLlm,
        vad: object,
    ) -> None:
        self.instructions = instructions
        self.tools = tools
        self.stt = stt
        self.tts = tts
        self.llm = llm
        self.vad = vad

    async def run_turn(self, frames: Sequence[rtc.AudioFrame]) -> str:
        transcript = await self.stt.transcribe_frames(frames)
        response = await self.llm.respond(self.instructions, transcript)
        await self.tts.speak(response)
        return response


async def test_call_loop_composes_stt_llm_and_tts_without_livekit(integration_env) -> None:
    transcript = "Please move the follow-up to Tuesday afternoon and keep the budget packet ready."
    response = "Absolutely. I'll keep the Tuesday afternoon follow-up and budget packet on deck."
    stt = MockStt(transcript)
    llm = MockLlm(response)
    tts = MockTts()
    seen_voice_ids: list[str | None] = []

    async def fake_stt_factory(_config: object) -> MockStt:
        return stt

    async def fake_tts_factory(_config: object, voice_id: str | None) -> MockTts:
        seen_voice_ids.append(voice_id)
        return tts

    def fake_llm_factory(_config: object) -> MockLlm:
        return llm

    async def fake_vad_loader() -> object:
        return {"provider": "fake-vad"}

    agent = cast(LoopbackAgent, await create_pipeline(
        PipelineConfig(
            stt=MoonshineSttConfig(provider="moonshine", model="small"),
            tts=PocketTtsConfig(provider="pocket", model=None, pythonCommand=None),
            llm=ResolvedLLMConfig(baseURL="http://llm.local", apiKey="test-key", model="test-model"),
        ),
        "You are a calm scheduling assistant.",
        "Hades",
        integration_env.db,
        SkillContext(
            audience="public",
            direction="inbound",
            channel="phone",
            modality="voice",
            call_id="call-loop-1",
            person_id="person-loop-1",
            source="mid-call",
        ),
        agent_cls=LoopbackAgent,
        stt_factory=fake_stt_factory,
        tts_factory=fake_tts_factory,
        llm_factory=fake_llm_factory,
        vad_loader=fake_vad_loader,
    ))

    frames = [_silence_frame(), _silence_frame()]
    spoken = await agent.run_turn(frames)

    assert spoken == response
    assert len(stt.seen_frames) == 2
    assert stt.seen_frames[0] is frames[0]
    assert stt.seen_frames[1] is frames[1]
    assert len(llm.calls) == 1
    assert llm.calls[0][0].startswith("You are a calm scheduling assistant.")
    assert llm.calls[0][1] == transcript
    assert tts.spoken_text == [response]
    assert seen_voice_ids == ["Hades"]
    tool_names = {
        getattr(getattr(tool, "info", None), "name", "")
        for tool in agent.tools
    }
    assert "chat" in tool_names
    assert len(agent.tools) == 13
