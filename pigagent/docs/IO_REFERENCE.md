# Agent I/O Reference

How to configure, invoke, and communicate with the AI voice agent from other
modules in a larger project.

## Overview

The agent is a Python process that connects to LiveKit Cloud, joins a room,
listens for user audio, runs it through an STT→LLM→TTS pipeline, and streams
synthesized speech back. It also exposes a Flask-based token server and a
LiveKit data channel for text-level integration.

```
                    ┌──────────────┐
   .env ───────────►│              │──► TTS audio (LiveKit room)
   .config ────────►│    agent     │──► LiveKit data messages (JSON over WebRTC)
   LiveKit room ───►│  (main.py)   │──► loguru logs (stderr + file)
                    │              │──► token_server.py :3000 (HTTP)
                    └──────────────┘
```

---

## 1. Inputs (what the agent reads)

### 1a. `.env` — Secrets (required)

```
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
DEEPGRAM_API_KEY=...       # if STT_PROVIDER=deepgram
CARTESIA_API_KEY=...        # if STT_PROVIDER=cartesia, or always for TTS
XAI_API_KEY=...             # if LLM_PROVIDER=grok
DASHSCOPE_API_KEY=...       # if LLM_PROVIDER=qwen
DASHSCOPE_US_API_KEY=...    # if LLM_PROVIDER=qwen-us
PERPLEXITY_API_KEY=...      # if POLICY_SEARCH_BACKEND=perplexity
```

### 1b. `.config` — Behaviour (TOML flat key=value)

All agent behaviour is configured via `pigagent/.config`. See [config.py](config.py)
for every available key. The key groups:

| Category | Keys | Controls |
|---|---|---|
| Provider selection | `STT_PROVIDER`, `LLM_PROVIDER`, `POLICY_SEARCH_BACKEND` | Which services to use |
| STT | `DEEPGRAM_STT_MODEL`, `DEEPGRAM_ENABLE_DIARIZATION`, ... | Speech recognition |
| TTS | `CARTESIA_TTS_MODEL`, `CARTESIA_TTS_VOICE`, `CARTESIA_TTS_SPEED`, ... | Voice synthesis |
| LLM | `QWEN_MODEL`, `GROK_MODEL`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS` | Model & generation |
| Agent mode | `AGENT_MODE` (1/2/3), `ENABLE_INTERRUPTIONS`, `ENABLE_FILLER_WORDS` | Conversation behaviour |
| Search | `ENABLE_POLICY_SEARCH`, `FORCE_POLICY_SEARCH`, `POLICY_SEARCH_BACKEND` | Web search |
| Mode 3 | `GROUP_RESPONSE_COOLDOWN_SECONDS`, `GROUP_MIN_TURNS_BEFORE_RESPONSE`, ... | Group discussion gating |
| Logging | `LOG_LEVEL`, `LOG_TO_FILE`, `LOG_FILE_PATH` | Log output |

### 1c. LiveKit room — Audio & participant events

The agent subscribes to a LiveKit room (`AutoSubscribe.AUDIO_ONLY`).
Input arrives as:

- **User audio** — PCM audio track from room participants. The agent applies
  Silero VAD (voice activity detection), then streams it to STT.
- **Participant join/leave** — `participant_connected` / `participant_disconnected`
  events logged for tracking.
- **Room metadata** — optional JSON in `ctx.job.metadata`. If present, it is
  appended to the LLM system prompt as context lines (`- key: value`).

### 1d. Job context (from LiveKit)

When the agent worker is dispatched (via `cli.run_app`), it receives a
`JobContext` containing:

| Field | Used for |
|---|---|
| `ctx.room.name` | Logging |
| `ctx.job.id` | Logging |
| `ctx.job.metadata` | Optional JSON merged into LLM instructions |
| `ctx.room.remote_participants` | Counting connected participants |

---

## 2. Outputs (what the agent produces)

### 2a. TTS audio (LiveKit room — primary output)

The agent publishes synthesized speech into the LiveKit room. Audio format:

- **Codec**: PCM 16-bit signed, 24000 Hz (default; configurable)
- **Model**: Cartesia `sonic-3` (configurable)
- **Streaming**: Audio is streamed as it's generated (word-level timestamps enabled)

The pipeline: `LLM text → TTS synthesis → LiveKit audio track → user's browser`

### 2b. LiveKit data messages (JSON over WebRTC data channel)

Three reliable (reliable=True) data topics are published to the room.
These are the primary integration surface for other modules.

**Topic: `user_transcript`** — sent when a final STT transcript arrives

```json
{
  "text": "what the user said",
  "speaker_id": 2           // integer, or null if diarization disabled
}
```

**Topic: `agent_response`** — sent when the LLM finishes generating

```
Raw UTF-8 text of the agent's full response.
```

**Topic: `agent_voice_started`** — sent when TTS audio begins playing

```
"started"  (literal bytes)
```

**Mode 2 only — Topic: `interrupt_debug`** — periodic status messages

```
"Interrupt check: 12.5s / 30.0s | Agent: listening"
"INTERRUPT TRIGGERED after 31.2s"
"LLM Interrupt: ..."
```

### 2c. Logging (loguru)

Output goes to:
1. **stderr** — colourised, human-readable (level from `LOG_LEVEL` config)
2. **File** — `logs/agent_YYYY-MM-DD.log` with daily rotation, 7-day retention

Log format: `timestamp | level | module:function:line - message`

Key log markers other modules can grep for:

| Pattern | Meaning |
|---|---|
| `🎤 [DEBUG] User started speaking` | Turn begins |
| `👤 [STT] User transcribed:` | Final transcript |
| `🤖 [DEBUG] Agent started THINKING` | LLM request sent |
| `⏱️ [TIMING] T0:` through `T5:` | Per-turn latency breakdown |
| `🤖 [LLM] Response:` | LLM output |
| `⏱️ [TIMING] T5:` | Agent audio playing (turn complete) |
| `🔇 [MODE 3] Suppressed` | Agent chose not to respond |

