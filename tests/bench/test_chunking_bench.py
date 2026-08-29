"""Benchmarks for text chunking — runs during transcript indexing."""

from __future__ import annotations

import pytest

from mystic.memory import chunk_text

SHORT_TEXT = "Hello, this is a brief message. " * 5  # ~160 chars, single chunk
MEDIUM_TEXT = (
    "The quarterly report shows a 15% increase in engagement metrics across all regions. "
    "We need to follow up with the marketing team about campaign attribution. "
    "The next board meeting is scheduled for Tuesday afternoon. "
) * 20  # ~5k chars

LONG_TEXT = (
    "Caller: Hi, I wanted to discuss the project timeline and budget allocations.\n"
    "Agent: Of course. Let me pull up the latest figures from the quarterly review.\n"
    "Caller: The main concern is that the infrastructure costs have gone up by about 20%.\n"
    "Agent: I see that reflected in the November data. Would you like me to prepare a summary?\n"
    "Caller: Yes, and include the projected savings from the migration plan.\n\n"
) * 40  # ~20k chars, realistic long call transcript


@pytest.mark.bench
class TestChunking:
    def test_short_text(self, benchmark):
        benchmark(chunk_text, SHORT_TEXT)

    def test_medium_text(self, benchmark):
        benchmark(chunk_text, MEDIUM_TEXT)

    def test_long_transcript(self, benchmark):
        benchmark(chunk_text, LONG_TEXT)

    def test_custom_params(self, benchmark):
        benchmark(chunk_text, LONG_TEXT, chunk_size=1000, overlap=200)
