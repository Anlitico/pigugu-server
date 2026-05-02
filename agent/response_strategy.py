# agent/response_strategy.py
"""
Response Strategy Module

Implements intelligent decision-making for when the AI agent should respond
in different conversation contexts (1-on-1 vs group conversations).
"""

import time
from typing import Optional, List
from loguru import logger
from speaker_tracker import SpeakerTracker


class ResponseStrategy:
    """
    Determines when the agent should respond based on conversation context.
    
    Supports different strategies for:
    - 1-on-1 conversations: Respond after each user turn (current behavior)
    - Group conversations: Respond intelligently based on context
    """
    
    def __init__(
        self,
        enabled: bool = False,
        group_silence_threshold: float = 3.0,
        direct_address_keywords: Optional[List[str]] = None,
    ):
        """
        Initialize response strategy
        
        Args:
            enabled: Enable smart response logic (default: False, maintains current behavior)
            group_silence_threshold: Seconds of silence before responding in group (default: 3.0)
            direct_address_keywords: Keywords indicating direct address (default: Trump-related)
        """
        self.enabled = enabled
        self.group_silence_threshold = group_silence_threshold
        self.direct_address_keywords = direct_address_keywords or [
            "trump", "president", "donald", "you", "what do you think",
            "your opinion", "your thoughts", "what about you"
        ]
        
        # Track timing for silence detection
        self.last_utterance_time: Optional[float] = None
        
        logger.info(f"✅ ResponseStrategy initialized (enabled={enabled})")
        if enabled:
            logger.info(f"   Group silence threshold: {group_silence_threshold}s")
            logger.info(f"   Direct address keywords: {len(self.direct_address_keywords)}")
    
    def should_respond(
        self,
        speaker_tracker: SpeakerTracker,
        last_utterance: str,
        current_time: Optional[float] = None
    ) -> bool:
        """
        Determine if the agent should respond to the current situation
        
        Args:
            speaker_tracker: SpeakerTracker instance with conversation state
            last_utterance: The most recent utterance text
            current_time: Current timestamp (default: now)
        
        Returns:
            True if agent should respond, False otherwise
        """
        if current_time is None:
            current_time = time.time()
        
        # If smart response is disabled, always respond (maintain current behavior)
        if not self.enabled:
            logger.debug("🤔 [RESPONSE] Smart response disabled - always respond")
            return True
        
        # Determine conversation mode
        is_group = speaker_tracker.is_group_conversation(current_time)
        
        if is_group:
            return self._should_respond_group(
                speaker_tracker, last_utterance, current_time
            )
        else:
            return self._should_respond_1on1(
                speaker_tracker, last_utterance, current_time
            )
    
    def _should_respond_1on1(
        self,
        speaker_tracker: SpeakerTracker,
        last_utterance: str,
        current_time: float
    ) -> bool:
        """
        Response logic for 1-on-1 conversations
        
        In 1-on-1 mode: Always respond after user finishes speaking
        (maintains current behavior)
        
        Args:
            speaker_tracker: SpeakerTracker instance
            last_utterance: Most recent utterance
            current_time: Current timestamp
        
        Returns:
            True (always respond in 1-on-1)
        """
        logger.info("🤔 [RESPONSE] Conversation type: 1-ON-1")
        logger.info("🤔 [RESPONSE] Decision: RESPOND (1-on-1 mode - always respond)")
        return True
    
    def _should_respond_group(
        self,
        speaker_tracker: SpeakerTracker,
        last_utterance: str,
        current_time: float
    ) -> bool:
        """
        Response logic for group conversations
        
        Group response criteria:
        1. Directly addressed (keywords detected)
        2. Natural pause after statement (silence threshold)
        3. NOT during active rapid discussion
        
        Args:
            speaker_tracker: SpeakerTracker instance
            last_utterance: Most recent utterance
            current_time: Current timestamp
        
        Returns:
            True if agent should respond, False otherwise
        """
        logger.info("🤔 [RESPONSE] Conversation type: GROUP")
        
        # Update last utterance time
        self.last_utterance_time = current_time
        
        # Check 1: Direct address detection
        if self._is_directly_addressed(last_utterance):
            logger.info("🤔 [RESPONSE] Decision: RESPOND (direct address detected)")
            return True
        
        # Check 2: Silence threshold
        # Note: This check will be evaluated on subsequent calls
        # For now, we'll use a simple heuristic based on conversation history
        recent_turns = speaker_tracker.get_recent_turns(count=5)
        
        if len(recent_turns) >= 3:
            # Check if there's been rapid back-and-forth (< 1s between turns)
            time_gaps = []
            for i in range(1, len(recent_turns)):
                gap = recent_turns[i].timestamp - recent_turns[i-1].timestamp
                time_gaps.append(gap)
            
            if time_gaps:
                avg_gap = sum(time_gaps) / len(time_gaps)
                if avg_gap < 1.0:
                    logger.info(
                        f"🤔 [RESPONSE] Decision: SKIP "
                        f"(rapid discussion detected, avg gap: {avg_gap:.2f}s)"
                    )
                    return False
        
        # Check 3: Has enough time passed since last utterance?
        # This is a simplified check - in production, we'd wait for actual silence
        # For now, we'll respond if not in rapid discussion mode
        logger.info(
            f"🤔 [RESPONSE] Decision: RESPOND "
            f"(natural conversation flow, threshold: {self.group_silence_threshold}s)"
        )
        return True
    
    def _is_directly_addressed(self, utterance: str) -> bool:
        """
        Check if the utterance directly addresses the agent
        
        Args:
            utterance: The utterance text
        
        Returns:
            True if directly addressed, False otherwise
        """
        utterance_lower = utterance.lower()
        
        for keyword in self.direct_address_keywords:
            if keyword in utterance_lower:
                logger.debug(f"🎯 [RESPONSE] Direct address keyword found: '{keyword}'")
                return True
        
        return False
    
    def update_last_utterance_time(self, timestamp: Optional[float] = None) -> None:
        """
        Update the timestamp of the last utterance
        
        Args:
            timestamp: Utterance timestamp (default: current time)
        """
        if timestamp is None:
            timestamp = time.time()
        
        self.last_utterance_time = timestamp
    
    def get_time_since_last_utterance(self, current_time: Optional[float] = None) -> Optional[float]:
        """
        Get time elapsed since last utterance
        
        Args:
            current_time: Reference time (default: current time)
        
        Returns:
            Seconds since last utterance, or None if no utterances yet
        """
        if self.last_utterance_time is None:
            return None
        
        if current_time is None:
            current_time = time.time()
        
        return current_time - self.last_utterance_time