### 2d. Token server (HTTP)

A separate lightweight Flask process (`token_server.py`) runs on `:3000`.

| Endpoint | Method | Response | Used by |
|---|---|---|---|
| `/token` | GET | `{"token": "<jwt>", "url": "wss://..."}` | Web client fetches a LiveKit access token |
| `/health` | GET | `{"status": "ok", "service": "token-server"}` | Health checks |

The token grants: `room_join`, `can_publish`, `can_subscribe`, `can_publish_data`
for room `test-room` with identity `engineer`.

---

## 3. Invocation

### Start the agent

```bash
cd agent
source .venv/bin/activate
python main.py start           # production mode
python main.py dev             # development mode (LiveKit dev server)
```

`main.py` calls `cli.run_app(WorkerOptions(...))` which connects to the
LiveKit Cloud WSS URL and waits for room dispatch.

### Start the token server

```bash
cd agent
source .venv/bin/activate
python token_server.py         # listens on 0.0.0.0:3000
```

Both must be running for a user to join a room and talk to the agent.

### Stop

```bash
# token_server: Ctrl+C (SIGINT)
# agent: Ctrl+C, or kill the process
# Both are nohup-safe — see stop.sh / status.sh for management scripts
```

### Current deployment (nohup pattern)

```bash
cd ~/trump-ai-livekit/agent
source .venv/bin/activate
nohup python token_server.py > logs/token_server.log 2>&1 &
nohup python main.py start > logs/agent.log 2>&1 &
```

---

## 4. Interfacing from another module

### 4a. Subscribe to LiveKit data messages

The cleanest way to get real-time agent output (transcripts, responses,
timing signals) is to join the same LiveKit room and listen on the data
channel. Any LiveKit client (Python, JS, Go, Rust) can do this:

```
Room.on("data_received", topic="user_transcript")  → JSON {"text": ..., "speaker_id": ...}
Room.on("data_received", topic="agent_response")    → raw text
Room.on("data_received", topic="agent_voice_started") → "started"
```

### 4b. Send text to the agent via data channel

The agent has a fallback text path (`on_data_received` handler, line ~1470
of main.py) for non-plugin providers. For the primary plugin path, text
input goes through the STT pipeline (user speaks → STT produces text →
LLM). There is currently no direct "inject text" data channel for the
plugin path — text injection would require adding a topic-based handler.

### 4c. Pass metadata at dispatch time

When creating the LiveKit room dispatch (server-side), include a JSON
metadata string in the job. The agent will merge it into the LLM system
prompt as context:

```json
{"topic": "customer_support", "user_name": "Alice", "urgency": "high"}
```

Appears in the LLM prompt as:

```
Context:
- topic: customer_support
- user_name: Alice
- urgency: high
```

### 4d. Call the health endpoint

```bash
curl http://localhost:3000/health
# {"status": "ok", "service": "token-server"}
```

### 4e. Parsing log output

If your system reads the agent's stdout/stderr, the structured timing block
(`⏱️ [TIMING] T0:` through `T5:`) gives per-turn latency data. The summary
ends with `VERDICT: ✅ EXCELLENT / ⚠️ ACCEPTABLE / ❌ SLOW`.

---

## 5. Configuration override from an external module

If a parent process wants to override `.config` values at launch without
editing the file, it can set environment variables. Pydantic Settings
(on `AgentConfig`) will pick them up:

```bash
AGENT_MODE=2 INTERRUPT_INTERVAL_SECONDS=15 python main.py start
```

Note: `.env` is for secrets only (API keys). `.config` is for behaviour.
Environment variable overrides work for config keys but this is not the
project's intended pattern — prefer editing `.config` directly.

---

## 6. Dependencies

From `pyproject.toml`:

- **Python**: ≥3.12
- **LiveKit**: `livekit~=1.0`, `livekit-agents[silero,turn-detector]~=1.3`
- **Plugins**: OpenAI (`livekit-plugins-openai`), Deepgram, Cartesia, noise cancellation
- **TTS**: Cartesia (always required — the project has no other TTS backend)
- **LLM**: OpenAI-compatible endpoints (Qwen DashScope or Grok xAI)
- **Token server**: Flask + flask-cors
- **Package manager**: uv

---

## 7. Quick reference for integrators

| Concern | Answer |
|---|---|
| How do I start the agent? | `python main.py start` from `pigagent/` with venv active |
| How does the agent get user speech? | Auto-subscribes to LiveKit room audio, runs VAD → STT |
| How does the agent speak back? | LLM text → Cartesia TTS → LiveKit audio track |
| How do I get transcripts out? | Subscribe to LiveKit data topic `user_transcript` |
| How do I get agent responses as text? | Subscribe to LiveKit data topic `agent_response` |
| How do I know when the agent finishes speaking? | Listen for `agent_voice_started` data topic |
| How do I pass context to the LLM? | Include JSON in LiveKit job metadata |
| How do I change the voice/emotion? | Edit `.config` — `CARTESIA_TTS_VOICE`, `CARTESIA_TTS_EMOTION` |
| How do I change the LLM provider? | Set `LLM_PROVIDER` in `.config` and the corresponding API key in `.env` |
| How do I switch agent modes? | Set `AGENT_MODE` in `.config` (1=default, 2=interrupt, 3=group) |
| How do I enable web search? | Set `ENABLE_POLICY_SEARCH=true` and pick `POLICY_SEARCH_BACKEND` |
| What ports does it need? | None inbound (outbound WSS to LiveKit Cloud only), except token_server :3000 |
