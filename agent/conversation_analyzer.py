# agent/conversation_analyzer.py
"""
Conversation Analyzer Module

Advanced analysis of conversation dynamics including turn-taking patterns,
conversation modes, and engagement scoring for intelligent response decisions.
"""

import time
from enum import Enum
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
from loguru import logger
from speaker_tracker import SpeakerTracker, ConversationTurn


class ConversationMode(Enum):
    """Types of conversation modes detected"""
    DEBATE = "DEBATE"              # Fast back-and-forth between 2 speakers
    DISCUSSION = "DISCUSSION"      # Multiple speakers, moderate pace
    INTERVIEW = "INTERVIEW"        # One speaker dominates, others ask questions
    MONOLOGUE = "MONOLOGUE"        # Single speaker extended turn
    UNKNOWN = "UNKNOWN"            # Not enough data


@dataclass
class TurnDynamics:
    """Metrics about turn-taking dynamics"""
    avg_turn_gap: float           # Average time between turns (seconds)
    turn_frequency: float         # Turns per minute
    speaker_alternation: float    # How often speakers alternate (0-1)
    longest_gap: float            # Longest silence between turns
    shortest_gap: float           # Shortest time between turns


@dataclass
class EngagementFactors:
    """Factors contributing to engagement score"""
    silence_duration: float       # Time since last utterance
    direct_address: bool          # Was agent directly addressed
    topic_relevance: float        # How relevant is topic (0-1)
    turn_count: int              # Number of turns in conversation
    speaker_count: int           # Number of active speakers


