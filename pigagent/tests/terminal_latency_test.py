#!/usr/bin/env python3
"""
Terminal-Based Granular Latency Test

Measures each step of the voice pipeline independently:
- STT: Deepgram WebSocket streaming (realistic end-of-speech to transcript)
- LLM: Qwen via OpenAI SDK (TTFT and total time)
- TTS: Cartesia streaming (TTFB and total time)

Usage:
    ENV=CLOUD uv run python terminal_latency_test.py
    ENV=CLOUD uv run python terminal_latency_test.py --no-playback
    ENV=CLOUD uv run python terminal_latency_test.py --verbose
"""

import argparse
import asyncio
import os
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Fix Windows console encoding for Unicode
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import numpy as np
from dotenv import load_dotenv

load_dotenv()

# Set environment before importing config
if "ENV" not in os.environ:
    os.environ["ENV"] = "CLOUD"

from config import get_config, AI_PERSONALITY


@dataclass
class TimingResult:
    """Timing data for a single question"""
    question_file: str
    transcript: str = ""
    llm_response: str = ""
    
    # Audio info
    audio_duration: float = 0.0
    
    # STT timing (streaming)
    stt_latency: float = 0.0  # Time from last audio chunk to final transcript
    
    # LLM timing
    llm_ttft: float = 0.0  # Time to first token
    llm_total: float = 0.0  # Total generation time
    llm_tokens: int = 0
    
    # TTS timing
    tts_ttfb: float = 0.0  # Time to first audio byte
    tts_total: float = 0.0  # Total synthesis time
    tts_audio_duration: float = 0.0
    
    @property
    def pipeline_total(self) -> float:
        """Total pipeline time (excluding playback)"""
        return self.stt_latency + self.llm_total + self.tts_total


def load_wav_file(filepath: str, target_sample_rate: int = 16000) -> tuple[bytes, float, int]:
    """
    Load WAV file and convert to target sample rate
    
    Returns:
        tuple: (pcm_bytes, duration_seconds, sample_rate)
    """
    with wave.open(filepath, 'rb') as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        framerate = wav.getframerate()
        n_frames = wav.getnframes()
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
    
    # Convert stereo to mono
    if channels == 2:
        audio_array = audio_array.reshape(-1, 2)
        audio_array = audio_array.mean(axis=1).astype(np.int16)
    
    # Resample if needed
    if framerate != target_sample_rate:
        original_length = len(audio_array)
        target_length = int(original_length * target_sample_rate / framerate)
        x_original = np.linspace(0, 1, original_length)
        x_target = np.linspace(0, 1, target_length)
        audio_array = np.interp(x_target, x_original, audio_array).astype(np.int16)
    
    audio_data = audio_array.tobytes()
    duration = len(audio_array) / target_sample_rate
    
    return audio_data, duration, target_sample_rate


