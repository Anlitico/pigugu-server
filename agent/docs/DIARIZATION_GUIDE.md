# Diarization & Multi-Speaker Support Guide

## Overview

The Trump AI agent now supports speaker diarization and intelligent multi-speaker conversation handling. This allows the agent to:

1. **Identify different speakers** in a conversation
2. **Track conversation dynamics** (1-on-1 vs group conversations)
3. **Make intelligent response decisions** based on conversation context
4. **Analyze conversation patterns** (debates, discussions, interviews)

## Quick Start

### Enable Diarization (Phase 1)

Edit `agent/.config` and set:

```toml
DEEPGRAM_ENABLE_DIARIZATION = true
```

Restart the agent. Speaker information will now appear in logs:

```
👤 [STT] User transcribed: "Hello there" (speaker: 0)
👤 [STT] User transcribed: "How are you?" (speaker: 1)
📊 [SPEAKER] New speaker detected: speaker_1
📊 [SPEAKER] Speaker transition: speaker_0 → speaker_1
```

### Enable Smart Response (Phase 4 - Optional)

For intelligent group conversation handling:

```toml
ENABLE_SMART_RESPONSE = true
GROUP_RESPONSE_SILENCE_THRESHOLD = 3.0
DIRECT_ADDRESS_KEYWORDS = "Trump,president,Donald,you,what do you think"
```

When enabled:
- **1-on-1 conversations**: Agent responds normally (no change in behavior)
- **Group conversations**: Agent waits for appropriate moments to respond

## Architecture

### Modules

1. **`speaker_tracker.py`** (Phase 2-3)
   - Tracks individual speakers and their activity
   - Maintains conversation history with speaker attribution
   - Detects conversation mode (1-on-1 vs group)

2. **`response_strategy.py`** (Phase 4)
   - Determines when agent should respond
   - Implements different strategies for 1-on-1 and group conversations
   - Detects direct address through keywords

3. **`conversation_analyzer.py`** (Phase 5)
   - Advanced conversation dynamics analysis
   - Turn-taking pattern detection
   - Engagement scoring system
   - Conversation mode detection (debate, discussion, interview, monologue)

### Integration

All modules integrate with `main.py`:

```python
# Initialize at session start
speaker_tracker = SpeakerTracker(active_window_seconds=60.0)
response_strategy = ResponseStrategy(enabled=config.ENABLE_SMART_RESPONSE, ...)

# Track utterances in event handlers
@session.on("user_input_transcribed")
def on_user_input_transcribed(event):
    # Extract speaker ID from diarization
    speaker_id = event.alternatives[0].words[0].speaker
    
    # Track in speaker tracker
    speaker_tracker.track_utterance(speaker_id, text)
    
    # Log conversation mode
    logger.info(speaker_tracker.get_conversation_mode_summary())
```

## Configuration Reference

### Phase 1: Diarization

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DEEPGRAM_ENABLE_DIARIZATION` | `false` | Enable speaker diarization |

### Phase 4: Smart Response

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENABLE_SMART_RESPONSE` | `false` | Enable intelligent response logic |
| `GROUP_RESPONSE_SILENCE_THRESHOLD` | `3.0` | Seconds of silence before responding in groups |
| `DIRECT_ADDRESS_KEYWORDS` | `"Trump,president,..."` | Keywords indicating direct address |

