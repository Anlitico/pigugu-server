"""Unit tests for the roast WebSocket endpoint."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Token validation ─────────────────────────────────────────────────────────


class TestValidateToken:
    def test_valid_user_prefix(self):
        from api.roast_ws import _validate_token
        result = asyncio.run(_validate_token("user_alice123"))
        assert result == "alice123"

    def test_empty_token(self):
        from api.roast_ws import _validate_token
        result = asyncio.run(_validate_token(""))
        assert result is None

    def test_random_string(self):
        from api.roast_ws import _validate_token
        result = asyncio.run(_validate_token("not_a_user_token"))
        assert result is None


# ── Active roast lookup ──────────────────────────────────────────────────────


class TestLookupActiveRoast:
    def test_returns_none_when_no_roast(self):
        from api.roast_ws import _lookup_active_roast

        async def _run():
            redis = AsyncMock()
            # RoastState is imported inside _lookup_active_roast as
            #   from roast.state import RoastState
            # so we patch the source module.
            with patch("roast.state.RoastState") as mock_state:
                mock_state._load_active = AsyncMock(return_value=None)
                result = await _lookup_active_roast("u1", redis)
                assert result is None

        asyncio.run(_run())

    def test_returns_roast_when_active(self):
        from api.roast_ws import _lookup_active_roast

        async def _run():
            redis = AsyncMock()
            mock_state_obj = MagicMock()
            mock_state_obj.roast_instance_id = "riid-123"
            mock_state_obj.roast_id = "roast-456"
            mock_state_obj.mode = MagicMock()
            mock_state_obj.mode.__str__ = MagicMock(return_value="roast_together")
            mock_state_obj.phase = MagicMock()
            mock_state_obj.phase.value = "active"

            with patch("roast.state.RoastState") as mock_state_cls:
                mock_state_cls._load_active = AsyncMock(return_value=mock_state_obj)
                result = await _lookup_active_roast("u1", redis)
                assert result is not None
                assert result["roast_instance_id"] == "riid-123"
                assert result["roast_id"] == "roast-456"
                assert result["mode"] == "roast_together"

        asyncio.run(_run())

    def test_returns_none_when_phase_not_active(self):
        from api.roast_ws import _lookup_active_roast

        async def _run():
            redis = AsyncMock()
            mock_state_obj = MagicMock()
            mock_state_obj.phase = MagicMock()
            mock_state_obj.phase.value = "closed"

            with patch("roast.state.RoastState") as mock_state_cls:
                mock_state_cls._load_active = AsyncMock(return_value=mock_state_obj)
                result = await _lookup_active_roast("u1", redis)
                assert result is None

        asyncio.run(_run())


# ── _handle_start_roast ──────────────────────────────────────────────────────


class TestHandleStartRoast:
    def test_validates_required_fields(self):
        from api.roast_ws import _handle_start_roast

        async def _run():
            ws = AsyncMock()
            agent = MagicMock()
            await _handle_start_roast(ws, agent, "u1", {
                "type": "start_roast",
                # Missing roast_id, mode_id, prompt
            })
            # Should send an error
            call_args = ws.send_json.call_args[0][0]
            assert call_args["type"] == "error"
            assert call_args["code"] == "INVALID_PARAMS"

        asyncio.run(_run())

    def test_streams_agent_chunks_and_final(self):
        from api.roast_ws import _handle_start_roast

        async def _run():
            ws = AsyncMock()
            agent = MagicMock()

            async def _start_roast(**kwargs):
                yield "Opening line"
                yield " continues here"

            agent.start_roast = _start_roast

            await _handle_start_roast(ws, agent, "u1", {
                "type": "start_roast",
                "roast_id": "r1",
                "mode_id": "poison_opinion",
                "prompt": "test prompt",
                "persona_id": 1,
            })

            # Collect all send_json calls
            calls = [c[0][0] for c in ws.send_json.call_args_list]

            # Should have: chunk1, chunk2, final, state_change
            assert any(c["type"] == "agent_response" and "Opening line" in c["text"] for c in calls)
            assert any(c["type"] == "agent_response" and "continues here" in c["text"] for c in calls)
            assert any(c["type"] == "agent_response" and c["final"] for c in calls)
            assert any(c["type"] == "state_change" and c["state"] == "listening" for c in calls)

        asyncio.run(_run())

    def test_error_handling_on_start_failure(self):
        from api.roast_ws import _handle_start_roast

        async def _run():
            ws = AsyncMock()
            agent = MagicMock()

            async def _failing(**kwargs):
                raise RuntimeError("LLM connection lost")
                yield  # unreachable

            agent.start_roast = _failing

            await _handle_start_roast(ws, agent, "u1", {
                "type": "start_roast",
                "roast_id": "r1",
                "mode_id": "debate",
                "prompt": "test",
            })

            error_call = ws.send_json.call_args_list[-1][0][0]
            assert error_call["type"] == "error"
            assert error_call["code"] == "ROAST_START_FAILED"

        asyncio.run(_run())


# ── Router registration ──────────────────────────────────────────────────────


class TestRouterRegistration:
    def test_ws_route_is_mounted(self):
        from api.server import create_app
        app = create_app()
        route_paths = [getattr(r, "path", "") for r in app.routes]
        assert "/roast/ws" in route_paths

    def test_livekit_token_route_is_mounted(self):
        from api.server import create_app
        app = create_app()
        route_paths = [getattr(r, "path", "") for r in app.routes]
        assert "/livekit/token" in route_paths


# ── WebSocket auth flow ──────────────────────────────────────────────────────


class TestWebSocketAuthFlow:
    def test_auth_failure_closes_with_4001(self):
        """Connecting without a valid token should fail with code 4001."""
        from api.roast_ws import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        client = TestClient(app)
        with client.websocket_connect("/roast/ws?token=bad_token") as ws:
            # First message should be an auth error
            data = ws.receive_json()
            assert data["type"] == "error"
            assert data["code"] == "AUTH_FAILED"
            # Connection should close
            try:
                ws.receive_json()
                pytest.fail("Should have disconnected")
            except Exception:
                pass  # Expected — connection closed

    def test_valid_token_gets_connected_event(self):
        """A valid token should yield a connected event, then stay open."""
        from api.roast_ws import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        # Override Redis and PigAgent to avoid real dependencies
        with patch("api.roast_ws.get_redis", return_value=AsyncMock()), \
             patch("api.roast_ws.get_pig_agent", return_value=MagicMock()), \
             patch("api.roast_ws._lookup_active_roast", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = None

            client = TestClient(app)
            with client.websocket_connect("/roast/ws?token=user_test123") as ws:
                data = ws.receive_json()
                assert data["type"] == "connected"
                assert data["user_id"] == "test123"
                # Send ping — should get pong
                ws.send_json({"type": "ping"})
                pong = ws.receive_json()
                assert pong["type"] == "pong"

    def test_unknown_message_type_returns_error(self):
        from api.roast_ws import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        with patch("api.roast_ws.get_redis", return_value=AsyncMock()), \
             patch("api.roast_ws.get_pig_agent", return_value=MagicMock()), \
             patch("api.roast_ws._lookup_active_roast", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = None

            client = TestClient(app)
            with client.websocket_connect("/roast/ws?token=user_u1") as ws:
                ws.receive_json()  # consume connected event
                ws.send_json({"type": "garbage"})
                err = ws.receive_json()
                assert err["type"] == "error"
                assert err["code"] == "UNKNOWN_MESSAGE_TYPE"


# ── Message routing ──────────────────────────────────────────────────────────


class TestStartRoastIntegration:
    def test_start_roast_message_calls_agent(self):
        """Sending start_roast should trigger agent.start_roast and stream back."""
        from api.roast_ws import router
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        agent = MagicMock()

        async def _start_roast(**kwargs):
            yield "Welcome to the roast!"
            yield " Let's begin."

        agent.start_roast = _start_roast

        with patch("api.roast_ws.get_redis", return_value=AsyncMock()), \
             patch("api.roast_ws.get_pig_agent", return_value=agent), \
             patch("api.roast_ws._lookup_active_roast", new_callable=AsyncMock) as mock_lookup:
            mock_lookup.return_value = None

            client = TestClient(app)
            with client.websocket_connect("/roast/ws?token=user_u1") as ws:
                ws.receive_json()  # connected

                ws.send_json({
                    "type": "start_roast",
                    "roast_id": "r1",
                    "mode_id": "poison_opinion",
                    "prompt": "Trump posted something",
                    "persona_id": 1,
                })

                # Collect all messages after start_roast
                messages = []
                while True:
                    try:
                        msg = ws.receive_json()
                        messages.append(msg)
                        if msg.get("final"):
                            break
                    except Exception:
                        break

                texts = [m.get("text", "") for m in messages if m["type"] == "agent_response"]
                combined = "".join(texts)
                assert "Welcome to the roast!" in combined
