#!/usr/bin/env python3
"""
Check LiveKit Token Information and Expiration

Usage:
    python check_token.py YOUR_TOKEN_HERE
"""

import sys
import jwt
from datetime import datetime

def check_token(token_string):
    """Decode and display token information"""
    try:
        # Decode without verification (just to read the payload)
        decoded = jwt.decode(token_string, options={"verify_signature": False})
        
        print("\n" + "="*70)
        print("🔍 Token Information")
        print("="*70)
        
        # Extract common fields
        if 'sub' in decoded:
            print(f"User Identity: {decoded['sub']}")
        
        if 'name' in decoded:
            print(f"User Name: {decoded['name']}")
        
        if 'video' in decoded:
            video_grants = decoded['video']
            if 'room' in video_grants:
                print(f"Room: {video_grants['room']}")
            if 'roomJoin' in video_grants:
                print(f"Can Join Room: {video_grants['roomJoin']}")
            if 'canPublish' in video_grants:
                print(f"Can Publish: {video_grants['canPublish']}")
            if 'canSubscribe' in video_grants:
                print(f"Can Subscribe: {video_grants['canSubscribe']}")
        
        print("="*70)
        
        # Check expiration
        if 'exp' in decoded:
            exp_timestamp = decoded['exp']
            exp_datetime = datetime.fromtimestamp(exp_timestamp)
            now = datetime.now()
            
            print(f"\n⏰ Expiration Information:")
            print(f"Expires at: {exp_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Current time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
            
            if now < exp_datetime:
                time_left = exp_datetime - now
                hours_left = time_left.total_seconds() / 3600
                days_left = time_left.days
                
                if days_left > 0:
                    print(f"✅ Token is VALID - {days_left} days, {int(hours_left % 24)} hours remaining")
                else:
                    print(f"✅ Token is VALID - {int(hours_left)} hours remaining")
            else:
                print("❌ Token is EXPIRED")
        else:
            print("\n⚠️ No expiration found (token might be invalid)")
        
        print("="*70 + "\n")
        
        # Print full decoded payload for debugging
        print("📋 Full Token Payload:")
        import json
        print(json.dumps(decoded, indent=2))
        print()
        
    except jwt.DecodeError:
        print("❌ Error: Invalid token format")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False
    
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_token.py YOUR_TOKEN_HERE")
        print("\nExample:")
        print("  python check_token.py eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...")
        sys.exit(1)
    
    token = sys.argv[1]
    check_token(token)