### Phase 5: Advanced Analysis

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ENGAGEMENT_THRESHOLD` | `0.7` | Score threshold to respond (0-1) |
| `DEBATE_MODE_SILENCE_MULTIPLIER` | `1.5` | Wait longer in debates (multiplier) |

## Usage Examples

### Example 1: Testing Diarization (Basic)

1. Enable diarization in `.config`:
   ```toml
   DEEPGRAM_ENABLE_DIARIZATION = true
   ```

2. Run the agent:
   ```bash
   cd agent
   python main.py
   ```

3. Connect with 2+ browser tabs (different speakers)

4. Check logs for speaker identification:
   ```
   📊 [SPEAKER] New speaker detected: speaker_0
   📊 [SPEAKER] New speaker detected: speaker_1
   📊 [SPEAKER] Conversation mode: 1-ON-1 (2 active, 2 total speakers)
   ```

### Example 2: Group Conversation Mode

1. Enable smart response:
   ```toml
   ENABLE_SMART_RESPONSE = true
   ```

2. Connect with 3+ participants

3. Have a group discussion without addressing the agent

4. Observe in logs:
   ```
   🤔 [RESPONSE] Conversation type: GROUP
   🤔 [RESPONSE] Decision: SKIP (speakers actively discussing)
   ```

5. Directly address the agent: "Trump, what do you think?"

6. Observe response:
   ```
   🤔 [RESPONSE] Decision: RESPOND (direct address detected)
   ```

### Example 3: Advanced Analysis

The `conversation_analyzer.py` provides detailed metrics:

```
📊 [ANALYSIS] Conversation mode: DEBATE
📊 [ANALYSIS] Turn frequency: 0.5s avg gap (high intensity)
📊 [ANALYSIS] Topic relevance: 0.85
🤔 [ENGAGEMENT] Score: 0.45 (below threshold 0.7)
🤔 [RESPONSE] Decision: SKIP (active debate detected)
```

## Conversation Modes

The system detects different conversation patterns:

### 1. **1-on-1 (Default)**
- Single user speaking to agent
- Agent responds after each user turn
- **Behavior**: Same as before (always responds)

### 2. **Group Discussion**
- Multiple speakers, moderate pace
- Agent waits for appropriate moment
- **Behavior**: Responds when directly addressed OR after silence threshold

### 3. **Debate**
- Fast back-and-forth between 2 speakers
- High speaker alternation rate
- **Behavior**: Waits longer for natural break (1.5x silence threshold)

### 4. **Interview**
- One speaker dominates (70%+ of words)
- Others ask short questions
- **Behavior**: Standard group response logic

### 5. **Monologue**
- Single speaker extended turn
- **Behavior**: Waits for speaker to finish

## Testing Scenarios

### Scenario 1: Single User

```
User: "Hello"
Agent: "Hello! I'm Trump..."  ← Responds immediately
```

**Mode**: 1-on-1
**Smart Response**: Disabled or responds normally

### Scenario 2: Two Users Chatting

```
User A: "Did you see the game?"
User B: "Yeah, it was great!"
User A: "The best plays..."
```

**Mode**: GROUP (rapid discussion)
**Smart Response**: Agent waits (doesn't interrupt)

### Scenario 3: Direct Address in Group

```
User A: "What about the economy?"
User B: "I think it's doing okay"
User A: "Trump, what's your opinion?"  ← Direct address
Agent: "Let me tell you about the economy..."  ← Responds
```

**Mode**: GROUP
**Smart Response**: Responds due to direct address keyword

### Scenario 4: Natural Pause

```
User A: "I wonder what he thinks"
[3 seconds of silence]
Agent: "You know what I think? Let me tell you..."  ← Responds
```

**Mode**: GROUP
**Smart Response**: Responds after silence threshold

## Troubleshooting

### Diarization Not Working

**Problem**: No speaker IDs in logs

**Solutions**:
1. Verify `DEEPGRAM_ENABLE_DIARIZATION = true` in `.config`
2. Check you're using Deepgram STT (not Cartesia)
3. Ensure multiple audio sources (different browser tabs/devices)
4. Check Deepgram API supports diarization for your account

### Speaker IDs Inconsistent

**Problem**: Same speaker gets different IDs

**Solutions**:
- This is expected behavior - Deepgram assigns IDs per session
- Speaker IDs are relative (speaker_0, speaker_1, etc.)
- Use conversation patterns, not absolute IDs

### Agent Not Responding in Group

**Problem**: Agent never responds even when directly addressed

**Solutions**:
1. Check `ENABLE_SMART_RESPONSE = true`
2. Verify direct address keywords match your speech
3. Lower `ENGAGEMENT_THRESHOLD` (try 0.5)
4. Check logs for engagement score details

### Agent Responds Too Often in Group

**Problem**: Agent interrupts group discussions

**Solutions**:
1. Increase `GROUP_RESPONSE_SILENCE_THRESHOLD` (try 5.0)
2. Increase `ENGAGEMENT_THRESHOLD` (try 0.8)
3. Check if conversation is being detected as 1-on-1 (logs)

## Performance Considerations

### Latency

- **Diarization adds ~50-100ms** to STT latency (Deepgram processing)
- Minimal impact on user experience
- Worth the cost for multi-speaker scenarios

### Cost

- **Diarization** may incur additional Deepgram API costs
- Check your Deepgram plan for pricing
- Consider enabling only when needed

### Resource Usage

- Speaker tracking uses minimal memory (< 1MB per session)
- Conversation history limited to last 100 turns
- No significant CPU impact

## Future Enhancements

Potential improvements (not yet implemented):

1. **Voice-based speaker recognition**
   - Identify speakers by voice characteristics
   - Persistent speaker IDs across sessions

2. **Sentiment analysis**
   - Detect conversation emotion/tone
   - Adjust response strategy accordingly

3. **Dynamic interruption handling**
   - Real-time adjustment of silence thresholds
   - Learn from user feedback

4. **Multi-modal context**
   - Integrate video cues (if available)
   - Body language and visual context

## API Reference

### SpeakerTracker

```python
tracker = SpeakerTracker(active_window_seconds=60.0)

# Track utterances
tracker.track_utterance(speaker_id=0, text="Hello")
tracker.track_agent_response(text="Hi there!")

# Query state
speaker_count = tracker.get_speaker_count()
is_group = tracker.is_group_conversation()
last_speaker = tracker.get_last_speaker()

# Get history
recent_turns = tracker.get_recent_turns(count=10)
summary = tracker.get_conversation_summary()
```

### ResponseStrategy

```python
strategy = ResponseStrategy(
    enabled=True,
    group_silence_threshold=3.0,
    direct_address_keywords=["trump", "president"]
)

# Decide if should respond
should_respond = strategy.should_respond(
    speaker_tracker=tracker,
    last_utterance="What do you think?",
    current_time=time.time()
)
```

### ConversationAnalyzer

```python
analyzer = ConversationAnalyzer(
    engagement_threshold=0.7,
    debate_silence_multiplier=1.5
)

# Analyze conversation
turn_dynamics = analyzer.analyze_turn_dynamics(conversation_history)
conv_mode = analyzer.detect_conversation_mode(speaker_tracker, turn_dynamics)

# Get engagement score
should_respond, score, reason = analyzer.should_respond_advanced(
    speaker_tracker, last_utterance, direct_address, current_time
)
```

## Support

For issues or questions:

1. Check logs for detailed debugging information
2. Review configuration parameters
3. Test with diarization disabled to isolate issues
4. Refer to plan document for implementation details

---

**Last Updated**: 2026-01-26
**Version**: 1.0.0
**Author**: AI Assistant
