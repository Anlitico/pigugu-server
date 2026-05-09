#!/usr/bin/env python3
"""
Test script for AI Agent deployment

This script validates:
1. Configuration loading
2. API key presence
3. LiveKit connection
4. STT/LLM/TTS component initialization
5. Basic agent functionality
"""

import asyncio
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from config import get_config, AI_PERSONALITY
from core.stt import create_stt
from core.llm import create_llm
from core.tts import create_tts

# Load environment variables
load_dotenv()

# Configure logger
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    level="INFO",
    colorize=True,
)


class DeploymentTest:
    """Test suite for agent deployment"""
    
    def __init__(self):
        self.config = None
        self.passed = 0
        self.failed = 0
        self.warnings = 0
    
    def test_header(self, test_name: str):
        """Print test header"""
        logger.info("=" * 70)
        logger.info(f"TEST: {test_name}")
        logger.info("=" * 70)
    
    def test_pass(self, message: str):
        """Mark test as passed"""
        self.passed += 1
        logger.success(f"✓ PASS: {message}")
    
    def test_fail(self, message: str):
        """Mark test as failed"""
        self.failed += 1
        logger.error(f"✗ FAIL: {message}")
    
    def test_warn(self, message: str):
        """Mark test as warning"""
        self.warnings += 1
        logger.warning(f"⚠ WARN: {message}")
    
    def test_configuration_loading(self):
        """Test 1: Configuration Loading"""
        self.test_header("Configuration Loading")
        
        try:
            self.config = get_config()
            self.test_pass("Configuration loaded successfully")
            
            # Check environment
            env = os.getenv("ENV", "DEV")
            logger.info(f"Current Environment: {env}")
            
            return True
        except Exception as e:
            self.test_fail(f"Failed to load configuration: {e}")
            return False
    
    def test_api_keys(self):
        """Test 2: API Keys Validation"""
        self.test_header("API Keys Validation")
        
        required_keys = {
            "LIVEKIT_API_KEY": "LiveKit API Key",
            "LIVEKIT_API_SECRET": "LiveKit API Secret",
            "CARTESIA_API_KEY": "Cartesia API Key (STT & TTS)",
            "DASHSCOPE_API_KEY": "DashScope API Key (Qwen LLM)",
        }
        
        all_present = True
        for key, description in required_keys.items():
            value = os.getenv(key)
            if value:
                masked = value[:8] + "..." + value[-4:] if len(value) > 12 else "***"
                self.test_pass(f"{description}: {masked}")
            else:
                self.test_fail(f"{description} is missing")
                all_present = False
        
        return all_present
    
    def test_configuration_values(self):
        """Test 3: Configuration Values"""
        self.test_header("Configuration Values")
        
        if not self.config:
            self.test_fail("Configuration not loaded")
            return False
        
        try:
            # LiveKit
            logger.info(f"LiveKit URL: {self.config.LIVEKIT_URL}")
            
            # STT
            logger.info(f"STT Model: {self.config.CARTESIA_STT_MODEL}")
            logger.info(f"STT Language: {self.config.CARTESIA_STT_LANGUAGE}")
            logger.info(f"STT Sample Rate: {self.config.CARTESIA_STT_SAMPLE_RATE}")
            
            # TTS
            logger.info(f"TTS Model: {self.config.CARTESIA_TTS_MODEL}")
            logger.info(f"TTS Voice: {self.config.CARTESIA_TTS_VOICE}")
            logger.info(f"TTS Sample Rate: {self.config.CARTESIA_TTS_SAMPLE_RATE}")
            
            # LLM
            llm_provider = self.config.LLM_PROVIDER.lower()
            if llm_provider == "grok" or llm_provider == "xai":
                logger.info(f"LLM Model: {self.config.GROK_MODEL}")
            else:
                logger.info(f"LLM Model: {self.config.QWEN_MODEL}")
            logger.info(f"LLM Provider: {self.config.LLM_PROVIDER}")
            logger.info(f"LLM Temperature: {self.config.LLM_TEMPERATURE}")
            llm_config = self.config.get_llm_config()
            logger.info(f"LLM Base URL: {llm_config['base_url']}")
            logger.info(f"Policy Search Enabled: {self.config.ENABLE_POLICY_SEARCH}")
            logger.info(f"Force Policy Search: {self.config.FORCE_POLICY_SEARCH}")
            logger.info(f"Prompt Length: {len(AI_PERSONALITY)} chars")
            
            # Agent
            logger.info(f"Agent Workers: {self.config.AGENT_WORKERS}")
            logger.info(f"Interruptions: {self.config.ENABLE_INTERRUPTIONS}")
            logger.info(f"Welcome Greeting: {self.config.ENABLE_WELCOME_GREETING}")
            
            self.test_pass("All configuration values loaded")
            return True
        except Exception as e:
            self.test_fail(f"Configuration values error: {e}")
            return False
    
    def test_stt_initialization(self):
        """Test 4: STT Component Initialization"""
        self.test_header("STT Component Initialization")
        
        try:
            cartesia_api_key = os.getenv("CARTESIA_API_KEY")
            
            stt = create_stt(
                model=self.config.CARTESIA_STT_MODEL,
                language=self.config.CARTESIA_STT_LANGUAGE,
                encoding=self.config.CARTESIA_STT_ENCODING,
                sample_rate=self.config.CARTESIA_STT_SAMPLE_RATE,
                api_key=cartesia_api_key,
                base_url=self.config.CARTESIA_STT_BASE_URL
            )
            
            self.test_pass(f"STT initialized: {self.config.CARTESIA_STT_MODEL}")
            logger.info(f"STT Type: {type(stt).__name__}")
            return True
        except Exception as e:
            self.test_fail(f"STT initialization failed: {e}")
            return False
    
    def test_llm_initialization(self):
        """Test 5: LLM Component Initialization"""
        self.test_header("LLM Component Initialization")
        
        try:
            llm_config = self.config.get_llm_config()
            
            # Determine model based on provider
            llm_provider = self.config.LLM_PROVIDER.lower()
            if llm_provider == "grok" or llm_provider == "xai":
                model = self.config.GROK_MODEL
            else:
                model = self.config.QWEN_MODEL
            
            llm = create_llm(
                model=model,
                temperature=self.config.LLM_TEMPERATURE,
                instructions=AI_PERSONALITY,
                max_tokens=self.config.LLM_MAX_TOKENS,
                api_key=llm_config["api_key"],
                base_url=llm_config["base_url"]
            )
            
            self.test_pass(f"LLM initialized: {model} (Provider: {self.config.LLM_PROVIDER})")
            logger.info(f"LLM Type: {type(llm).__name__}")
            return True
        except Exception as e:
            self.test_fail(f"LLM initialization failed: {e}")
            return False
    
    def test_tts_initialization(self):
        """Test 6: TTS Component Initialization"""
        self.test_header("TTS Component Initialization")
        
        try:
            cartesia_api_key = os.getenv("CARTESIA_API_KEY")
            
            # Parse emotion if provided
            emotion_list = None
            if self.config.CARTESIA_TTS_EMOTION:
                emotion_list = [e.strip() for e in self.config.CARTESIA_TTS_EMOTION.split(",")]
            
            tts = create_tts(
                model=self.config.CARTESIA_TTS_MODEL,
                language=self.config.CARTESIA_TTS_LANGUAGE,
                encoding=self.config.CARTESIA_TTS_ENCODING,
                voice=self.config.CARTESIA_TTS_VOICE,
                speed=self.config.CARTESIA_TTS_SPEED,
                emotion=emotion_list,
                volume=self.config.CARTESIA_TTS_VOLUME,
                sample_rate=self.config.CARTESIA_TTS_SAMPLE_RATE,
                word_timestamps=self.config.CARTESIA_TTS_WORD_TIMESTAMPS,
                api_key=cartesia_api_key,
                base_url=self.config.CARTESIA_TTS_BASE_URL
            )
            
            self.test_pass(f"TTS initialized: {self.config.CARTESIA_TTS_MODEL}")
            logger.info(f"TTS Type: {type(tts).__name__}")
            return True
        except Exception as e:
            self.test_fail(f"TTS initialization failed: {e}")
            return False
    
    async def test_livekit_connection(self):
        """Test 7: LiveKit Connection"""
        self.test_header("LiveKit Connection Test")
        
        try:
            from livekit import api
            
            livekit_url = self.config.LIVEKIT_URL
            livekit_api_key = os.getenv("LIVEKIT_API_KEY")
            livekit_api_secret = os.getenv("LIVEKIT_API_SECRET")
            
            # Create LiveKit API client
            lkapi = api.LiveKitAPI(
                livekit_url,
                livekit_api_key,
                livekit_api_secret,
            )
            
            # Try to list rooms (this validates the connection)
            rooms = await lkapi.room.list_rooms(api.ListRoomsRequest())
            
            self.test_pass(f"LiveKit connection successful")
            logger.info(f"Connected to: {livekit_url}")
            logger.info(f"Active rooms: {len(rooms.rooms)}")
            
            await lkapi.aclose()
            return True
        except Exception as e:
            self.test_fail(f"LiveKit connection failed: {e}")
            self.test_warn("Make sure LiveKit server is running")
            return False
    
    def test_file_structure(self):
        """Test 8: File Structure"""
        self.test_header("File Structure")
        
        required_files = [
            "main.py",
            "config.py",
            "stt.py",
            "llm.py",
            "tts.py",
            ".config",
            ".env",
        ]
        
        agent_dir = Path(__file__).parent
        all_present = True
        
        for filename in required_files:
            filepath = agent_dir / filename
            if filepath.exists():
                self.test_pass(f"Found: {filename}")
            else:
                self.test_fail(f"Missing: {filename}")
                all_present = False
        
        return all_present
    
    def print_summary(self):
        """Print test summary"""
        logger.info("=" * 70)
        logger.info("TEST SUMMARY")
        logger.info("=" * 70)
        logger.success(f"Passed:   {self.passed}")
        if self.failed > 0:
            logger.error(f"Failed:   {self.failed}")
        else:
            logger.info(f"Failed:   {self.failed}")
        if self.warnings > 0:
            logger.warning(f"Warnings: {self.warnings}")
        logger.info("=" * 70)
        
        if self.failed == 0:
            logger.success("✓ All tests passed! Agent is ready for deployment.")
            return True
        else:
            logger.error("✗ Some tests failed. Please fix the issues above.")
            return False


async def main():
    """Run all deployment tests"""
    logger.info("🚀 Starting Agent Deployment Tests")
    logger.info("")
    
    tester = DeploymentTest()
    
    # Run tests
    tester.test_file_structure()
    tester.test_configuration_loading()
    tester.test_api_keys()
    tester.test_configuration_values()
    tester.test_stt_initialization()
    tester.test_llm_initialization()
    tester.test_tts_initialization()
    await tester.test_livekit_connection()
    
    # Print summary
    logger.info("")
    success = tester.print_summary()
    
    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

