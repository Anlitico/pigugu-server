# tests/integration/test_api.py
"""Integration tests for HTTP API — needs DASHSCOPE_US_API_KEY in .env."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=True)


@pytest.mark.integration
class TestRoastStartEndpoint:
    def test_returns_sse_stream(self):
        if not os.getenv("DASHSCOPE_US_API_KEY"):
            pytest.skip("DASHSCOPE_US_API_KEY not set")

        from api.server import create_app
        from agent import PigAgent
        from system_prompts import PersonaRegistry
        from roast import GameModeRegistry

        PersonaRegistry.register_defaults()
        GameModeRegistry.register_defaults()

        prompts = PersonaRegistry.build_prompt_cache()
        game_modes = GameModeRegistry.build_cache()

        agent = PigAgent(
            ctx=None,
            redis=MagicMock(),
            pg_pool=MagicMock(),
            model="qwen3.6-flash",
            prompts=prompts,
            game_modes=game_modes,
            tools=[],
            tool_handlers={},
            temperature=0.6,
            max_tokens=200,
            max_iterations=3,
        )

        import api.roast as roast_module
        original = roast_module.get_pig_agent
        roast_module.get_pig_agent = lambda: agent
        roast_module.router  # trigger import

        try:
            app = create_app()
            client = TestClient(app)

            response = client.post("/roast/start", json={
                "user_id": "int-test",
                "persona_id": 1,
                "roast_id": "test-roast-1",
                "mode_id": "roast_together",
                "prompt": "Trump tweeted about AI today. Share your thoughts.",
            })

            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            body = response.text
            assert 'data:' in body
            assert '"done"' in body
        finally:
            roast_module.get_pig_agent = original

    def test_invalid_mode_returns_400(self):
        from api.server import create_app
        from unittest.mock import MagicMock
        import api.roast as roast_module

        app = create_app()
        client = TestClient(app)

        # Mock get_pig_agent so it doesn't try to init real PG
        pig = MagicMock()
        pig._game_modes = {}
        original = roast_module.get_pig_agent
        roast_module.get_pig_agent = lambda: pig
        try:

            response = client.post("/roast/start", json={
                "user_id": "test",
                "persona_id": 1,
                "roast_id": "r1",
                "mode_id": "nonexistent",
                "prompt": "test",
            })

            assert response.status_code == 400
        finally:
            roast_module.get_pig_agent = original

    def test_missing_fields_returns_422(self):
        from api.server import create_app

        app = create_app()
        client = TestClient(app)

        response = client.post("/roast/start", json={
            "user_id": "test",
        })

        assert response.status_code == 422
