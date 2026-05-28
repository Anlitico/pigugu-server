# Pigugu Agent Chat Test Tools

## Prerequisites

### 1. Python Environment

Requires Python 3.11+. Create and activate a virtual environment:

```bash
cd pigagent
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (macOS / Linux)
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install livekit livekit-agents livekit-plugins-deepgram python-dotenv
```

Or from the project's `pyproject.toml`:

```bash
pip install .
```

### 3. Environment Variables

Create a `.env` file at the project root with LiveKit credentials:

```
LIVEKIT_URL=wss://<your-project>.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 4. Agent Worker

The agent worker must be running for the test page to work:

```bash
# Local
cd pigagent
python main.py start

# Or check K8s deployment (production)
kubectl get pods -l app=pigugu-agent
```

### 5. Chrome

Chrome with microphone access enabled (`getUserMedia`).

## Quick Start

```bash
cd pigagent/devtools
python debug_server.py
```

Open http://localhost:9000/chat in Chrome.

Enter an identity (min 8 chars, e.g. `test-XXXX`), click **Connect**, and start speaking.

## How It Works

1. The test page gets a LiveKit access token from the debug server (`POST /token`)
2. The token includes a `RoomConfiguration` that dispatches `pigugu-agent` to your room
3. The browser connects to LiveKit and publishes your microphone audio
4. Conversation transcripts (STT) and agent replies appear in the chat UI
5. Agent audio plays back through your speakers

## Files

| File | Purpose |
|------|---------|
| `debug_server.py` | HTTP server (port 9000): serves the test page, generates LiveKit tokens |
| `test_chat.html` | Browser-based chat test page with LiveKit client |
| `agent_test.py` | Python script: validates agent dispatch and audio subscription |
| `livekit_test.py` | Python script: connects to LiveKit, sends/receives audio and data |
| `check_livekit.py` | One-shot check: LiveKit connectivity and agent registration |

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
