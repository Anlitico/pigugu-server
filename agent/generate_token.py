#!/usr/bin/env python3
"""
Generate a LiveKit access token for testing the voice agent

Usage:
    python generate_token.py
    python generate_token.py --user "john-doe" --room "my-room"
"""

import os
import argparse
from dotenv import load_dotenv
from livekit import api

# Load environment variables
load_dotenv()

def generate_token(user_identity: str = "test-user", room_name: str = "test-room", valid_for_hours: int = 24):
    """Generate a LiveKit access token"""
    
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    
    if not api_key or not api_secret:
        print("❌ Error: LIVEKIT_API_KEY or LIVEKIT_API_SECRET not found in .env file")
        return None
    
    # Create token
    token = api.AccessToken(api_key, api_secret)
    
    # Set identity and grants
    token.with_identity(user_identity) \
         .with_name(user_identity.replace('-', ' ').title()) \
         .with_grants(api.VideoGrants(
             room_join=True,
             room=room_name,
             can_publish=True,
             can_subscribe=True,
         ))
    
    # Set expiration
    from datetime import timedelta
    token.with_ttl(timedelta(hours=valid_for_hours))
    
    jwt_token = token.to_jwt()
    
    # Print token info
    print("\n" + "="*70)
    print("🎫 LiveKit Access Token Generated")
    print("="*70)
    print(f"User: {user_identity}")
    print(f"Room: {room_name}")
    print(f"Valid for: {valid_for_hours} hours")
    print("="*70)
    print("\n📋 Your Access Token:")
    print(jwt_token)
    print("\n" + "="*70)
    print("✅ Copy the token above and paste it into the web client")
    print("="*70 + "\n")
    
    return jwt_token


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate LiveKit access token")
    parser.add_argument("--user", "-u", default="test-user", help="User identity (default: test-user)")
    parser.add_argument("--room", "-r", default="test-room", help="Room name (default: test-room)")
    parser.add_argument("--hours", type=int, default=24, help="Token validity in hours (default: 24)")
    
    args = parser.parse_args()
    
    generate_token(args.user, args.room, args.hours)

