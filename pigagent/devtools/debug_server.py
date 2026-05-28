"""Local test server: serves the test page, generates LiveKit tokens, collects browser logs."""
import json, os, sys, http.server, urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

from livekit import api

HTML_PATH = os.path.join(os.path.dirname(__file__), 'test_room.html')
CHAT_PATH = os.path.join(os.path.dirname(__file__), 'test_chat.html')
LOG_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'logs', 'pigagent', 'browser.log')
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

def _read_html(path=HTML_PATH):
    with open(path, encoding='utf-8') as f:
        return f.read()

def _write_log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{ts} | {msg}\n"
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line)
    print(f"[Browser] {msg}")


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._serve_html()
        elif path == '/chat':
            self._serve_chat()
        elif path == '/token':
            self._serve_token(None)
        else:
            super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        content_len = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(content_len)) if content_len > 0 else {}
        if path == '/token':
            self._serve_token(body)
        elif path == '/token-direct':
            self._serve_token_direct(body)
        elif path == '/dispatch':
            self._serve_dispatch(body)
        elif path == '/log':
            self._serve_log(body)
        else:
            self.send_error(404)

    def _serve_html(self):
        self._serve_file(HTML_PATH)

    def _serve_chat(self):
        self._serve_file(CHAT_PATH)

    def _serve_file(self, path):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(_read_html(path).encode())

    def _serve_token(self, body):
        body = body or {}
        room = body.get('room', 'test-room')
        identity = body.get('identity', 'web-user')
        token = api.AccessToken(
            api_key=os.getenv('LIVEKIT_API_KEY'),
            api_secret=os.getenv('LIVEKIT_API_SECRET'),
        ).with_identity(identity).with_name(identity).with_grants(
            api.VideoGrants(room_join=True, room=room)
        ).to_jwt()
        resp = {'token': token, 'url': os.getenv('LIVEKIT_URL'), 'room': room}
        self._json(200, resp)

    def _serve_token_direct(self, body):
        """Generate token using credentials submitted from the browser."""
        key = body.get('key', os.getenv('LIVEKIT_API_KEY', ''))
        secret = body.get('secret', os.getenv('LIVEKIT_API_SECRET', ''))
        room = body.get('room', 'test-room')
        identity = body.get('identity', 'web-user')
        url = body.get('url', os.getenv('LIVEKIT_URL', ''))
        if not key or not secret or not url:
            self._json(400, {'error': 'Missing url, key, or secret'})
            return
        token = api.AccessToken(
            api_key=key, api_secret=secret,
        ).with_identity(identity).with_name(identity).with_grants(
            api.VideoGrants(room_join=True, room=room)
        ).to_jwt()
        self._json(200, {'token': token, 'url': url})

    def _serve_dispatch(self, body):
        """Explicitly dispatch pigugu-agent to the room."""
        room = body.get('room', 'test-room')
        import asyncio
        async def _dispatch():
            lkapi = api.LiveKitAPI(
                os.getenv('LIVEKIT_URL', ''),
                api_key=os.getenv('LIVEKIT_API_KEY', ''),
                api_secret=os.getenv('LIVEKIT_API_SECRET', ''),
            )
            try:
                await lkapi.agent_dispatch.create_dispatch(
                    api.CreateAgentDispatchRequest(room=room, agent_name='pigugu-agent')
                )
            finally:
                await lkapi.aclose()
        try:
            asyncio.run(_dispatch())
            self._json(200, {'ok': True, 'room': room})
        except Exception as e:
            self._json(500, {'ok': False, 'error': str(e)})

    def _serve_log(self, body):
        level = body.get('level', 'INFO')
        msg = body.get('msg', '')
        _write_log(f"[{level}] {msg}")
        self._json(200, {'ok': True})

    def _json(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # suppress default logging


if __name__ == '__main__':
    port = 9000
    print(f'Test server: http://localhost:{port}')
    print(f'Browser logs: {LOG_PATH}')
    http.server.HTTPServer(('0.0.0.0', port), Handler).serve_forever()
