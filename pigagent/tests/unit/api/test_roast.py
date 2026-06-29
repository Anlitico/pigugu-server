# tests/unit/api/test_roast.py
"""Unit tests for roast API endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── RoastStartRequest ──────────────────────────────────────────────────────


class TestRoastStartRequest:
    def test_validates_required_fields(self):
        from api.roast import RoastStartRequest
        req = RoastStartRequest(
            user_id="u1",
            persona_id=1,
            roast_id="r1",
            mode_id="roast_together",
            prompt="news text",
        )
        assert req.user_id == "u1"
        assert req.prompt == "news text"

    def test_missing_field_raises(self):
        from api.roast import RoastStartRequest
        with pytest.raises(ValueError, match="persona_id"):
            RoastStartRequest(user_id="u1")  # type: ignore[reportCallIssue]


# ── _event_stream ──────────────────────────────────────────────────────────


class TestEventStream:
    def test_yields_sse_events(self, monkeypatch):
        from api.roast import _event_stream

        # Mock start_roast on the global pig_agent
        mock_agent = MagicMock()

        async def _start_roast(*args, **kwargs):
            yield "Hello roast!"
            yield " Part 2"

        mock_agent.start_roast = _start_roast

        import asyncio
        events = asyncio.run(_collect_events(_event_stream(
            mock_agent, 1, "r1", "roast_together", "news text",
        )))

        assert events[0].startswith("data: ")
        assert '"text"' in events[0]
        assert "Hello roast!" in events[0]
        assert "Part 2" in events[1]
        assert '"done"' in events[-1]

    def test_error_yields_error_event(self, monkeypatch):
        from api.roast import _event_stream

        mock_agent = MagicMock()

        async def _failing(*args, **kwargs):
            raise RuntimeError("LLM down")
            yield  # unreachable

        mock_agent.start_roast = _failing

        import asyncio
        events = asyncio.run(_collect_events(_event_stream(
            mock_agent, 1, "r1", "roast_together", "news",
        )))

        assert any('"error"' in e for e in events)


# ── POST /roast/start ──────────────────────────────────────────────────────


class TestStartRoastEndpoint:
    def test_unknown_game_mode_returns_400(self):
        from api.roast import router
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)

        with patch("api.roast.get_game_modes", return_value={}):
            client = TestClient(app)
            response = client.post("/roast/start", json={
                "user_id": "u1",
                "persona_id": 1,
                "roast_id": "r1",
                "mode_id": "unknown",
                "prompt": "test",
            })

        assert response.status_code == 400

    def test_valid_request_returns_sse(self):
        from api.roast import router
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        pig_agent = MagicMock()

        async def _start_roast(*args, **kwargs):
            yield "chunk1"
            yield "chunk2"

        pig_agent.start_roast = _start_roast

        # Mock session registry: no active agent → creates PigAgent → streams SSE
        mock_registry = MagicMock()
        mock_registry.has_active_agent = AsyncMock(return_value=False)

        app = FastAPI()
        app.include_router(router)

        with patch("roast.session_registry.registry", mock_registry), \
             patch("api.roast.create_pig_agent", return_value=pig_agent), \
             patch("api.roast.get_game_modes", return_value={"roast_together": MagicMock()}):
            client = TestClient(app)
            response = client.post("/roast/start", json={
                "user_id": "u1",
                "persona_id": 1,
                "roast_id": "r1",
                "mode_id": "roast_together",
                "prompt": "test prompt",
            })

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.text
        assert "chunk1" in body
        assert "chunk2" in body
        assert '"done"' in body

    def test_active_session_injects_and_returns_settled(self):
        from api.roast import router
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        # Mock session registry: agent IS active → inject via room → settled
        mock_registry = MagicMock()
        mock_registry.has_active_agent = AsyncMock(return_value=True)
        mock_registry.send_inject = AsyncMock()

        app = FastAPI()
        app.include_router(router)

        with patch("roast.session_registry.registry", mock_registry), \
             patch("api.roast.get_game_modes", return_value={"roast_together": MagicMock()}):
            client = TestClient(app)
            response = client.post("/roast/start", json={
                "user_id": "u1",
                "persona_id": 1,
                "roast_id": "r1",
                "mode_id": "roast_together",
                "prompt": "test prompt",
            })

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        body = response.text
        assert '"settled_in_room"' in body
        assert '"done"' in body
        mock_registry.send_inject.assert_called_once()


class TestServerCreateApp:
    def test_create_app_includes_roast_routes(self):
        from api.server import create_app
        app = create_app()
        routes = [getattr(r, "path", "") for r in app.routes]
        assert "/roast/start" in routes


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _collect_events(gen):
    events = []
    async for e in gen:
        events.append(e)
    return events
