# AI Voice Agent - Modular Architecture

A flexible, modular AI voice agent with separated STT/LLM/TTS components.

## 🎯 Features

- ✅ **Modular Design** - Mix and match providers
- ✅ **Multiple Providers** - OpenAI, Qwen, Deepgram, ElevenLabs, and more
- ✅ **Easy Configuration** - Simple environment variables
- ✅ **LiveKit Integration** - Real-time voice streaming
- ✅ **Cost Optimization** - Choose providers based on your budget
- ✅ **Advanced Logging** - Loguru with colors, rotation, and retention

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Voice Conversation                     │
│                                                          │
│  User speaks ──────────────────────> Agent responds     │
│     ↓                                      ↑             │
│     │                                      │             │
│     │                                      │             │
│  ┌──▼───────┐    ┌──────────┐    ┌───────▼──┐         │
│  │   STT    │───>│   LLM    │───>│   TTS    │         │
│  │ (stt.py) │    │ (llm.py) │    │ (tts.py) │         │
│  └──────────┘    └──────────┘    └──────────┘         │
│                                                          │
│  Speech-to-Text  Language Model  Text-to-Speech        │
│                                                          │
└─────────────────────────────────────────────────────────┘

Orchestrated by: main.py
Configured via: config.py
```

## 📁 Project Structure

```
agent/
├── main.py              # Main orchestrator
├── config.py            # Configuration management
├── stt.py              # Speech-to-Text providers
├── llm.py              # LLM providers
├── tts.py              # Text-to-Speech providers
├── requirements.txt    # Dependencies
├── README.md           # This file
├── ARCHITECTURE.md     # Detailed architecture docs
└── EXAMPLES.md         # Configuration examples
```

## 🚀 Quick Start

### 1. Install Dependencies

```bash
cd agent
pip install -r requirements.txt

# Install provider plugins (based on your choice)
pip install livekit-plugins-openai  # For OpenAI
pip install livekit-plugins-deepgram  # For Deepgram
pip install livekit-plugins-elevenlabs  # For ElevenLabs
```

### 2. Configure Environment

Create a `.env` file:

```bash
# Minimal configuration (all OpenAI)
LIVEKIT_URL=ws://livekit-server:8002
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret

STT_PROVIDER=openai
LLM_PROVIDER=openai
TTS_PROVIDER=openai

OPENAI_API_KEY=sk-your-key-here
```

### 3. Run the Agent

```bash
python main.py
```

You should see:

```
🤖 AI Voice Agent - Modular Architecture
======================================================================
STT Provider: openai
LLM Provider: openai
TTS Provider: openai
✅ Voice agent is active and listening...
```

## 🔧 Supported Providers

### STT (Speech-to-Text)
- **OpenAI** - Whisper (multilingual, accurate)
- **Deepgram** - Nova 2 (fastest, production-ready)
- **DashScope** - Paraformer (Chinese support, planned)

### LLM (Language Model)
- **OpenAI** - GPT-4o, GPT-4o-mini (versatile, powerful)
- **Qwen** - Qwen-Plus, Qwen-Turbo (cost-effective, Chinese support)
- **Anthropic** - Claude 3.5 (long context, planned)

### TTS (Text-to-Speech)
- **OpenAI** - TTS-1, TTS-1-HD (high quality, fast)
- **ElevenLabs** - Turbo v2 (most natural voices)
- **Deepgram** - Aura (real-time, affordable)
- **DashScope** - CosyVoice (Chinese voices, planned)

## 📝 Configuration Examples

### Example 1: All OpenAI (Simplest)

```env
STT_PROVIDER=openai
LLM_PROVIDER=openai
TTS_PROVIDER=openai
OPENAI_API_KEY=sk-your-key-here
```

**Cost:** ~$0.10/minute

---

### Example 2: Cost Optimized

```env
STT_PROVIDER=openai
LLM_PROVIDER=qwen
TTS_PROVIDER=openai
OPENAI_API_KEY=sk-your-key-here
DASHSCOPE_API_KEY=sk-your-key-here
```

**Cost:** ~$0.05/minute (50% cheaper!)

---

### Example 3: Premium Quality

```env
STT_PROVIDER=deepgram
LLM_PROVIDER=openai
TTS_PROVIDER=elevenlabs
DEEPGRAM_API_KEY=your-key
OPENAI_API_KEY=your-key
ELEVENLABS_API_KEY=your-key
```

**Cost:** ~$0.15/minute (best quality)

---

See [EXAMPLES.md](./EXAMPLES.md) for more configurations.

## 🎨 Customization

### Change AI Personality

Edit `config.py`:

```python
AI_PERSONALITY = """
You are Buddy, a friendly AI companion.
[Your custom personality here]
"""
```

### Add New Provider

1. Implement provider class in `stt.py`, `llm.py`, or `tts.py`
2. Register in factory function
3. Add config options in `config.py`

See [ARCHITECTURE.md](./ARCHITECTURE.md) for details.

## 📊 Performance

| Config | Latency | Cost/min | Quality |
|--------|---------|----------|---------|
| OpenAI All | 1-2s | $0.10 | ⭐⭐⭐⭐ |
| Qwen + OpenAI | 1.5-2.5s | $0.05 | ⭐⭐⭐⭐ |
| Premium | 0.8-1.5s | $0.15 | ⭐⭐⭐⭐⭐ |

## 🔍 Debugging

### View Logs

```bash
# Console logs (colorized)
python main.py

# Docker logs
docker-compose logs agent -f

# File logs (with rotation)
tail -f logs/agent_2025-12-20.log

# See LOGGING.md for advanced logging features
```

### Common Issues

**"API key not set"**
- Check your `.env` file has the right key for your provider

**"Provider plugin not found"**
- Install the plugin: `pip install livekit-plugins-openai`

**"Agent not connecting"**
- Make sure LiveKit server is running: `docker-compose up livekit`

## 📚 Documentation

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** - Detailed architecture and design
- **[EXAMPLES.md](./EXAMPLES.md)** - Configuration examples and use cases
- **[LOGGING.md](./LOGGING.md)** - Logging configuration and best practices
- **[requirements.txt](./requirements.txt)** - Python dependencies

## 🤝 Contributing

To add a new provider:

1. Fork the repo
2. Create provider class following existing patterns
3. Add tests
4. Update documentation
5. Submit PR

## 📄 License

MIT License

## 🆘 Support

For issues:
1. Check logs: `docker-compose logs agent`
2. Review config: `cat .env`
3. See documentation: [ARCHITECTURE.md](./ARCHITECTURE.md)
4. Open an issue on GitHub

---

**Made with ❤️ for flexible AI conversations**

