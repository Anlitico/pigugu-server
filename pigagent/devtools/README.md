# Pigugu Agent Chat Test Tools

## Quick Start

```bash
cd pigagent/devtools
python debug_server.py
```

Open http://localhost:9000/chat in Chrome.

Enter an identity (min 8 chars, e.g. `test-lijinzhao`), click **Connect**, and start speaking.

## How It Works

1. The test page gets a LiveKit access token from the debug server (`POST /token`)
2. The token includes a `RoomConfiguration` that dispatches `pigugu-agent` to your room
3. The browser connects to LiveKit and publishes your microphone audio
4. Conversatstion transcripts (STT) and agent replies appear in the chat UI
5. Agent audio plays back through your speakers

## Files

| File | Purpose |
|------|---------|
| `debug_server.py` | HTTP server (port 9000): serves the test page, generates LiveKit tokens |
| `test_chat.html` | Browser-based chat test page with LiveKit client |
| `agent_test.py` | Python script: validates agent dispatch and audio subscription |
| `livekit_test.py` | Python script: connects to LiveKit, sends/receives audio and data |
| `check_livekit.py` | One-shot check: LiveKit connectivity and agent registration |

## Prerequisites

- `.env` file at project root with valid `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_URL`
- Agent worker running (local via `python main.py start` or deployed to K8s)
- Chrome with microphone access

## Token Flow

```
Browser                       Debug Server (9000)              LiveKit Cloud
  │                                │                                │
  ├── POST /token ────────────────►│                                │
  │   {room, identity}             │                                │
  │                                ├── AccessToken +                │
  │                                │   RoomConfiguration            │
  │◄── {token, url} ───────────────┤   (agent=pigugu-agent)         │
  │                                │                                │
  ├── room.connect(url, token) ────────────────────────────────────►│
  │   (token includes agent dispatch config)                        │
  │                                                                 │
  │                            LiveKit dispatches pigugu-agent      │
  │                            Agent joins room, session starts     │
  │                                                                 │
  │◄── audio + data messages ───────────────────────────────────────┤
```
