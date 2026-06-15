"""Pytest configuration — dummy API keys to prevent import-time credential errors."""

import os

os.environ.setdefault("OPENAI_API_KEY", "pytest-dummy-key")
os.environ.setdefault("DASHSCOPE_API_KEY", "pytest-dummy-key")
os.environ.setdefault("DASHSCOPE_US_API_KEY", "pytest-dummy-key")
os.environ.setdefault("ARK_API_KEY", "pytest-dummy-key")
os.environ.setdefault("XAI_API_KEY", "pytest-dummy-key")
