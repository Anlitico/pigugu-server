# agent/unified_logger.py
"""
Unified logging system that sends all logs to both console and web client
with step tracking and timing information.
"""

import time
import asyncio
from typing import Optional
from loguru import logger
from livekit import rtc


class UnifiedLogger:
    """
    Unified logger that sends all logs to both console/file and web client.
    Tracks steps and timing between events.
    """
    
    def __init__(self, room: Optional[rtc.Room] = None):
        self.room = room
        self.last_timestamp = time.time()
        self.step_counter = 0
        
    def set_room(self, room: rtc.Room):
        """Set the LiveKit room for publishing logs to web client"""
        self.room = room
        
    def _get_elapsed_time(self) -> float:
        """Get time elapsed since last log (in seconds)"""
        current_time = time.time()
        elapsed = current_time - self.last_timestamp
        self.last_timestamp = current_time
        return elapsed
    
    def _format_log_message(self, step: str, message: str, include_timing: bool = True) -> tuple[str, str]:
        """
        Format log message with step and timing information.
        Returns (console_message, web_message)
        """
        if include_timing:
            elapsed = self._get_elapsed_time()
            self.step_counter += 1
            web_message = f"[Step {self.step_counter}] {step}: {message} | ⏱️ +{elapsed:.3f}s"
            console_message = f"[Step {self.step_counter}] {step}: {message} (⏱️ +{elapsed:.3f}s)"
        else:
            console_message = f"{step}: {message}"
            web_message = f"{step}: {message}"
        
        return console_message, web_message
    
    async def _publish_to_web(self, message: str):
        """Publish log message to web client"""
        if self.room:
            try:
                await self.room.local_participant.publish_data(
                    message.encode('utf-8'),
                    reliable=True,
                    topic="unified_log"
                )
            except Exception as e:
                logger.error(f"Failed to publish log to web: {e}")
    
    def log_step(self, step: str, message: str, level: str = "info", include_timing: bool = True):
        """
        Log a step with timing information.
        
        Args:
            step: Step name (e.g., "STT", "LLM", "TTS", "ROOM")
            message: Log message
            level: Log level (info, debug, warning, error)
            include_timing: Whether to include timing information
        """
        console_msg, web_msg = self._format_log_message(step, message, include_timing)
        
        # Log to console/file
        if level == "debug":
            logger.debug(console_msg)
        elif level == "warning":
            logger.warning(console_msg)
        elif level == "error":
            logger.error(console_msg)
        else:
            logger.info(console_msg)
        
        # Publish to web client (async)
        if self.room:
            asyncio.create_task(self._publish_to_web(web_msg))
    
    def reset_timing(self):
        """Reset timing counter (e.g., at start of new conversation)"""
        self.last_timestamp = time.time()
        
    def reset_step_counter(self):
        """Reset step counter"""
        self.step_counter = 0


# Global unified logger instance
_unified_logger: Optional[UnifiedLogger] = None


def get_unified_logger() -> UnifiedLogger:
    """Get the global unified logger instance"""
    global _unified_logger
    if _unified_logger is None:
        _unified_logger = UnifiedLogger()
    return _unified_logger


def init_unified_logger(room: rtc.Room):
    """Initialize the unified logger with a LiveKit room"""
    global _unified_logger
    _unified_logger = UnifiedLogger(room)
    return _unified_logger
