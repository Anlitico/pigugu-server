#!/usr/bin/env python3
"""
Simple Token Server for Trump AI Agent
Generates LiveKit access tokens on-demand for local development

For POC use only - no authentication needed for trusted team
"""

from flask import Flask, jsonify
from flask_cors import CORS
from livekit import api
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)  # Allow web client to call from different port

@app.route('/token', methods=['GET'])
def get_token():
    """Generate a fresh LiveKit access token"""
    
    # Get credentials from environment
    api_key = os.getenv('LIVEKIT_API_KEY')
    api_secret = os.getenv('LIVEKIT_API_SECRET')
    
    if not api_key or not api_secret:
        return jsonify({'error': 'LiveKit credentials not configured'}), 500
    
    # Create token with room access
    token = api.AccessToken(api_key, api_secret)
    token.with_identity('engineer')  # Can be any identity
    token.with_name('Test Engineer')
    token.with_grants(api.VideoGrants(
        room_join=True,
        room='test-room',  # Default room name
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True
    ))
    
    return jsonify({
        'token': token.to_jwt(),
        'url': os.getenv('LIVEKIT_URL', 'ws://localhost:8002')
    })

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok', 'service': 'token-server'})

if __name__ == '__main__':
    print("=" * 70)
    print("🎫 Token Server Starting")
    print("=" * 70)
    print(f"Server: http://localhost:3000")
    print(f"Endpoint: http://localhost:3000/token")
    print(f"LiveKit API Key: {os.getenv('LIVEKIT_API_KEY', 'NOT SET')[:20]}...")
    print("=" * 70)
    
    app.run(host='0.0.0.0', port=3000, debug=False)
