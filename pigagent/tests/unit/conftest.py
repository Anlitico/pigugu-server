# tests/unit/conftest.py
"""Shared fixtures for LLM unit tests."""

import os

# Set dummy API keys before any imports to allow provider instantiation
os.environ.setdefault("DASHSCOPE_US_API_KEY", "sk-test-dummy")
os.environ.setdefault("DASHSCOPE_API_KEY", "sk-test-dummy-cn")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-dummy")
os.environ.setdefault("ARK_API_KEY", "sk-test-dummy")
