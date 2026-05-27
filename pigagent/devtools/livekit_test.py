"""Quick LiveKit test: connect to room, send chat message, print agent response."""
import asyncio, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

from livekit import api, rtc


async def main():
    token = api.AccessToken(
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    ).with_identity("test-user").with_name("Test User").with_grants(
        api.VideoGrants(room_join=True, room="test-room")
    ).to_jwt()

    room = rtc.Room()
    print(f"Connecting to test-room...")

    @room.on("participant_connected")
    def on_participant(p: rtc.RemoteParticipant):
        print(f"[Room] {p.identity} joined")

    @room.on("data_received")
    def on_data(data: bytes):
        print(f"[Agent] {data.decode()}")

    await room.connect(os.getenv("LIVEKIT_URL"), token)
    print(f"Connected as {room.local_participant.identity}")
    print(f"Participants: {[p.identity for p in room.remote_participants.values()]}")

    # Send a text message to trigger agent response
    await room.local_participant.publish_data(
        b"Hello, what do you think about AI?",
        reliable=True,
    )
    print("Sent message, waiting for agent response...")

    # Wait a bit for agent to respond
    await asyncio.sleep(15)
    await room.disconnect()
    print("Done")


asyncio.run(main())
