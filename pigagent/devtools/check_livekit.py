"""Check LiveKit project: list rooms and agents."""
import os, sys, json, urllib.request, time, base64
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

key = os.getenv('LIVEKIT_API_KEY')
secret = os.getenv('LIVEKIT_API_SECRET')
host = 'shrump-test-jbnvclwi.livekit.cloud'

# Try to list rooms via LiveKit Server API
auth = base64.b64encode(f'{key}:{secret}'.encode()).decode()
headers = {'Authorization': f'Basic {auth}', 'Content-Type': 'application/json'}

def api(path, data=None):
    method = 'POST' if data else 'GET'
    url = f'https://{host}/twirp/livekit.RoomService/{path}'
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        return {'error': str(e)}

print('=== List Rooms ===')
print(json.dumps(api('ListRooms'), indent=2))

print('\n=== Create Room ===')
print(json.dumps(api('CreateRoom', {'name': 'api-test-room'}), indent=2))