async def transcribe_streaming(audio_data: bytes, sample_rate: int, config, verbose: bool = False) -> tuple[str, float]:
    """
    Transcribe audio using Deepgram WebSocket streaming
    
    Streams audio at real-time rate and measures latency from
    end of audio stream to final transcript.
    
    Returns:
        tuple: (transcript, latency_seconds)
    """
    from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
    
    deepgram_api_key = os.getenv("DEEPGRAM_API_KEY")
    if not deepgram_api_key:
        raise ValueError("DEEPGRAM_API_KEY not set in .env")
    
    deepgram = DeepgramClient(deepgram_api_key)
    
    transcript_parts = []
    final_transcript = ""
    finish_signal_time = 0.0
    last_transcript_time = 0.0
    transcript_received = asyncio.Event()
    
    # Create live transcription connection
    dg_connection = deepgram.listen.asyncwebsocket.v("1")
    
    async def on_message(self, result, **kwargs):
        nonlocal final_transcript, last_transcript_time
        if result.is_final:
            transcript = result.channel.alternatives[0].transcript
            if transcript:
                transcript_parts.append(transcript)
                final_transcript = " ".join(transcript_parts)
                last_transcript_time = time.perf_counter()
                if verbose:
                    print(f"    [STT] Final: {transcript}")
    
    async def on_error(self, error, **kwargs):
        print(f"    [STT] Error: {error}")
    
    async def on_close(self, close, **kwargs):
        transcript_received.set()
    
    dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
    dg_connection.on(LiveTranscriptionEvents.Error, on_error)
    dg_connection.on(LiveTranscriptionEvents.Close, on_close)
    
    # Configure options
    options = LiveOptions(
        model=config.DEEPGRAM_STT_MODEL,
        language=config.DEEPGRAM_STT_LANGUAGE,
        sample_rate=sample_rate,
        encoding="linear16",
        channels=1,
        punctuate=True,
        interim_results=False,
    )
    
    # Start connection
    if not await dg_connection.start(options):
        raise RuntimeError("Failed to connect to Deepgram")
    
    if verbose:
        print(f"    [STT] Connected to Deepgram ({config.DEEPGRAM_STT_MODEL})")
    
    # Stream audio at real-time rate
    chunk_duration_ms = 100  # 100ms chunks
    chunk_size = int(sample_rate * chunk_duration_ms / 1000) * 2  # 2 bytes per sample
    
    offset = 0
    while offset < len(audio_data):
        chunk = audio_data[offset:offset + chunk_size]
        await dg_connection.send(chunk)
        offset += chunk_size
        
        # Sleep to simulate real-time streaming
        await asyncio.sleep(chunk_duration_ms / 1000 * 0.95)
    
    # Record when we send the finish signal - this is "end of speech"
    finish_signal_time = time.perf_counter()
    await dg_connection.finish()
    
    # Wait for connection to close (final transcript received)
    try:
        await asyncio.wait_for(transcript_received.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        print("    [STT] Warning: Timeout waiting for transcript")
    
    # Calculate latency: time from finish signal to last transcript
    # If transcript came before finish signal (already processed), latency is ~0
    if last_transcript_time > finish_signal_time:
        latency = last_transcript_time - finish_signal_time
    elif last_transcript_time > 0:
        # Transcript arrived while still streaming - use small positive value
        latency = 0.05  # ~50ms processing overhead
    else:
        latency = 0.0
    
    return final_transcript.strip(), latency


async def generate_llm_response(transcript: str, config, verbose: bool = False) -> tuple[str, float, float, int]:
    """
    Generate LLM response using OpenAI SDK with streaming
    
    Returns:
        tuple: (response_text, ttft_seconds, total_seconds, token_count)
    """
    from openai import AsyncOpenAI
    
    llm_config = config.get_llm_config()
    
    client = AsyncOpenAI(
        api_key=llm_config["api_key"],
        base_url=llm_config["base_url"]
    )
    
    # Determine model
    provider = config.LLM_PROVIDER.lower()
    if provider in ("grok", "xai"):
        model = config.GROK_MODEL
    else:
        model = config.QWEN_MODEL
    
    if verbose:
        print(f"    [LLM] Using {model} at {llm_config['base_url']}")
    
    messages = [
        {"role": "system", "content": AI_PERSONALITY},
        {"role": "user", "content": transcript}
    ]
    
    start_time = time.perf_counter()
    first_token_time = 0.0
    response_text = ""
    token_count = 0
    
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS or 500,
        stream=True
    )
    
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            if not first_token_time:
                first_token_time = time.perf_counter()
                if verbose:
                    print(f"    [LLM] First token received")
            response_text += content
            token_count += 1
    
    end_time = time.perf_counter()
    
    ttft = first_token_time - start_time if first_token_time else 0.0
    total_time = end_time - start_time
    
    return response_text.strip(), ttft, total_time, token_count


async def synthesize_speech(text: str, config, verbose: bool = False) -> tuple[bytes, float, float, float, int]:
    """
    Synthesize speech using Cartesia streaming API
    
    Returns:
        tuple: (audio_bytes, ttfb_seconds, total_seconds, audio_duration, sample_rate)
    """
    import aiohttp
    import json
    
    cartesia_api_key = os.getenv("CARTESIA_API_KEY")
    if not cartesia_api_key:
        raise ValueError("CARTESIA_API_KEY not set in .env")
    
    url = f"{config.CARTESIA_TTS_BASE_URL}/tts/bytes"
    
    headers = {
        "X-API-Key": cartesia_api_key,
        "Cartesia-Version": "2024-06-10",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model_id": config.CARTESIA_TTS_MODEL,
        "transcript": text,
        "voice": {
            "mode": "id",
            "id": config.CARTESIA_TTS_VOICE
        },
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": config.CARTESIA_TTS_SAMPLE_RATE
        }
    }
    
    if config.CARTESIA_TTS_LANGUAGE:
        payload["language"] = config.CARTESIA_TTS_LANGUAGE
    
    if verbose:
        print(f"    [TTS] Using {config.CARTESIA_TTS_MODEL}, voice {config.CARTESIA_TTS_VOICE[:8]}...")
    
    start_time = time.perf_counter()
    first_byte_time = 0.0
    audio_chunks = []
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"Cartesia API error {response.status}: {error_text}")
            
            async for chunk in response.content.iter_chunked(4096):
                if not first_byte_time:
                    first_byte_time = time.perf_counter()
                    if verbose:
                        print(f"    [TTS] First audio chunk received")
                audio_chunks.append(chunk)
    
    end_time = time.perf_counter()
    
    audio_data = b"".join(audio_chunks)
    ttfb = first_byte_time - start_time if first_byte_time else 0.0
    total_time = end_time - start_time
    
    # Calculate audio duration
    sample_rate = config.CARTESIA_TTS_SAMPLE_RATE
    audio_duration = len(audio_data) / (sample_rate * 2)  # 2 bytes per sample
    
    return audio_data, ttfb, total_time, audio_duration, sample_rate


