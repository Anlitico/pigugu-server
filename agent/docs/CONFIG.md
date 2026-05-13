# Configuration Guide

## Overview

The agent uses a `.config` file (TOML format) to manage configuration across different environments. This approach provides:

- **Environment-based configuration**: DEV and PRODUCTION-SILICON
- **Clear separation**: Development vs Production settings
- **API key management**: Centralized credential storage
- **Easy deployment**: Switch environments with a single variable

## Quick Start

### 1. Create Configuration File

```bash
# Copy the example file
cp .config.example .config

# Edit .config and add your API keys
# Required keys:
#   - CARTESIA_API_KEY
#   - DASHSCOPE_API_KEY
```

### 2. Select Environment

**Development (default):**
```bash
# No ENV variable needed - defaults to DEV
python main.py dev
```

**Production:**
```bash
# Set ENV environment variable
export ENV=PRODUCTION-SILICON
python main.py dev

# Or inline:
ENV=PRODUCTION-SILICON python main.py dev
```

## Configuration File Structure

The `.config` file uses TOML format with two main sections:

```toml
[DEV]
# Development settings
LIVEKIT_URL = "ws://localhost:8002"
QWEN_MODEL = "qwen-plus"
LOG_LEVEL = "DEBUG"
AGENT_WORKERS = 1
...

[PRODUCTION-SILICON]
# Production settings
LIVEKIT_URL = "wss://your-production-server.com"
QWEN_MODEL = "qwen-max"
LOG_LEVEL = "INFO"
AGENT_WORKERS = 4
...
```

## Configuration Priority

Settings are loaded with the following priority (highest first):

1. **Environment Variables** - Override everything
2. **.config File** - Environment-specific values
3. **Default Values** - Fallback hardcoded defaults

### Example: Override via Environment Variable

```bash
# Override QWEN_MODEL for this run only
export QWEN_MODEL=qwen-turbo
python main.py dev
```

## Key Differences: DEV vs PRODUCTION-SILICON

| Setting | DEV | PRODUCTION-SILICON |
|---------|-----|-------------------|
| **LiveKit URL** | `ws://localhost:8002` | `wss://production-server.com` |
| **Qwen Model** | `qwen-plus` | `qwen-max` |
| **TTS Model** | `sonic-2` | `sonic-3` |
| **Workers** | 1 | 4 |
| **Log Level** | DEBUG | INFO |
| **Log Rotation** | Daily (`00:00`) | Size-based (`500 MB`) |
| **Log Retention** | 7 days | 30 days |
| **Log Path** | `logs/agent_{date}.log` | `/var/log/agent/agent_{date}.log` |

## Required API Keys

### Cartesia API Key
- **Purpose**: STT (Speech-to-Text) and TTS (Text-to-Speech)
- **Get it**: https://cartesia.ai
- **Config**: `CARTESIA_API_KEY`

### DashScope API Key
- **Purpose**: Qwen LLM (Language Model)
- **Get it**: https://dashscope.aliyun.com
- **Config**: `DASHSCOPE_API_KEY`

## Configuration Sections

### 1. LiveKit Configuration
```toml
LIVEKIT_URL = "ws://localhost:8002"
LIVEKIT_API_KEY = "devkey"
LIVEKIT_API_SECRET = "secret"
```

### 2. Speech-to-Text (Cartesia)
```toml
CARTESIA_STT_MODEL = "ink-whisper"
CARTESIA_STT_LANGUAGE = "en"
CARTESIA_STT_ENCODING = "pcm_s16le"
CARTESIA_STT_SAMPLE_RATE = 16000
```

### 3. Text-to-Speech (Cartesia)
```toml
CARTESIA_TTS_MODEL = "sonic-2"
CARTESIA_TTS_VOICE = "a0e99841-438c-4a64-b679-ae501e7d6091"
CARTESIA_TTS_LANGUAGE = "en"
CARTESIA_TTS_SAMPLE_RATE = 24000
CARTESIA_TTS_SPEED = 1.0  # Optional: 0.6-2.0
```

### 4. Language Model (Qwen)
```toml
QWEN_MODEL = "qwen-plus"  # or qwen-turbo, qwen-max, qwen-long
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LLM_TEMPERATURE = 0.6
LLM_MAX_TOKENS = ""  # Optional
```

### 5. Agent Settings
```toml
AGENT_WORKERS = 1
AGENT_LOG_CONVERSATIONS = true
ENABLE_INTERRUPTIONS = true
SILENCE_THRESHOLD = 30.0
```

### 6. Logging Configuration
```toml
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_TO_FILE = true
LOG_ROTATION = "00:00"  # or "500 MB"
LOG_RETENTION = "7 days"
LOG_FILE_PATH = "logs/agent_{time:YYYY-MM-DD}.log"
```

## Available Qwen Models

| Model | Description | Best For |
|-------|-------------|----------|
| `qwen-turbo` | Fast, efficient | Development, testing |
| `qwen-plus` | Balanced | General production use |
| `qwen-max` | Most capable | High-quality production |
| `qwen-long` | Long context | Document analysis |

## Troubleshooting

### Configuration not loading?
```bash
# Check which environment is active
ENV=PRODUCTION-SILICON python -c "import os; print(os.getenv('ENV', 'DEV'))"
```

### API keys not working?
1. Verify `.config` file exists (not just `.config.example`)
2. Check API keys are set in the correct environment section
3. Ensure no quotes around empty values (use `""` not `''`)

### Wrong environment loading?
```bash
# Explicitly set environment
export ENV=DEV
python main.py dev
```

## Security Best Practices

1. **Never commit `.config`** - It contains API keys (.gitignore handles this)
2. **Use environment variables in CI/CD** - Override sensitive values
3. **Rotate keys regularly** - Update API keys periodically
4. **Separate dev/prod keys** - Use different API keys for each environment

## Example: Deployment Workflow

### Development
```bash
# 1. Copy example config
cp .config.example .config

# 2. Add dev API keys to [DEV] section
vim .config

# 3. Run in dev mode (default)
python main.py dev
```

### Production
```bash
# 1. Update [PRODUCTION-SILICON] section with production keys
vim .config

# 2. Set production environment
export ENV=PRODUCTION-SILICON

# 3. Run agent
python main.py start
```

## Advanced: Dynamic Configuration

You can override any setting at runtime:

```bash
# Override multiple settings
export QWEN_MODEL=qwen-turbo
export LOG_LEVEL=DEBUG
export AGENT_WORKERS=2
python main.py dev
```

This is useful for:
- A/B testing models
- Temporary debugging
- CI/CD pipeline customization
- Resource-constrained environments

## Support

For configuration issues:
1. Check `CONFIG.md` (this file)
2. Review `.config.example` for correct format
3. Verify API keys are valid
4. Check logs for detailed error messages

