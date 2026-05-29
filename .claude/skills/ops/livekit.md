# LiveKit Operations

LiveKit server: `shrump-test-jbnvclwi.livekit.cloud`

## List Rooms

```bash
kubectl run lk-rooms --rm -i --restart=Never --image=python:3.13-slim -- bash -c "
pip install -q livekit
python -c \"
import asyncio
from livekit import api
async def main():
    lkapi = api.LiveKitAPI('wss://shrump-test-jbnvclwi.livekit.cloud', api_key='$(kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_KEY}" | base64 -d)', api_secret='$(kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_SECRET}" | base64 -d)')
    rooms = await lkapi.room.list_rooms(api.ListRoomsRequest())
    for r in rooms.rooms:
        print(f'Room: {r.name}  participants: {r.num_participants}')
    await lkapi.aclose()
asyncio.run(main())
\"
"
```

## List Participants in a Room

```bash
kubectl run lk-parts --rm -i --restart=Never --image=python:3.13-slim -- bash -c "
pip install -q livekit
python -c \"
import asyncio
from livekit import api
async def main():
    lkapi = api.LiveKitAPI('wss://shrump-test-jbnvclwi.livekit.cloud', api_key='$(kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_KEY}" | base64 -d)', api_secret='$(kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_SECRET}" | base64 -d)')
    parts = await lkapi.room.list_participants(api.ListParticipantsRequest(room='<room-name>'))
    for p in parts.participants:
        print(f'{p.identity} kind={p.kind} state={p.state}')
    await lkapi.aclose()
asyncio.run(main())
\"
"
```

## Dispatch Agent to Room

```bash
kubectl run lk-dispatch --rm -i --restart=Never --image=python:3.13-slim -- bash -c "
pip install -q livekit
python -c \"
import asyncio
from livekit import api
async def main():
    lkapi = api.LiveKitAPI('wss://shrump-test-jbnvclwi.livekit.cloud', api_key='$(kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_KEY}" | base64 -d)', api_secret='$(kubectl get secret pigugu-secrets -o jsonpath="{.data.LIVEKIT_API_SECRET}" | base64 -d)')
    d = await lkapi.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(room='<room>', agent_name='pigugu-agent'))
    print(f'Dispatch: {d.id}')
    await lkapi.aclose()
asyncio.run(main())
\"
"
```
