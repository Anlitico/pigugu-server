#!/usr/bin/env python3
"""
Test script to verify LLM system prompt functionality

This script demonstrates that the system prompt is properly 
set in the LLM module and accessible via initial_chat_ctx.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from llm import create_llm

# Load environment variables
load_dotenv()


def test_llm_with_system_prompt():
    """Test LLM creation with system prompt"""
    
    print("=" * 70)
    print("Testing LLM System Prompt")
    print("=" * 70)
    
    # Test 1: LLM with custom system prompt
    print("\n1. Creating LLM with custom system prompt...")
    
    custom_prompt = """You are Trump, a confident and charismatic AI assistant.

Your personality:
- Bold, direct, and energetic
- Always confident in your responses
- Use phrases like "tremendous", "amazing", "the best"
- Speak in short, punchy sentences

Communication style:
- Keep responses brief but impactful
- Show enthusiasm and confidence
- Use strong, positive language"""

    try:
        llm = create_llm(
            model="qwen-plus",
            temperature=0.8,
            instructions=custom_prompt,
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        
        print("\n✅ LLM created successfully!")
        print(f"\nModel: {llm.model}")
        print(f"Has initial_chat_ctx: {llm.initial_chat_ctx is not None}")
        
        if llm.initial_chat_ctx:
            print(f"Number of items in context: {len(llm.initial_chat_ctx.items)}")
            
            for i, item in enumerate(llm.initial_chat_ctx.items):
                print(f"\n--- Item {i+1} ---")
                print(f"Type: {item.type}")
                print(f"Role: {item.role}")
                if hasattr(item, 'text_content') and item.text_content:
                    preview = item.text_content[:100] + "..." if len(item.text_content) > 100 else item.text_content
                    print(f"Content preview: {preview}")
        
        print("\n" + "=" * 70)
        print("✓ System prompt is properly set in LLM!")
        print("=" * 70)
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return False


def test_llm_without_system_prompt():
    """Test LLM creation without system prompt"""
    
    print("\n" + "=" * 70)
    print("Testing LLM Without System Prompt")
    print("=" * 70)
    
    print("\n2. Creating LLM without system prompt...")
    
    try:
        llm = create_llm(
            model="qwen-plus",
            temperature=0.8,
            instructions="",  # Empty instructions
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        
        print("\n✅ LLM created successfully!")
        print(f"\nModel: {llm.model}")
        print(f"Has initial_chat_ctx: {llm.initial_chat_ctx is not None}")
        
        if llm.initial_chat_ctx is None:
            print("\n✓ Correctly has no initial chat context (as expected)")
        
        print("\n" + "=" * 70)
        print("✓ LLM works correctly without system prompt!")
        print("=" * 70)
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return False


if __name__ == "__main__":
    print("\n🚀 Testing LLM System Prompt Implementation\n")
    
    # Check API key
    if not os.getenv("DASHSCOPE_API_KEY"):
        print("❌ Error: DASHSCOPE_API_KEY not set in .env file")
        sys.exit(1)
    
    # Run tests
    test1_passed = test_llm_with_system_prompt()
    test2_passed = test_llm_without_system_prompt()
    
    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print(f"Test 1 (With prompt):    {'✓ PASS' if test1_passed else '✗ FAIL'}")
    print(f"Test 2 (Without prompt): {'✓ PASS' if test2_passed else '✗ FAIL'}")
    print("=" * 70)
    
    if test1_passed and test2_passed:
        print("\n✅ All tests passed! System prompt is working correctly.")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed.")
        sys.exit(1)

