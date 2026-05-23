# pigagent/speaker_tracker.py
"""
Speaker Tracking Module

Tracks speakers in a conversation, detects conversation modes (1-on-1 vs group),
and maintains conversation history with speaker attribution.
"""

import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from loguru import logger


@dataclass
class SpeakerState:
    """Tracks state for a single speaker"""
    speaker_id: int
    last_spoke_time: float
    word_count: int = 0
    turn_count: int = 0
    first_spoke_time: Optional[float] = None
    
    def __post_init__(self):
        if self.first_spoke_time is None:
            self.first_spoke_time = self.last_spoke_time


@dataclass
class ConversationTurn:
    """Represents a single turn in the conversation"""
    speaker_id: Optional[int]  # None for agent
    text: str
    timestamp: float
    is_agent: bool


class SpeakerTracker:
    """
    Tracks speakers in a conversation and analyzes conversation dynamics.
    
    Capabilities:
    - Track multiple speakers and their activity
    - Detect conversation mode (1-on-1 vs group)
    - Maintain conversation history with speaker attribution
    - Analyze conversation patterns
    """
    
    def __init__(self, active_window_seconds: float = 60.0):
        """
        Initialize speaker tracker
        
        Args:
            active_window_seconds: Time window to consider speakers "active" (default: 60s)
        """
        self.speakers: Dict[int, SpeakerState] = {}
        self.conversation_history: List[ConversationTurn] = []
        self.active_window_seconds = active_window_seconds
        self.last_speaker_id: Optional[int] = None
        
        logger.info(f"✅ SpeakerTracker initialized (active_window={active_window_seconds}s)")
    
    def track_utterance(self, speaker_id: int, text: str, timestamp: Optional[float] = None) -> None:
        """
        Track a new utterance from a speaker
        
        Args:
            speaker_id: Numeric ID of the speaker
            text: The utterance text
            timestamp: Timestamp of utterance (default: current time)
        """
        if timestamp is None:
            timestamp = time.time()
        
        # Update or create speaker state
        if speaker_id not in self.speakers:
            self.speakers[speaker_id] = SpeakerState(
                speaker_id=speaker_id,
                last_spoke_time=timestamp,
                word_count=0,
                turn_count=0
            )
            logger.info(f"📊 [SPEAKER] New speaker detected: speaker_{speaker_id}")
        
        speaker = self.speakers[speaker_id]
        
        # Check if this is a new turn (different speaker or gap in speech)
        if self.last_speaker_id != speaker_id:
            speaker.turn_count += 1
            if self.last_speaker_id is not None:
                logger.info(f"📊 [SPEAKER] Speaker transition: speaker_{self.last_speaker_id} → speaker_{speaker_id}")
        
        # Update speaker stats
        speaker.last_spoke_time = timestamp
        speaker.word_count += len(text.split())
        self.last_speaker_id = speaker_id
        
        # Add to conversation history
        self.conversation_history.append(ConversationTurn(
            speaker_id=speaker_id,
            text=text,
            timestamp=timestamp,
            is_agent=False
        ))
    
    def track_agent_response(self, text: str, timestamp: Optional[float] = None) -> None:
        """
        Track an agent response
        
        Args:
            text: The agent's response text
            timestamp: Timestamp of response (default: current time)
        """
        if timestamp is None:
            timestamp = time.time()
        
        self.conversation_history.append(ConversationTurn(
            speaker_id=None,
            text=text,
            timestamp=timestamp,
            is_agent=True
        ))
        
        # Reset last_speaker_id when agent speaks
        self.last_speaker_id = None
    
    def get_active_speakers(self, current_time: Optional[float] = None) -> List[int]:
        """
        Get list of speakers active within the active window
        
        Args:
            current_time: Reference time (default: current time)
        
        Returns:
            List of active speaker IDs
        """
        if current_time is None:
            current_time = time.time()
        
        active_speakers = [
            speaker_id
            for speaker_id, speaker in self.speakers.items()
            if (current_time - speaker.last_spoke_time) <= self.active_window_seconds
        ]
        
        return sorted(active_speakers)
    
    def get_speaker_count(self) -> int:
        """
        Get total number of unique speakers detected
        
        Returns:
            Number of unique speakers
        """
        return len(self.speakers)
    
    def is_group_conversation(self, current_time: Optional[float] = None) -> bool:
        """
        Determine if this is a group conversation
        
        Heuristic: 3+ active speakers OR 2 active speakers with multiple turns each
        
        Args:
            current_time: Reference time (default: current time)
        
        Returns:
            True if group conversation, False if 1-on-1
        """
        active_speakers = self.get_active_speakers(current_time)
        active_count = len(active_speakers)
        
        if active_count >= 3:
            return True
        
        if active_count == 2:
            # Check if both speakers have had multiple turns (indicates back-and-forth)
            turns = [self.speakers[sid].turn_count for sid in active_speakers]
            if all(t >= 2 for t in turns):
                return True
        
        return False
    
    def get_last_speaker(self) -> Optional[int]:
        """
        Get the ID of the last speaker (excluding agent)
        
        Returns:
            Speaker ID or None if no speakers yet
        """
        return self.last_speaker_id
    
    def get_recent_turns(self, count: int = 10) -> List[ConversationTurn]:
        """
        Get the most recent conversation turns
        
        Args:
            count: Number of recent turns to return (default: 10)
        
        Returns:
            List of recent ConversationTurn objects
        """
        return self.conversation_history[-count:] if self.conversation_history else []
    
    def get_conversation_summary(self, max_turns: int = 10) -> str:
        """
        Get a formatted summary of recent conversation
        
        Args:
            max_turns: Maximum number of turns to include (default: 10)
        
        Returns:
            Formatted string with conversation summary
        """
        recent_turns = self.get_recent_turns(max_turns)
        
        if not recent_turns:
            return "No conversation history yet"
        
        summary_lines = ["📝 [CONTEXT] Recent conversation:"]
        for turn in recent_turns:
            if turn.is_agent:
                label = "agent"
            else:
                label = f"speaker_{turn.speaker_id}"
            
            # Truncate long messages
            text = turn.text if len(turn.text) <= 80 else turn.text[:77] + "..."
            summary_lines.append(f"  {label}: \"{text}\"")
        
        return "\n".join(summary_lines)
    
    def get_conversation_mode_summary(self, current_time: Optional[float] = None) -> str:
        """
        Get a summary of the current conversation mode
        
        Args:
            current_time: Reference time (default: current time)
        
        Returns:
            Formatted string describing conversation mode
        """
        is_group = self.is_group_conversation(current_time)
        active_speakers = self.get_active_speakers(current_time)
        total_speakers = self.get_speaker_count()
        
        mode = "GROUP" if is_group else "1-ON-1"
        return (
            f"📊 [SPEAKER] Conversation mode: {mode} "
            f"({len(active_speakers)} active, {total_speakers} total speakers)"
        )
    
    def reset(self) -> None:
        """Reset all tracking state"""
        self.speakers.clear()
        self.conversation_history.clear()
        self.last_speaker_id = None
        logger.info("🔄 [SPEAKER] Tracker reset")
