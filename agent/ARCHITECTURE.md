# Agent Architecture - Modular STT/LLM/TTS

## Overview

The agent has been refactored into a **modular architecture** that separates concerns into distinct components:

- **STT (Speech-to-Text)** - `stt.py`
- **LLM (Large Language Model)** - `llm.py`
- **TTS (Text-to-Speech)** - `tts.py`
- **Configuration** - `config.py`
- **Main Entry Point** - `main.py`

This design allows you to:
- ✅ Mix and match providers (e.g., OpenAI TTS + Qwen LLM + Deepgram STT)
- ✅ Easily add new providers
- ✅ Test different configurations
- ✅ Maintain clean, organized code

---

## File Structure

```
agent/
├── main.py              # Main entry point, orchestrates components
├── config.py            # Configuration management
├── stt.py              # Speech-to-Text providers
├── llm.py              # Large Language Model providers
├── tts.py              # Text-to-Speech providers
├── requirements.txt    # Python dependencies
├── .env.example        # Example configuration
├── .env                # Your actual configuration (not in git)
└── ARCHITECTURE.md     # This file
```

---

## Supported Providers

### STT (Speech-to-Text)

| Provider | Model | Plugin Required | Status |
|----------|-------|-----------------|--------|
| **OpenAI** | whisper-1 | `livekit-plugins-openai` | ✅ Ready |
| **Deepgram** | nova-2 | `livekit-plugins-deepgram` | ✅ Ready |
| **DashScope** | paraformer-realtime-v2 | Custom | 🚧 Planned |

### LLM (Large Language Model)

| Provider | Model | Plugin Required | Status |
|----------|-------|-----------------|--------|
| **OpenAI** | gpt-4o, gpt-4o-mini | `livekit-plugins-openai` | ✅ Ready |
| **Qwen** | qwen-plus, qwen-turbo | Custom (dashscope) | ✅ Ready |
| **Anthropic** | claude-3-5-sonnet | Custom | 🚧 Planned |

### TTS (Text-to-Speech)

| Provider | Model | Plugin Required | Status |
|----------|-------|-----------------|--------|
| **OpenAI** | tts-1, tts-1-hd | `livekit-plugins-openai` | ✅ Ready |
| **ElevenLabs** | eleven_turbo_v2 | `livekit-plugins-elevenlabs` | ✅ Ready |
| **Deepgram** | aura-asteria-en | `livekit-plugins-deepgram` | ✅ Ready |
| **DashScope** | cosyvoice-v1 | Custom | 🚧 Planned |

---

## Configuration

### Step 1: Copy Configuration Template

```bash
cd agent/
cp .env.example .env
```

### Step 2: Choose Your Providers

Edit `.env` and set your provider preferences:

```bash
# Example: Use OpenAI for everything
STT_PROVIDER=openai
LLM_PROVIDER=openai
TTS_PROVIDER=openai
OPENAI_API_KEY=sk-your-key-here

# Example: Mix providers
STT_PROVIDER=deepgram
LLM_PROVIDER=qwen
TTS_PROVIDER=elevenlabs
DEEPGRAM_API_KEY=your-key
DASHSCOPE_API_KEY=your-key
ELEVENLABS_API_KEY=your-key
```

### Step 3: Install Required Plugins

Based on your chosen providers, install the necessary packages:

```bash
# For OpenAI providers
pip install livekit-plugins-openai

# For Deepgram providers
pip install livekit-plugins-deepgram

# For ElevenLabs TTS
pip install livekit-plugins-elevenlabs

# For Qwen/DashScope (already in requirements.txt)
pip install dashscope
```

---

## Usage Examples

### Example 1: All OpenAI (Simplest)

**Configuration:**
```env
STT_PROVIDER=openai
LLM_PROVIDER=openai
TTS_PROVIDER=openai
OPENAI_API_KEY=sk-your-key-here
```

**Run:**
```bash
python main.py
```

**Output:**
```
🤖 AI Voice Agent - Modular Architecture
STT Provider: openai
LLM Provider: openai
TTS Provider: openai
✅ Voice agent is active and listening...
```

---

### Example 2: Qwen LLM + OpenAI Voice

**Configuration:**
```env
STT_PROVIDER=openai
LLM_PROVIDER=qwen
TTS_PROVIDER=openai
OPENAI_API_KEY=sk-your-openai-key
DASHSCOPE_API_KEY=sk-your-dashscope-key
QWEN_MODEL=qwen-plus
```

**Benefits:**
- Lower cost (Qwen is cheaper than GPT-4)
- Better Chinese language support (if needed)
- High-quality voice with OpenAI TTS

---

### Example 3: Premium Quality (GPT-4 + ElevenLabs)

**Configuration:**
```env
STT_PROVIDER=deepgram
LLM_PROVIDER=openai
TTS_PROVIDER=elevenlabs
DEEPGRAM_API_KEY=your-key
OPENAI_API_KEY=your-key
ELEVENLABS_API_KEY=your-key
OPENAI_LLM_MODEL=gpt-4o
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
```

**Benefits:**
- Best-in-class STT (Deepgram)
- Most capable LLM (GPT-4)
- Most natural TTS (ElevenLabs)

---

## How It Works

### Component Flow

