#!/usr/bin/env python3
"""
US Latency Test - Measures realistic response latency for voice agent

This test simulates a US user by:
1. Connecting to LiveKit as a participant
2. Publishing pre-recorded audio (simulating user speech)
3. Measuring time from user stops speaking to agent starts speaking
4. Waiting for agent to finish speaking
5. Reporting detailed timing breakdown

Key Metric: Response Latency (T1→T2)
- T1: User finishes speaking
- T2: Agent starts speaking
- This is the "awkward silence" users experience

Usage:
    uv run python test_latency_us.py

Requirements:
    - Agent must be running (ENV=CLOUD uv run python main.py start)
    - Test audio files in test_audio/ directory:
      - question1.m4a: "Hello, good morning"
      - question2.m4a: "What do you think of Japan?"
    - ffmpeg installed on the system (for M4A decoding)
"""

import asyncio
import os
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import numpy as np
from dotenv import load_dotenv
from livekit import rtc, api

load_dotenv()


@dataclass
class TurnTiming:
    """Timing data for a single conversation turn"""
    question: str
    user_start: float = 0.0
    user_end: float = 0.0
    agent_start: float = 0.0
    agent_end: float = 0.0
    
    @property
    def user_duration(self) -> float:
        """How long user spoke"""
        return self.user_end - self.user_start
    
    @property
    def response_latency(self) -> float:
        """THE KEY METRIC: Time from user stops to agent starts"""
        if self.agent_start and self.user_end:
            return self.agent_start - self.user_end
        return 0.0
    
    @property
    def agent_duration(self) -> float:
        """How long agent spoke"""
        if self.agent_end and self.agent_start:
            return self.agent_end - self.agent_start
        return 0.0
    
    @property
    def total_turn_time(self) -> float:
        """Full turn duration"""
        if self.agent_end and self.user_start:
            return self.agent_end - self.user_start
        return 0.0


