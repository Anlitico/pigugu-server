# tests/integration/conftest.py
"""Fallback keys only for providers not yet configured by the user.

Real keys should be in pigagent/.env. load_dotenv() in the test file reads them.
These fallbacks prevent the pool from crashing when providers like DeepSeek
or Volcengine don't have keys yet — their tests simply skip.
"""

import os

# Only set if the user has NOT configured the provider yet.
# DASHSCOPE_US_API_KEY is NOT here — it should come from .env.
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-dummy")
os.environ.setdefault("ARK_API_KEY", "sk-test-dummy")
