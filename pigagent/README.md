# PigAgent — Voice Agent

LiveKit STT → LLM → TTS conversation pipeline with context management, compression, and PG persistence.

## Quick Start (Local)

### 1. Infrastructure

```bash
# From repo root:
# Start Postgres + Redis
docker compose up -d postgres redis

# Run database migrations (uses pigagent's venv, runs from repo root)
uv run --directory pigagent alembic upgrade head
```

### 2. Start Agent

```bash
cd pigagent

# Install dependencies (first time)
uv sync

# Start agent (includes API server on :8080)
uv run python main.py
```

### 3. Verify

```bash
# API health check
curl http://localhost:8080/health

# Run tests
uv run pytest tests/ -k "not integration" -v
```

### Docker (Full Stack)

```bash
docker compose up -d
```

## Environment

Copy `.env.example` to `.env` and fill in API keys. See [.env.example](../.env.example) for required variables.

Key variables for local dev:

| Variable | Default |
|----------|---------|
| `REDIS_URL` | `redis://localhost:6379/0` |
| `DATABASE_URL` | `postgresql://pigugu:pigugu@localhost:5432/pigugu` |
| `QWEN_MODEL` | `qwen3.6-plus` |
| `API_PORT` | `8080` |

## Architecture

```
Browser ──WebRTC──> LiveKit ──STT──> Agent ──LLM──> TTS ──> LiveKit ──> Browser
                       │                  │
                       │            ┌──────┴──────┐
                       │            │  Redis (hot) │
                       │            │  PG (durable)│
                       │            └──────────────┘
                       │
                  Deepgram/Cartesia
```

### Context Pipeline

```
load ──> assemble ──> to_messages ──> LLM
  │           │
  │    ┌──────┴──────┐
  │    │ L2 profile   │ ← UserMemory
  │    │ L3 summary   │ ← Session compression
  │    │ L4 roast     │ ← Game context
  │    │ raw turns    │ ← Uncompressed turns (> anchor)
  │    └─────────────┘
  │
  └── Redis GET + LRANGE (fast path)
      PG fallback (recovery path)
```

Compression triggers when uncompressed turns > 100 OR total tokens > 200K.

## Project Layout

```
pigagent/
├── agent.py              # PigAgent — generate_reply entry point
├── main.py               # CLI entry point
├── config.py             # All configuration
├── core/
│   ├── agent/            # ReAct agent loop (AgentRunner)
│   ├── llm/              # LLM providers (Qwen, Volcengine)
│   └── audio/            # STT / TTS adapters
├── context/              # Context management
│   ├── manager.py        # ContextManager orchestrator
│   ├── schema.py         # Data structures
│   ├── snapshot.py       # ContextSnapshot + compression triggers
│   ├── storage/          # Redis + PG I/O
│   └── compression/      # L2/L3/L4 compression pipeline
├── lk/                   # LiveKit integration
├── roast/                # Roast game mode
└── tests/                # Unit tests
```
