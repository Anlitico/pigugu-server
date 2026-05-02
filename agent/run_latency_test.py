#!/usr/bin/env python3
"""
All-in-One Latency Test Runner

This script handles everything in a single command:
1. Starts the agent in the background
2. Waits for agent to initialize
3. Runs the latency test
4. Shows results
5. Cleans up (stops agent)

Usage:
    uv run python run_latency_test.py

This is designed for single-terminal environments like Alibaba ECS web workbench.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def check_prerequisites():
    """Check that everything is ready to run"""
    errors = []
    
    # Check test audio files (support both WAV and M4A)
    test_audio_dir = Path(__file__).parent / "test_audio"
    
    if not test_audio_dir.exists():
        errors.append(f"Test audio directory not found: {test_audio_dir}")
        errors.append("  Create it with: mkdir -p test_audio")
    else:
        # Check for question1 (wav or m4a)
        q1_wav = test_audio_dir / "question1.wav"
        q1_m4a = test_audio_dir / "question1.m4a"
        if not q1_wav.exists() and not q1_m4a.exists():
            errors.append("Missing: question1.wav or question1.m4a")
            errors.append("  Record 'Hello, good morning' (~1-2 seconds)")
        
        # Check for question2 (wav or m4a)
        q2_wav = test_audio_dir / "question2.wav"
        q2_m4a = test_audio_dir / "question2.m4a"
        if not q2_wav.exists() and not q2_m4a.exists():
            errors.append("Missing: question2.wav or question2.m4a")
            errors.append("  Record 'What do you think of Japan?' (~2-3 seconds)")
    
    # Check API keys
    if not os.getenv("LIVEKIT_API_KEY"):
        errors.append("LIVEKIT_API_KEY not set in .env")
    if not os.getenv("LIVEKIT_API_SECRET"):
        errors.append("LIVEKIT_API_SECRET not set in .env")
    
    if errors:
        print("=" * 60)
        print("PREREQUISITES CHECK FAILED")
        print("=" * 60)
        for error in errors:
            print(f"  {error}")
        print("\nAudio format: WAV recommended (no ffmpeg needed), M4A also supported")
        return False
    
    return True


def start_agent():
    """Start the agent in background"""
    print("Starting agent in background...")
    
    # Set environment for CLOUD mode
    env = os.environ.copy()
    env["ENV"] = "CLOUD"
    
    # Start agent as subprocess
    agent_script = Path(__file__).parent / "main.py"
    
    # Use subprocess to run agent
    process = subprocess.Popen(
        [sys.executable, str(agent_script), "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    
    print(f"Agent started with PID: {process.pid}")
    return process


def wait_for_agent(process, timeout=30):
    """Wait for agent to initialize"""
    print(f"Waiting up to {timeout}s for agent to initialize...")
    
    start_time = time.time()
    ready_indicators = [
        "Waiting for participants",
        "Connected to LiveKit",
        "Agent ready",
        "Starting worker",
    ]
    
    while time.time() - start_time < timeout:
        if process.poll() is not None:
            # Process exited
            print("ERROR: Agent process exited unexpectedly")
            stdout, _ = process.communicate()
            print(stdout)
            return False
        
        # Check stdout for ready indicators
        try:
            line = process.stdout.readline()
            if line:
                # Print agent output for debugging
                print(f"  [Agent] {line.strip()}")
                
                # Check for ready indicators
                for indicator in ready_indicators:
                    if indicator.lower() in line.lower():
                        print(f"\nAgent is ready! (detected: '{indicator}')")
                        # Give more time for agent to register with LiveKit Cloud
                        print("Waiting 10 seconds for agent to register with LiveKit Cloud...")
                        time.sleep(10)
                        return True
        except:
            pass
        
        time.sleep(0.1)
    
    print(f"Timeout waiting for agent ({timeout}s)")
    print("Assuming agent is ready and continuing...")
    return True


def stop_agent(process):
    """Stop the agent process"""
    print("\nStopping agent...")
    
    if process.poll() is None:
        # Process is still running
        try:
            # Try graceful shutdown first
            process.terminate()
            try:
                process.wait(timeout=5)
                print("Agent stopped gracefully")
            except subprocess.TimeoutExpired:
                # Force kill
                process.kill()
                process.wait()
                print("Agent force killed")
        except Exception as e:
            print(f"Error stopping agent: {e}")
    else:
        print("Agent already stopped")


async def run_test():
    """Run the latency test"""
    # Import and run the test
    from test_latency_us import main as test_main
    await test_main()


def main():
    """Main entry point"""
    print("=" * 70)
    print("ALL-IN-ONE LATENCY TEST RUNNER")
    print("=" * 70)
    print()
    
    # Check prerequisites
    if not check_prerequisites():
        return 1
    
    agent_process = None
    
    try:
        # Start agent
        agent_process = start_agent()
        
        # Wait for agent to be ready
        if not wait_for_agent(agent_process):
            print("Failed to start agent")
            return 1
        
        print("\n" + "=" * 70)
        print("RUNNING LATENCY TEST")
        print("=" * 70)
        
        # Run the test
        asyncio.run(run_test())
        
        return 0
        
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        return 1
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # Always clean up
        if agent_process:
            stop_agent(agent_process)


if __name__ == "__main__":
    sys.exit(main())
