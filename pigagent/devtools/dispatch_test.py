"""Test if agent dispatch works from Python client."""
import asyncio, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
from livekit import api, rtc

async def main():
    token = api.AccessToken(
        api_key=os.getenv('LIVEKIT_API_KEY'),
        api_secret=os.getenv('LIVEKIT_API_SECRET'),
    ).with_identity('py-check').with_grants(
        api.VideoGrants(room_join=True, room='debug-room')
    ).to_jwt()

    room = rtc.Room()
    def on_p(p):
        print(f'JOIN: {p.identity}')
    room.on('participant_connected', on_p)

    print(f'[{time.strftime("%H:%M:%S")}] Connecting to debug-room...')
    await room.connect(os.environ['LIVEKIT_URL'], token)
    print(f'[{time.strftime("%H:%M:%S")}] Connected.')
    for _ in range(10):
        await asyncio.sleep(1)
        agents = [p.identity for p in room.remote_participants.values() if p.identity.startswith('agent')]
        if agents:
            print(f'AGENT IN ROOM: {agents}')
            break
    else:
        print('NO AGENT after 10s')
    await room.disconnect()

asyncio.run(main())
