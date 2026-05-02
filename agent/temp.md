TIME        │ COMPONENT        │ EVENT
════════════╪══════════════════╪═══════════════════════════════════════════════════════
            │                  │
20:28:25.72 │ STT (Deepgram)   │ Final transcript arrives:
            │                  │   "Hi, I my name is Thomas, and I would like you
            │                  │    to speak to me only English..." (speaker: S0)
            │                  │
20:28:25.72 │ TTS Pipeline     │ speech_created (source: generate_reply)
            │                  │   └─ Preemptive generation kicks in immediately
            │                  │      (STT final arrived before VAD end-of-speech)
            │                  │
20:28:26.51 │ MODE 3 GATING    │ on_user_turn_completed fires
            │                  │   └─ "Direct address detected" (keyword "you" matched)
            │                  │   └─ Does NOT raise StopResponse → allows reply
            │                  │
20:28:26.53 │ LLM (Qwen)       │ User message added to context:
            │                  │   "[Speaker S0]: Hi, I my name is Thomas..."
            │                  │
20:28:26.53 │ AGENT STATE       │ thinking (LLM request begins)
            │                  │
            │    ┌─────────────────────────────────────────────┐
            │    │  LLM is streaming tokens...                 │
            │    │  "Thomas — great name, tremendous name,     │
            │    │   believe me! English only? Absolutely,     │
            │    │   we do English better than anyone..."      │
            │    │   (generating full response)                │
            │    └─────────────────────────────────────────────┘
            │                  │
20:28:27.81 │ LLM METRICS      │ TTFT: 0.822s, 20 tokens generated, 15.4 tok/s
            │                  │
20:28:28.25 │ TTS (Cartesia)   │ Agent started speaking ◀━━━━━━━━━━━━━━━━━━━━━━━━━━
            │                  │   └─ TTS audio begins playing to room            ┃
            │                  │   └─ Agent says: "Thomas—great..."               ┃
            │                  │                                                   ┃
            │                  │              ⚡ 500ms of audio plays ⚡            ┃
            │                  │                                                   ┃
20:28:28.75 │ STT (Deepgram)   │ Interim transcript: "Okay, Thomas."              ┃
            │                  │   └─ This is HUMAN S1 speaking!                   ┃
            │                  │                                                   ┃
            │    ┌─────────────────────────────────────────────┐                   ┃
            │    │         SILERO VAD DETECTS VOICE            │                   ┃
            │    │                                             │                   ┃
            │    │  Raw audio chunk → VAD model → score: 0.93  │                   ┃
            │    │                                             │                   ┃
            │    │  VAD says: "A human is speaking!"           │                   ┃
            │    │                                             │                   ┃
            │    │  AgentSession checks:                       │                   ┃
            │    │    allow_interruptions? → TRUE (from config)│                   ┃
            │    │    agent currently speaking? → YES           │                   ┃
            │    │                                             │                   ┃
            │    │  ══> INTERRUPT! Cancel TTS playback ◀━━━━━━━━━━━━━━━━━━━━━━━━━━┛
            │    └─────────────────────────────────────────────┘
            │                  │
20:28:28.75 │ AGENT STATE       │ Agent stopped speaking (now listening)
            │                  │
            │    ┌─────────────────────────────────────────────┐
            │    │     CONVERSATION HISTORY TRUNCATION          │
            │    │                                             │
            │    │  LLM generated: "Thomas—great name,         │
            │    │    tremendous name, believe me! English     │
            │    │    only? Absolutely, we do English better   │
            │    │    than anyone..."                          │
            │    │                                             │
            │    │  But user only HEARD: "Thomas—great"        │
            │    │    (500ms of audio before interruption)     │
            │    │                                             │
            │    │  LiveKit truncates chat history to:          │
            │    │    assistant: "Thomas—great"                 │
            │    │    (everything after is discarded)           │
            │    └─────────────────────────────────────────────┘
            │                  │
20:28:30.70 │ LLM HISTORY       │ Response logged: "Thomas—great"  ← truncated!
            │                  │
20:28:30.69 │ STT (Deepgram)   │ Final transcript:
            │                  │   "Okay, Thomas. Let's keep the conversation"
            │                  │   (speaker: S1) ← this is the human who interrupted
            │                  │
20:28:30.69 │ SPEAKER TRACKER   │ Speaker transition: S0 → S1
            │                  │
20:28:31.57 │ MODE 3 GATING    │ on_user_turn_completed fires
            │                  │   └─ "Suppressed -- cooldown (0.9s < 15.0s)"
            │                  │   └─ Raises StopResponse → agent stays quiet
            │                  │
20:28:32.93 │ STT (Deepgram)   │ Final transcript:
            │                  │   "in English. What would you like to talk
            │                  │    about now?" (speaker: S1)
            │                  │
20:28:34.96 │ STT (Deepgram)   │ Final transcript:
            │                  │   "So let's say the" (speaker: S0)
            │                  │   ← Thomas tries to speak but gets cut off
            │                  │
            │    ┌─────────────────────────────────────────────┐
            │    │  10s later... group_discussion_checker runs  │
            │    │                                             │
            │    │  LLM decision: QUIET                        │
            │    │  REASON: "Thomas was cut off mid-sentence   │
            │    │  ('So let's say the...'), and S1 is still   │
            │    │  actively engaging—jumping in would          │
            │    │  interrupt the natural flow"                 │
            │    └─────────────────────────────────────────────┘
            │                  │
20:28:48.24 │ MODE 3 CHECKER   │ LLM decides to stay QUIET
            │                  │
════════════╧══════════════════╧═══════════════════════════════════════════════════════