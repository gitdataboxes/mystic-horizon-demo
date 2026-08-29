# Dashboard

## What is the dashboard?

The dashboard is a web page served from your local machine. It's the main way
to interact with the agent when you're not calling by phone. Open it in a
browser at localhost on the agent's configured port. You need to log in with a
token that's generated when the agent starts.

## What pages does the dashboard have?

The dashboard has these pages:

- **Live** — The main conversation page. Voice and text chat with the agent
  happen here. Shows a real-time transcript, a chat feed, and voice controls
  (mic toggle, waveform display).
- **Settings** — Configure providers: voice (STT and TTS), LLM, phone
  (Tailscale and Twilio), and email (SMTP). This is where you enter API keys
  and choose between local and cloud providers.
- **Setup** — First-time onboarding. Walks through initial configuration:
  downloading dependencies, choosing providers, and getting the agent ready to
  run.

The dashboard also has detail views for calls, people, and actions that you can
navigate to from the live page.

## How does voice chat work in the dashboard?

When you open the Live page, your browser connects directly to a LiveKit room
running on your machine. Click the mic toggle to start speaking. Your audio goes
through the voice pipeline: speech-to-text converts your words, the LLM
generates a response, and text-to-speech speaks it back. The conversation
transcript appears in real time. You can also type text messages in the chat
input — text and voice share the same conversation.

## How does text chat work?

Type a message in the chat input on the Live page. Text messages go through the
same LLM pipeline as voice — the agent processes your message and responds.
Text responses appear in the chat feed. You can mix voice and text freely in the
same conversation.

## What are the voice provider options?

Speech-to-text (STT) options:
- **Moonshine** — Runs locally on your machine. No API key needed, no data
  leaves your computer. Supports tiny, small, and medium model sizes.
- **Deepgram** — Cloud service. Requires an API key. Generally more accurate,
  especially in noisy environments. Uses the nova-3 model.

Text-to-speech (TTS) options:
- **Pocket TTS** — Runs locally using ONNX models. No API key needed. Limited
  to one active synthesis job at a time.
- **Inworld** — Cloud service. Requires an API key. Supports concurrent
  synthesis, generally lower latency for longer responses.

Choose providers on the Settings page. Local providers keep everything on your
machine. Cloud providers need internet and API keys but may offer better quality.
