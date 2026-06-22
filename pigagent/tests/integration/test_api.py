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

        from tests.integration.test_pigagent import _make_test_prompt_store
        prompt_store = _make_test_prompt_store()
        game_modes = GameModeRegistry.build_cache()

        agent = PigAgent(
            "int-test",
            ctx=None,
            redis=MagicMock(),
            pg_pool=MagicMock(),
            model="qwen3.6-flash",
            prompt_store=prompt_store,
            game_modes=game_modes,
            tools=[],
            tool_handlers={},
            temperature=0.6,
            max_tokens=200,
            max_iterations=3,
        )

        import api.roast as roast_module
        original_create = roast_module.create_pig_agent
        async def _mock_create(uid): return agent
        roast_module.create_pig_agent = _mock_create
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
            roast_module.create_pig_agent = original_create

    def test_invalid_mode_returns_400(self):
        from api.server import create_app
        from unittest.mock import MagicMock
        import api.roast as roast_module

        app = create_app()
        client = TestClient(app)

        # Mock pig_agent & game_modes so it doesn't try to init real PG
        pig = MagicMock()
        original_create = roast_module.create_pig_agent
        async def _mock_pig(uid): return pig
        roast_module.create_pig_agent = _mock_pig
        original_game_modes = roast_module.get_game_modes
        roast_module.get_game_modes = lambda: {}
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
            roast_module.create_pig_agent = original_create
            roast_module.get_game_modes = original_game_modes

    def test_missing_fields_returns_422(self):
        from api.server import create_app

        app = create_app()
        client = TestClient(app)

        response = client.post("/roast/start", json={
            "user_id": "test",
        })

        assert response.status_code == 422