```
┌─────────────────────────────────────────────────────────┐
│                    LiveKit Room                         │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                   Voice Agent (main.py)                  │
│                                                          │
│  ┌─────────┐      ┌─────────┐      ┌─────────┐        │
│  │   STT   │ ───> │   LLM   │ ───> │   TTS   │        │
│  │ (stt.py)│      │ (llm.py)│      │ (tts.py)│        │
│  └─────────┘      └─────────┘      └─────────┘        │
│       ↑                                  │               │
│       │                                  ↓               │
│   Audio In                          Audio Out            │
└─────────────────────────────────────────────────────────┘
```

### 1. STT (Speech-to-Text)

**Input:** User's audio stream  
**Output:** Text transcription  
**Location:** `stt.py`

```python
from stt import create_stt

# Create STT component
stt = create_stt(provider="openai", model="whisper-1")

# Or use Deepgram
stt = create_stt(provider="deepgram", model="nova-2")
```

### 2. LLM (Large Language Model)

**Input:** User's text message  
**Output:** AI response text  
**Location:** `llm.py`

```python
from llm import create_llm

# Create LLM component
llm = create_llm(
    provider="qwen",
    instructions="You are a helpful assistant",
    model="qwen-plus",
    temperature=0.7
)

# Chat with the LLM
response = await llm.chat("Hello, how are you?")
```

### 3. TTS (Text-to-Speech)

**Input:** AI response text  
**Output:** Audio stream  
**Location:** `tts.py`

```python
from tts import create_tts

# Create TTS component
tts = create_tts(
    provider="openai",
    model="tts-1",
    voice="nova"
)

# Or use ElevenLabs
tts = create_tts(
    provider="elevenlabs",
    voice="21m00Tcm4TlvDq8ikWAM"
)
```

---

## Adding New Providers

To add a new provider, follow this pattern:

### 1. Add Provider Class

In `stt.py`, `llm.py`, or `tts.py`:

```python
class MyNewProvider(ProviderBase):
    """My new provider implementation"""
    
    def __init__(self, model: str = "default-model"):
        self.model = model
        # Initialize your provider
    
    async def process(self, input_data):
        """Process the input"""
        # Your implementation
        pass
```

### 2. Register Provider

Add to the factory function:

```python
def create_stt(provider: str = "openai", **kwargs):
    providers = {
        "openai": OpenAISTT,
        "deepgram": DeepgramSTT,
        "mynew": MyNewProvider,  # Add here
    }
    return providers[provider](**kwargs)
```

### 3. Update Configuration

In `config.py`:

```python
class AgentConfig(BaseSettings):
    MYNEW_API_KEY: Optional[str] = Field(default=None)
    MYNEW_MODEL: str = Field(default="default-model")
```

---

## Testing

### Unit Tests

Test individual components:

```python
# test_llm.py
import asyncio
from llm import create_llm

async def test_qwen():
    llm = create_llm(provider="qwen")
    response = await llm.chat("Hello!")
    print(f"Response: {response}")

asyncio.run(test_qwen())
```

### Integration Tests

Test the full agent:

```bash
# Start LiveKit server first
docker-compose up livekit

# Run agent
python main.py
```

---

## Troubleshooting

### Issue: "Provider plugin not found"

**Solution:** Install the required plugin:
```bash
pip install livekit-plugins-openai
# or
pip install livekit-plugins-deepgram
# or
pip install livekit-plugins-elevenlabs
```

### Issue: "API key not set"

**Solution:** Check your `.env` file:
```bash
cat .env | grep API_KEY
```

Make sure the key for your chosen provider is set.

### Issue: "Custom provider not working with VoiceAgent"

**Explanation:** Custom providers (like Qwen) don't have LiveKit plugins, so they use a text-based interface instead of real-time voice streaming.

**Solutions:**
1. Use providers with LiveKit plugins for voice streaming
2. Implement a custom streaming adapter for your provider

---

## Performance Considerations

### Latency Comparison

| Provider Combo | Avg Latency | Cost/min |
|----------------|-------------|----------|
| OpenAI All | ~1-2s | $0.10 |
| Qwen + OpenAI Voice | ~1.5-2.5s | $0.05 |
| Deepgram + GPT-4 + ElevenLabs | ~0.8-1.5s | $0.15 |

### Optimization Tips

1. **Choose fast models:**
   - STT: Deepgram nova-2 (fastest)
   - LLM: gpt-4o-mini or qwen-turbo (fast + cheap)
   - TTS: elevenlabs turbo (fastest)

2. **Enable streaming:**
   ```python
   agent = VoiceAgent(
       stt=stt,
       llm=llm,
       tts=tts,
       streaming=True  # Enable streaming
   )
   ```

3. **Reduce max_tokens:**
   ```env
   LLM_MAX_TOKENS=150  # Faster responses
   ```

---

## Future Enhancements

### Planned Features

- [ ] Real-time streaming for custom LLM providers
- [ ] Support for more TTS voices
- [ ] Emotion detection and response
- [ ] Multi-language support
- [ ] Custom wake words
- [ ] Voice cloning integration

### Contributing

To contribute a new provider:

1. Implement the provider class in the appropriate file
2. Add configuration options
3. Update this documentation
4. Submit a PR with tests

---

## License

MIT License - see LICENSE file

## Support

For issues or questions:
- Check the logs: `docker-compose logs agent`
- Review configuration: `cat agent/.env`
- Test components individually (see Testing section)

---

**Document Version:** 1.0  
**Last Updated:** December 2025