class ConversationAnalyzer:
    """
    Analyzes conversation dynamics for intelligent response decisions.
    
    Provides:
    - Turn dynamics analysis (gaps, frequency, patterns)
    - Conversation mode detection (debate, discussion, etc.)
    - Engagement scoring (should agent respond now?)
    """
    
    def __init__(
        self,
        engagement_threshold: float = 0.7,
        debate_silence_multiplier: float = 1.5,
        topic_keywords: Optional[Dict[str, List[str]]] = None
    ):
        """
        Initialize conversation analyzer
        
        Args:
            engagement_threshold: Score threshold to respond (default: 0.7)
            debate_silence_multiplier: Increase silence requirement in debates (default: 1.5)
            topic_keywords: Keywords for topic relevance detection
        """
        self.engagement_threshold = engagement_threshold
        self.debate_silence_multiplier = debate_silence_multiplier
        
        # Default topic keywords for Trump AI (politics, economy, etc.)
        self.topic_keywords = topic_keywords or {
            "politics": ["election", "vote", "congress", "senate", "law", "policy"],
            "economy": ["economy", "jobs", "trade", "business", "tax", "money"],
            "immigration": ["border", "immigration", "wall", "visa", "illegal"],
            "foreign_policy": ["china", "russia", "nato", "trade deal", "international"],
            "healthcare": ["healthcare", "obamacare", "insurance", "medical"],
        }
        
        logger.info(f"✅ ConversationAnalyzer initialized (threshold={engagement_threshold})")
    
    def analyze_turn_dynamics(
        self,
        conversation_history: List[ConversationTurn],
        window_size: int = 10
    ) -> TurnDynamics:
        """
        Analyze turn-taking dynamics in recent conversation
        
        Args:
            conversation_history: List of conversation turns
            window_size: Number of recent turns to analyze (default: 10)
        
        Returns:
            TurnDynamics with metrics about conversation flow
        """
        recent_turns = conversation_history[-window_size:] if conversation_history else []
        
        if len(recent_turns) < 2:
            # Not enough data
            return TurnDynamics(
                avg_turn_gap=0.0,
                turn_frequency=0.0,
                speaker_alternation=0.0,
                longest_gap=0.0,
                shortest_gap=0.0
            )
        
        # Calculate time gaps between turns
        gaps = []
        alternations = 0
        
        for i in range(1, len(recent_turns)):
            gap = recent_turns[i].timestamp - recent_turns[i-1].timestamp
            gaps.append(gap)
            
            # Check if speaker changed (alternation)
            prev_speaker = recent_turns[i-1].speaker_id if not recent_turns[i-1].is_agent else "agent"
            curr_speaker = recent_turns[i].speaker_id if not recent_turns[i].is_agent else "agent"
            if prev_speaker != curr_speaker:
                alternations += 1
        
        # Calculate metrics
        avg_gap = sum(gaps) / len(gaps) if gaps else 0.0
        longest_gap = max(gaps) if gaps else 0.0
        shortest_gap = min(gaps) if gaps else 0.0
        
        # Turn frequency (turns per minute)
        total_time = recent_turns[-1].timestamp - recent_turns[0].timestamp
        turn_frequency = (len(recent_turns) - 1) / (total_time / 60.0) if total_time > 0 else 0.0
        
        # Speaker alternation rate
        speaker_alternation = alternations / (len(recent_turns) - 1) if len(recent_turns) > 1 else 0.0
        
        return TurnDynamics(
            avg_turn_gap=avg_gap,
            turn_frequency=turn_frequency,
            speaker_alternation=speaker_alternation,
            longest_gap=longest_gap,
            shortest_gap=shortest_gap
        )
    
    def detect_conversation_mode(
        self,
        speaker_tracker: SpeakerTracker,
        turn_dynamics: TurnDynamics
    ) -> ConversationMode:
        """
        Detect the current conversation mode
        
        Args:
            speaker_tracker: SpeakerTracker with conversation state
            turn_dynamics: Analyzed turn dynamics
        
        Returns:
            ConversationMode enum value
        """
        speaker_count = speaker_tracker.get_speaker_count()
        recent_turns = speaker_tracker.get_recent_turns(count=10)
        
        if len(recent_turns) < 3:
            return ConversationMode.UNKNOWN
        
        # Monologue: Single speaker, low alternation
        if speaker_count == 1:
            return ConversationMode.MONOLOGUE
        
        # Debate: 2 speakers, fast back-and-forth, high alternation
        if (speaker_count == 2 and 
            turn_dynamics.avg_turn_gap < 2.0 and
            turn_dynamics.speaker_alternation > 0.7):
            return ConversationMode.DEBATE
        
        # Interview: One speaker dominates (agent or one human)
        # Check if one speaker has significantly more words
        speaker_words = {}
        for turn in recent_turns:
            if not turn.is_agent:
                sid = turn.speaker_id
                if sid not in speaker_words:
                    speaker_words[sid] = 0
                speaker_words[sid] += len(turn.text.split())
        
        if speaker_words:
            max_words = max(speaker_words.values())
            total_words = sum(speaker_words.values())
            if max_words / total_words > 0.7:  # One speaker has 70%+ of words
                return ConversationMode.INTERVIEW
        
        # Discussion: Multiple speakers, moderate pace
        if speaker_count >= 2:
            return ConversationMode.DISCUSSION
        
        return ConversationMode.UNKNOWN
    
    def calculate_topic_relevance(self, text: str) -> float:
        """
        Calculate how relevant the text is to agent's expertise
        
        Args:
            text: Text to analyze
        
        Returns:
            Relevance score 0.0-1.0
        """
        text_lower = text.lower()
        
        # Count matching keywords across all topics
        total_matches = 0
        max_possible = 0
        
        for topic, keywords in self.topic_keywords.items():
            max_possible += len(keywords)
            for keyword in keywords:
                if keyword in text_lower:
                    total_matches += 1
        
        # Normalize to 0-1 range
        relevance = min(1.0, total_matches / 3.0)  # Cap at 3 keywords = 1.0
        
        return relevance
    
    def calculate_engagement_score(
        self,
        speaker_tracker: SpeakerTracker,
        last_utterance: str,
        direct_address: bool,
        current_time: Optional[float] = None,
        base_silence_threshold: float = 3.0
    ) -> Tuple[float, EngagementFactors]:
        """
        Calculate engagement score (0-1) for whether agent should respond
        
        Args:
            speaker_tracker: SpeakerTracker with conversation state
            last_utterance: Most recent utterance text
            direct_address: Was agent directly addressed
            current_time: Current timestamp (default: now)
            base_silence_threshold: Base silence requirement (seconds)
        
        Returns:
            Tuple of (score, factors) where score is 0-1
        """
        if current_time is None:
            current_time = time.time()
        
        # Get conversation state
        recent_turns = speaker_tracker.get_recent_turns(count=10)
        turn_dynamics = self.analyze_turn_dynamics(recent_turns)
        conv_mode = self.detect_conversation_mode(speaker_tracker, turn_dynamics)
        
        # Calculate time since last utterance
        silence_duration = 0.0
        if recent_turns:
            silence_duration = current_time - recent_turns[-1].timestamp
        
        # Adjust silence threshold based on conversation mode
        silence_threshold = base_silence_threshold
        if conv_mode == ConversationMode.DEBATE:
            silence_threshold *= self.debate_silence_multiplier
        
        # Calculate individual factors (0-1 scale)
        
        # 1. Silence factor: Have we waited long enough?
        silence_factor = min(1.0, silence_duration / silence_threshold)
        
        # 2. Direct address factor: Strongly favor responding if addressed
        address_factor = 1.0 if direct_address else 0.3
        
        # 3. Topic relevance factor
        relevance_factor = self.calculate_topic_relevance(last_utterance)
        
        # 4. Turn count factor: More turns = more engaged conversation
        turn_factor = min(1.0, len(recent_turns) / 10.0)
        
        # 5. Speaker count factor: More speakers = wait for clearer opening
        speaker_count = speaker_tracker.get_speaker_count()
        speaker_factor = 1.0 if speaker_count <= 2 else 0.7
        
        # Weighted combination of factors
        score = (
            silence_factor * 0.30 +     # 30% weight on silence
            address_factor * 0.35 +     # 35% weight on direct address
            relevance_factor * 0.15 +   # 15% weight on topic
            turn_factor * 0.10 +        # 10% weight on engagement
            speaker_factor * 0.10       # 10% weight on speaker count
        )
        
        # Create factors object for logging
        factors = EngagementFactors(
            silence_duration=silence_duration,
            direct_address=direct_address,
            topic_relevance=relevance_factor,
            turn_count=len(recent_turns),
            speaker_count=speaker_count
        )
        
        return score, factors
    
    def should_respond_advanced(
        self,
        speaker_tracker: SpeakerTracker,
        last_utterance: str,
        direct_address: bool,
        current_time: Optional[float] = None,
        base_silence_threshold: float = 3.0
    ) -> Tuple[bool, float, str]:
        """
        Advanced decision on whether agent should respond
        
        Args:
            speaker_tracker: SpeakerTracker with conversation state
            last_utterance: Most recent utterance text
            direct_address: Was agent directly addressed
            current_time: Current timestamp (default: now)
            base_silence_threshold: Base silence requirement (seconds)
        
        Returns:
            Tuple of (should_respond, score, reason)
        """
        score, factors = self.calculate_engagement_score(
            speaker_tracker, last_utterance, direct_address,
            current_time, base_silence_threshold
        )
        
        # Get conversation mode for logging
        recent_turns = speaker_tracker.get_recent_turns(count=10)
        turn_dynamics = self.analyze_turn_dynamics(recent_turns)
        conv_mode = self.detect_conversation_mode(speaker_tracker, turn_dynamics)
        
        # Determine if we should respond
        should_respond = score >= self.engagement_threshold
        
        # Build reason string
        if should_respond:
            if direct_address:
                reason = f"direct address detected (score: {score:.2f})"
            elif factors.silence_duration > base_silence_threshold * 1.5:
                reason = f"long silence ({factors.silence_duration:.1f}s, score: {score:.2f})"
            else:
                reason = f"engagement threshold met (score: {score:.2f})"
        else:
            if conv_mode == ConversationMode.DEBATE:
                reason = f"active debate detected (score: {score:.2f}, waiting for break)"
            else:
                reason = f"engagement too low (score: {score:.2f} < {self.engagement_threshold})"
        
        # Log analysis
        logger.info(f"📊 [ANALYSIS] Conversation mode: {conv_mode.value}")
        logger.info(f"📊 [ANALYSIS] Turn frequency: {turn_dynamics.avg_turn_gap:.2f}s avg gap")
        logger.info(f"📊 [ANALYSIS] Topic relevance: {factors.topic_relevance:.2f}")
        logger.info(f"🤔 [ENGAGEMENT] Score: {score:.2f} (threshold: {self.engagement_threshold})")
        logger.info(f"🤔 [RESPONSE] Decision: {'RESPOND' if should_respond else 'SKIP'} ({reason})")
        
        return should_respond, score, reason
