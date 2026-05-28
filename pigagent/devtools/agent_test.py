"""Quick test: connect to LiveKit room, verify agent dispatch, record audio response."""
import asyncio, os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

from livekit import api, rtc

async def main():
    token = api.AccessToken(
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    ).with_identity("python-test").with_name("Test User").with_grants(
        api.VideoGrants(room_join=True, room="test-room")
    ).to_jwt()

    room = rtc.Room()
    agent_joined = False
    audio_received = False

    @room.on("participant_connected")
    def on_participant(p: rtc.RemoteParticipant):
        nonlocal agent_joined
        if p.identity.startswith("agent-"):
            agent_joined = True
            print(f"[OK] Agent dispatched: {p.identity}")

    @room.on("track_subscribed")
    def on_track(track, pub, p):
        nonlocal audio_received
        if track.kind == "audio":
            audio_received = True
            print(f"[OK] Audio from {p.identity}")

    @room.on("disconnected")
    def on_disconnected(*_):
        print("Room disconnected")

    print("Connecting to test-room...")
    await room.connect(os.environ["LIVEKIT_URL"], token)
    print(f"Connected. Waiting for agent dispatch...")

    # Wait for agent to join
    for _ in range(30):
        await asyncio.sleep(1)
        if agent_joined:
            break

    if not agent_joined:
        print("[FAIL] Agent did not join within 30s")
    else:
        print(f"[OK] Agent is in the room. Waiting for audio...")
        await asyncio.sleep(5)

    if audio_received:
        print("[OK] Test passed - agent is working!")
    else:
        print("[INFO] No audio received (agent waiting for speech)")

    await room.disconnect()

asyncio.run(main())