def play_audio(audio_data: bytes, sample_rate: int):
    """Play audio using sounddevice"""
    try:
        import sounddevice as sd
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        audio_float = audio_array.astype(np.float32) / 32768.0
        sd.play(audio_float, sample_rate)
        sd.wait()
    except ImportError:
        print("    [Playback] sounddevice not installed, skipping playback")
    except Exception as e:
        print(f"    [Playback] Error: {e}")


async def run_single_test(audio_file: str, config, verbose: bool = False, play: bool = True) -> TimingResult:
    """Run test for a single audio file"""
    result = TimingResult(question_file=Path(audio_file).name)
    
    print(f"\n{'='*70}")
    print(f"Testing: {Path(audio_file).name}")
    print(f"{'='*70}")
    
    # Load audio
    print("  Loading audio...")
    audio_data, duration, sample_rate = load_wav_file(audio_file, config.DEEPGRAM_STT_SAMPLE_RATE)
    result.audio_duration = duration
    print(f"  Audio: {duration:.2f}s @ {sample_rate}Hz")
    
    # STT
    print("  [1/3] STT (Deepgram streaming)...")
    transcript, stt_latency = await transcribe_streaming(audio_data, sample_rate, config, verbose)
    result.transcript = transcript
    result.stt_latency = stt_latency
    print(f"        Transcript: \"{transcript}\"")
    print(f"        Latency: {stt_latency*1000:.0f}ms")
    
    if not transcript:
        print("  ERROR: No transcript received, skipping LLM and TTS")
        return result
    
    # LLM
    print("  [2/3] LLM (Qwen streaming)...")
    response, ttft, total_time, tokens = await generate_llm_response(transcript, config, verbose)
    result.llm_response = response
    result.llm_ttft = ttft
    result.llm_total = total_time
    result.llm_tokens = tokens
    print(f"        Response: \"{response[:80]}...\"" if len(response) > 80 else f"        Response: \"{response}\"")
    print(f"        TTFT: {ttft*1000:.0f}ms | Total: {total_time*1000:.0f}ms | Tokens: {tokens}")
    
    # TTS
    print("  [3/3] TTS (Cartesia streaming)...")
    audio_output, ttfb, tts_total, audio_duration, tts_sample_rate = await synthesize_speech(response, config, verbose)
    result.tts_ttfb = ttfb
    result.tts_total = tts_total
    result.tts_audio_duration = audio_duration
    print(f"        TTFB: {ttfb*1000:.0f}ms | Total: {tts_total*1000:.0f}ms | Audio: {audio_duration:.1f}s")
    
    # Playback
    if play and audio_output:
        print("  [4/4] Playing audio...")
        play_audio(audio_output, tts_sample_rate)
    
    print(f"\n  Pipeline Total: {result.pipeline_total*1000:.0f}ms")
    
    return result


