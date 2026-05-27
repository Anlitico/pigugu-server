# tests/unit/conftest.py
"""Shared fixtures for LLM unit tests."""

import os

import pytest

# Set dummy API keys before any imports to allow provider instantiation
os.environ.setdefault("DASHSCOPE_US_API_KEY", "sk-test-dummy")
os.environ.setdefault("DASHSCOPE_API_KEY", "sk-test-dummy-cn")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-dummy")
os.environ.setdefault("ARK_API_KEY", "sk-test-dummy")


@pytest.fixture(scope="session")
def qwen_provider():
    from core.llm.providers.qwen import QwenProvider
    return QwenProvider(api_key="sk-test")


@pytest.fixture(scope="session")
def volcengine_provider():
    from core.llm.providers.volcengine import VolcengineProvider
    return VolcengineProvider(api_key="sk-test")