class LatencyTest:
    """Latency test runner"""
    
    def __init__(self):
        self.room: Optional[rtc.Room] = None
        self.audio_source: Optional[rtc.AudioSource] = None
        self.current_timing: Optional[TurnTiming] = None
        self.agent_speaking = False
        self.agent_audio_received = asyncio.Event()
        self.agent_finished_speaking = asyncio.Event()
        self.results: list[TurnTiming] = []
        
        # Audio settings
        self.sample_rate = 16000
        self.num_channels = 1
        
    def generate_token(self, room_name: str = "test-room") -> str:
        """Generate LiveKit access token"""
        api_key = os.getenv("LIVEKIT_API_KEY")
        api_secret = os.getenv("LIVEKIT_API_SECRET")
        
        if not api_key or not api_secret:
            raise ValueError("LIVEKIT_API_KEY and LIVEKIT_API_SECRET required in .env")
        
        token = api.AccessToken(api_key, api_secret)
        token.with_identity("latency-tester")
        token.with_name("Latency Test User")
        token.with_grants(api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True
        ))
        
        return token.to_jwt()
    
    def load_audio_file(self, filepath: str) -> tuple[bytes, float]:
        """
        Load audio file and return raw PCM data and duration
        
        - For WAV files: reads directly (no external dependencies)
        - For M4A/MP3: uses ffmpeg to convert
        
        Returns:
            tuple: (pcm_bytes, duration_seconds)
        """
        print(f"  Loading audio: {filepath}")
        
        # Check if it's a WAV file - can read directly
        if filepath.lower().endswith('.wav'):
            return self._load_wav_file(filepath)
        else:
            return self._load_with_ffmpeg(filepath)
    
    def _load_wav_file(self, filepath: str) -> tuple[bytes, float]:
        """Load WAV file and convert to 16kHz mono"""
        with wave.open(filepath, 'rb') as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            framerate = wav.getframerate()
            n_frames = wav.getnframes()
            
            print(f"  Original: {channels}ch, {sample_width*8}bit, {framerate}Hz")
            
            raw_data = wav.readframes(n_frames)
            duration = n_frames / framerate
        
        # Convert to numpy array
        if sample_width == 2:
            dtype = np.int16
        elif sample_width == 1:
            dtype = np.uint8
        else:
            raise ValueError(f"Unsupported sample width: {sample_width}")
        
        audio_array = np.frombuffer(raw_data, dtype=dtype)
        
        # Convert stereo to mono if needed
        if channels == 2:
            audio_array = audio_array.reshape(-1, 2)
            audio_array = audio_array.mean(axis=1).astype(np.int16)
            print(f"  Converted to mono")
        
        # Resample to target sample rate if needed
        if framerate != self.sample_rate:
            # Simple resampling using numpy interpolation
            original_length = len(audio_array)
            target_length = int(original_length * self.sample_rate / framerate)
            
            # Use linear interpolation for resampling
            x_original = np.linspace(0, 1, original_length)
            x_target = np.linspace(0, 1, target_length)
            audio_array = np.interp(x_target, x_original, audio_array).astype(np.int16)
            
            print(f"  Resampled: {framerate}Hz -> {self.sample_rate}Hz")
        
        # Convert back to bytes
        audio_data = audio_array.tobytes()
        
        # Recalculate duration based on resampled data
        duration = len(audio_array) / self.sample_rate
        
        print(f"  Final: 1ch, 16bit, {self.sample_rate}Hz, {duration:.2f}s")
        return audio_data, duration
    
    def _load_with_ffmpeg(self, filepath: str) -> tuple[bytes, float]:
        """Load audio file using ffmpeg (for M4A, MP3, etc.)"""
        # Use ffmpeg to convert to WAV (mono, 16kHz, 16-bit)
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_file:
            tmp_wav_path = tmp_file.name
        
        try:
            # Convert to WAV using ffmpeg
            cmd = [
                'ffmpeg', '-y', '-i', filepath,
                '-ar', str(self.sample_rate),  # 16kHz
                '-ac', '1',  # mono
                '-sample_fmt', 's16',  # 16-bit signed
                '-f', 'wav',
                tmp_wav_path
            ]
            
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                print(f"  ffmpeg error: {result.stderr}")
                raise RuntimeError(f"ffmpeg failed: {result.stderr}")
            
            # Read the converted WAV file
            with wave.open(tmp_wav_path, 'rb') as wav:
                n_frames = wav.getnframes()
                framerate = wav.getframerate()
                audio_data = wav.readframes(n_frames)
                duration = n_frames / framerate
                
                print(f"  Converted: 1ch, 16bit, {framerate}Hz, {duration:.2f}s")
            
            return audio_data, duration
            
        finally:
            # Clean up temp file
            if os.path.exists(tmp_wav_path):
                os.unlink(tmp_wav_path)
    
    async def publish_audio(self, audio_data: bytes, duration: float):
        """Publish audio data to the room"""
        if not self.audio_source:
            raise RuntimeError("Audio source not initialized")
        
        # Calculate frame parameters
        samples_per_frame = 480  # 30ms at 16kHz
        bytes_per_sample = 2  # 16-bit audio
        bytes_per_frame = samples_per_frame * bytes_per_sample
        frame_duration = samples_per_frame / self.sample_rate
        
        # Publish audio in frames
        offset = 0
        while offset < len(audio_data):
            chunk = audio_data[offset:offset + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                # Pad last frame with silence
                chunk = chunk + b'\x00' * (bytes_per_frame - len(chunk))
            
            # Create audio frame
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=self.sample_rate,
                num_channels=self.num_channels,
                samples_per_channel=samples_per_frame
            )
            
            await self.audio_source.capture_frame(frame)
            offset += bytes_per_frame
            
            # Wait to maintain real-time playback
            await asyncio.sleep(frame_duration * 0.9)  # Slightly faster to avoid buffer underrun
    
    async def on_track_subscribed(self, track: rtc.Track, publication: rtc.TrackPublication, participant: rtc.RemoteParticipant):
        """Handle when we receive a track from the agent"""
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"  Subscribed to agent audio track from {participant.identity}")
            
            # Create audio stream to receive agent audio
            audio_stream = rtc.AudioStream(track)
            
            asyncio.create_task(self._monitor_agent_audio(audio_stream))
    
    async def _monitor_agent_audio(self, audio_stream: rtc.AudioStream):
        """Monitor agent audio to detect when they start/stop speaking"""
        silence_threshold = 0.5  # seconds of silence to consider "stopped speaking"
        last_audio_time = 0.0
        audio_detected = False
        
        async for frame_event in audio_stream:
            frame = frame_event.frame
            current_time = time.perf_counter()
            
            # Check if frame has meaningful audio (not just silence)
            # Simple check: any non-zero samples
            has_audio = any(b != 0 for b in frame.data[:100])  # Check first 100 bytes
            
            if has_audio:
                if not audio_detected:
                    # Agent started speaking
                    audio_detected = True
                    if self.current_timing and not self.current_timing.agent_start:
                        self.current_timing.agent_start = current_time
                        print(f"  Agent started speaking at {current_time:.3f}")
                    self.agent_audio_received.set()
                
                last_audio_time = current_time
                self.agent_speaking = True
            
            # Check for silence (agent stopped speaking)
            if audio_detected and (current_time - last_audio_time) > silence_threshold:
                if self.agent_speaking:
                    self.agent_speaking = False
                    if self.current_timing:
                        self.current_timing.agent_end = last_audio_time
                        print(f"  Agent stopped speaking at {last_audio_time:.3f}")
                    self.agent_finished_speaking.set()
                    audio_detected = False
    
    async def run_turn(self, audio_file: str, question: str) -> TurnTiming:
        """Run a single conversation turn"""
        print(f"\n{'='*60}")
        print(f"Turn: \"{question}\"")
        print(f"{'='*60}")
        
        # Reset events
        self.agent_audio_received.clear()
        self.agent_finished_speaking.clear()
        
        # Load audio
        audio_data, duration = self.load_audio_file(audio_file)
        
        # Initialize timing
        self.current_timing = TurnTiming(question=question)
        
        # Record user start time
        self.current_timing.user_start = time.perf_counter()
        print(f"  User started speaking at {self.current_timing.user_start:.3f}")
        
        # Publish audio (simulating user speaking)
        await self.publish_audio(audio_data, duration)
        
        # Record user end time
        self.current_timing.user_end = time.perf_counter()
        print(f"  User stopped speaking at {self.current_timing.user_end:.3f}")
        print(f"  User speech duration: {self.current_timing.user_duration:.2f}s")
        
        # Wait for agent to respond (with timeout)
        print("  Waiting for agent response...")
        try:
            await asyncio.wait_for(self.agent_audio_received.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            print("  ERROR: Agent did not respond within 15 seconds")
            return self.current_timing
        
        # Wait for agent to finish speaking (with timeout)
        print("  Waiting for agent to finish speaking...")
        try:
            await asyncio.wait_for(self.agent_finished_speaking.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            print("  WARNING: Agent speaking timeout (60s) - may still be talking")
            self.current_timing.agent_end = time.perf_counter()
        
        # Store result
        result = self.current_timing
        self.results.append(result)
        
        return result
    
    async def connect(self, room_name: str = "test-room"):
        """Connect to LiveKit room"""
        livekit_url = os.getenv("LIVEKIT_URL", "wss://shrump-test-jbnvclwi.livekit.cloud")
        token = self.generate_token(room_name)
        
        print(f"Connecting to LiveKit: {livekit_url}")
        print(f"Room: {room_name}")
        
        self.room = rtc.Room()
        
        # Set up event handlers
        @self.room.on("track_subscribed")
        def on_track_subscribed(track, publication, participant):
            asyncio.create_task(self.on_track_subscribed(track, publication, participant))
        
        # Connect to room
        await self.room.connect(livekit_url, token)
        print(f"Connected! Local participant: {self.room.local_participant.identity}")
        
        # Create and publish audio track
        self.audio_source = rtc.AudioSource(self.sample_rate, self.num_channels)
        audio_track = rtc.LocalAudioTrack.create_audio_track("user-audio", self.audio_source)
        
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        
        publication = await self.room.local_participant.publish_track(audio_track, options)
        print(f"Published audio track: {publication.sid}")
        
        # List current participants
        print(f"Room participants: {len(self.room.remote_participants)}")
        for pid, p in self.room.remote_participants.items():
            print(f"  - {p.identity} (tracks: {len(p.track_publications)})")
        
        # Wait for agent to detect us and be fully ready
        # The agent needs time to: receive job dispatch, connect to room, initialize STT/LLM/TTS
        print("Waiting 15 seconds for agent to fully initialize and subscribe...")
        await asyncio.sleep(15)
        
        # Check again for participants
        print(f"Room participants after wait: {len(self.room.remote_participants)}")
        for pid, p in self.room.remote_participants.items():
            print(f"  - {p.identity} (tracks: {len(p.track_publications)})")
    
    async def disconnect(self):
        """Disconnect from room"""
        if self.room:
            await self.room.disconnect()
            print("Disconnected from room")
    
    def print_results(self):
        """Print formatted results"""
        print("\n")
        print("=" * 70)
        print("LATENCY TEST RESULTS")
        print("=" * 70)
        
        for i, timing in enumerate(self.results, 1):
            print(f"\nTurn {i}: \"{timing.question}\"")
            print("-" * 50)
            print(f"  User speaking:      0.0s → {timing.user_duration:.1f}s  ({timing.user_duration:.1f}s duration)")
            print(f"  Waiting for agent:  {timing.user_duration:.1f}s → {timing.user_duration + timing.response_latency:.1f}s  ★ RESPONSE LATENCY: {timing.response_latency:.2f}s ★")
            print(f"  Agent speaking:     {timing.user_duration + timing.response_latency:.1f}s → {timing.total_turn_time:.1f}s  ({timing.agent_duration:.1f}s duration)")
            print(f"  Total turn time:    {timing.total_turn_time:.1f}s")
        
        # Summary
        response_latencies = [t.response_latency for t in self.results if t.response_latency > 0]
        
        if response_latencies:
            print("\n")
            print("=" * 70)
            print("SUMMARY - Response Latency (the wait time users feel)")
            print("=" * 70)
            
            for i, timing in enumerate(self.results, 1):
                status = ""
                if timing.response_latency < 2.0:
                    status = "✓ Good"
                elif timing.response_latency < 3.0:
                    status = "⚠ Acceptable"
                else:
                    status = "✗ Slow"
                print(f"  Turn {i}: {timing.response_latency:.2f}s  {status}")
            
            avg = sum(response_latencies) / len(response_latencies)
            print(f"\n  Average: {avg:.2f}s")
            print("\n  Thresholds:")
            print("    ✓ Under 2s = Good conversational flow")
            print("    ⚠ 2-3s = Noticeable but acceptable")
            print("    ✗ Over 3s = Feels slow/awkward")


async def main():
    """Main test entry point"""
    print("=" * 70)
    print("US LATENCY TEST")
    print("=" * 70)
    
    # Check for test audio files (support both WAV and M4A)
    test_audio_dir = Path(__file__).parent / "test_audio"
    
    # Try WAV first, then M4A
    question1_file = None
    question2_file = None
    
    for ext in ['.wav', '.m4a']:
        q1 = test_audio_dir / f"question1{ext}"
        q2 = test_audio_dir / f"question2{ext}"
        if q1.exists() and question1_file is None:
            question1_file = q1
        if q2.exists() and question2_file is None:
            question2_file = q2
    
    if not test_audio_dir.exists():
        print(f"\nERROR: Test audio directory not found: {test_audio_dir}")
        print("\nPlease create the directory and add your test audio files:")
        print(f"  mkdir -p {test_audio_dir}")
        print("  - question1.wav (or .m4a): 'Hello, good morning' (~1-2 seconds)")
        print("  - question2.wav (or .m4a): 'What do you think of Japan?' (~2-3 seconds)")
        print("\nWAV format recommended (no ffmpeg needed). M4A requires ffmpeg.")
        return
    
    if question1_file is None or question2_file is None:
        print(f"\nERROR: Test audio files not found:")
        if question1_file is None:
            print(f"  Missing: question1.wav or question1.m4a")
        if question2_file is None:
            print(f"  Missing: question2.wav or question2.m4a")
        print("\nPlease add your test audio files (WAV or M4A format)")
        return
    
    print(f"Found audio files:")
    print(f"  Question 1: {question1_file}")
    print(f"  Question 2: {question2_file}")
    
    # Run test
    test = LatencyTest()
    
    try:
        await test.connect()
        
        # Run turn 1: "Hello, good morning"
        await test.run_turn(str(question1_file), "Hello, good morning")
        
        # Brief pause between turns
        print("\nWaiting 2 seconds before next question...")
        await asyncio.sleep(2)
        
        # Run turn 2: "What do you think of Japan?"
        await test.run_turn(str(question2_file), "What do you think of Japan?")
        
        # Print results
        test.print_results()
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await test.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