def print_summary(results: list[TimingResult]):
    """Print summary table of results"""
    print("\n")
    print("=" * 80)
    print("LATENCY TEST SUMMARY")
    print("=" * 80)
    
    # Header
    print(f"\n{'Metric':<25} ", end="")
    for i, r in enumerate(results, 1):
        print(f"{'Q'+str(i):>12}", end="")
    if len(results) > 1:
        print(f"{'Average':>12}", end="")
    print()
    print("-" * (25 + 12 * len(results) + (12 if len(results) > 1 else 0)))
    
    # Rows
    metrics = [
        ("Audio Duration (s)", [r.audio_duration for r in results]),
        ("STT Latency (ms)", [r.stt_latency * 1000 for r in results]),
        ("LLM TTFT (ms)", [r.llm_ttft * 1000 for r in results]),
        ("LLM Total (ms)", [r.llm_total * 1000 for r in results]),
        ("LLM Tokens", [r.llm_tokens for r in results]),
        ("TTS TTFB (ms)", [r.tts_ttfb * 1000 for r in results]),
        ("TTS Total (ms)", [r.tts_total * 1000 for r in results]),
        ("TTS Audio (s)", [r.tts_audio_duration for r in results]),
        ("Pipeline Total (ms)", [r.pipeline_total * 1000 for r in results]),
    ]
    
    for name, values in metrics:
        print(f"{name:<25} ", end="")
        for v in values:
            if "Tokens" in name:
                print(f"{v:>12.0f}", end="")
            elif "(s)" in name:
                print(f"{v:>12.2f}", end="")
            else:
                print(f"{v:>12.0f}", end="")
        if len(values) > 1:
            avg = sum(values) / len(values)
            if "Tokens" in name:
                print(f"{avg:>12.0f}", end="")
            elif "(s)" in name:
                print(f"{avg:>12.2f}", end="")
            else:
                print(f"{avg:>12.0f}", end="")
        print()
    
    # Key insights
    print("\n" + "=" * 80)
    print("KEY INSIGHTS")
    print("=" * 80)
    
    avg_stt = sum(r.stt_latency for r in results) / len(results) * 1000
    avg_llm_ttft = sum(r.llm_ttft for r in results) / len(results) * 1000
    avg_pipeline = sum(r.pipeline_total for r in results) / len(results) * 1000
    
    print(f"\n  Perceived Response Latency (user stops speaking to audio starts):")
    print(f"    STT Latency + LLM TTFT + TTS TTFB = {avg_stt:.0f} + {avg_llm_ttft:.0f} + {sum(r.tts_ttfb for r in results)/len(results)*1000:.0f} = {avg_stt + avg_llm_ttft + sum(r.tts_ttfb for r in results)/len(results)*1000:.0f}ms")
    
    print(f"\n  Total Pipeline Time (end-to-end processing):")
    print(f"    Average: {avg_pipeline:.0f}ms")
    
    # Rating
    perceived_latency = avg_stt + avg_llm_ttft + sum(r.tts_ttfb for r in results)/len(results)*1000
    if perceived_latency < 500:
        rating = "Excellent - Near real-time"
    elif perceived_latency < 1000:
        rating = "Good - Natural conversation"
    elif perceived_latency < 2000:
        rating = "Acceptable - Noticeable delay"
    else:
        rating = "Slow - Awkward pauses"
    
    print(f"\n  Rating: {rating}")


async def main():
    parser = argparse.ArgumentParser(description="Terminal-based latency test for voice pipeline")
    parser.add_argument("--no-playback", action="store_true", help="Skip audio playback")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed API responses")
    args = parser.parse_args()
    
    print("=" * 80)
    print("TERMINAL LATENCY TEST")
    print("=" * 80)
    print(f"Environment: {os.environ.get('ENV', 'DEV')}")
    
    # Load config
    config = get_config()
    
    print(f"\nConfiguration:")
    print(f"  STT: Deepgram {config.DEEPGRAM_STT_MODEL} @ {config.DEEPGRAM_STT_SAMPLE_RATE}Hz")
    print(f"  LLM: {config.LLM_PROVIDER} {config.QWEN_MODEL}")
    print(f"  TTS: Cartesia {config.CARTESIA_TTS_MODEL} @ {config.CARTESIA_TTS_SAMPLE_RATE}Hz")
    
    # Find test audio files
    test_audio_dir = Path(__file__).parent / "test_audio"
    
    audio_files = []
    for name in ["question1.WAV", "question1.wav", "question2.WAV", "question2.wav"]:
        path = test_audio_dir / name
        if path.exists() and path.name.lower() not in [f.name.lower() for f in audio_files]:
            audio_files.append(path)
    
    if not audio_files:
        print(f"\nERROR: No test audio files found in {test_audio_dir}")
        print("Expected: question1.WAV, question2.WAV")
        sys.exit(1)
    
    print(f"\nTest audio files:")
    for f in audio_files:
        print(f"  - {f.name}")
    
    # Run tests
    results = []
    for audio_file in sorted(audio_files):
        result = await run_single_test(
            str(audio_file), 
            config, 
            verbose=args.verbose,
            play=not args.no_playback
        )
        results.append(result)
    
    # Print summary
    print_summary(results)


if __name__ == "__main__":
    asyncio.run(main())
