"""Unit tests for agent proxy endpoints (modules/agent/router.py)."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ── Pre-import mocks for optional prod deps not installed locally ──
for _mod in ("firebase_admin", "firebase_admin.credentials", "firebase_admin.messaging"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

if "modules.push.service" not in sys.modules:
    _push = MagicMock()
    _push.init_firebase = MagicMock()
    sys.modules["modules.push.service"] = _push

from modules.agent.router import router  # noqa: E402
from core.deps import get_current_user     # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_user(user_id: str):
    u = MagicMock()
    u.id = user_id
    return u


def _make_app(*, override_user: MagicMock | None = None):
    app = FastAPI()
    app.include_router(router)
    if override_user:
        app.dependency_overrides[get_current_user] = lambda: override_user
    return app


def _mock_http_client(response_json: dict):
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = response_json
    mock_resp.raise_for_status = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    return mock_client


# ── Tests ────────────────────────────────────────────────────────────────────


class TestLiveKitTokenProxy:
    def test_returns_token_json_when_agent_up(self):
        mock_user = _mock_user("u1")
        app = _make_app(override_user=mock_user)

        with patch("modules.agent.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = _mock_http_client({"token": "jwt", "url": "wss://lk"})
            mock_client_cls.return_value = mock_client

            client = TestClient(app)
            response = client.get("/livekit/token")

        assert response.status_code == 200
        assert response.json()["token"] == "jwt"

    def test_returns_502_when_agent_unavailable(self):
        mock_user = _mock_user("u1")
        app = _make_app(override_user=mock_user)

        # Use httpx.HTTPError so it passes through the handler's except clause
        import httpx
        with patch("modules.agent.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(
                side_effect=httpx.HTTPError("Connection refused")
            )
            mock_client_cls.return_value = mock_client

            client = TestClient(app)
            response = client.get("/livekit/token")

        assert response.status_code == 502

    def test_default_room_name_passed_through(self):
        mock_user = _mock_user("u1")
        app = _make_app(override_user=mock_user)

        with patch("modules.agent.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = _mock_http_client({"token": "t", "url": "wss://x"})
            mock_client_cls.return_value = mock_client

            client = TestClient(app)
            client.get("/livekit/token")

        call_kwargs = mock_client.get.call_args
        assert call_kwargs is not None
        assert call_kwargs[1]["params"]["room_name"] == "roast-room"
        assert call_kwargs[1]["params"]["user_id"] == "u1"


class TestRoastWsProxyAuth:
    def test_invalid_jwt_token_returns_auth_error(self):
        app = _make_app()

        # decode_access_token is imported from core.security inside the WS handler
        with patch("core.security.decode_access_token",
                   side_effect=ValueError("bad token")):
            client = TestClient(app)
            with client.websocket_connect("/roast/ws?token=bad") as ws:
                data = ws.receive_json()
                assert data["type"] == "error"
                assert data["code"] == "AUTH_FAILED"

    def test_valid_jwt_accepts_connection(self):
        app = _make_app()
        mock_user = MagicMock()

        # get_user_by_id is imported inside the WS handler via
        #   from modules.auth.service import get_user_by_id
        with patch("core.security.decode_access_token",
                   return_value={"sub": "mock-uuid"}), \
             patch("modules.auth.service.get_user_by_id",
                   new_callable=AsyncMock) as mock_get_user, \
             patch("core.database.AsyncSessionLocal") as mock_session_cls, \
             patch("modules.agent.router.websockets.connect") as mock_ws:
            mock_get_user.return_value = mock_user
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)
            mock_session_cls.return_value = mock_session

            # Agent WS raises to stop the forwarder loop quickly
            mock_agent_ws = AsyncMock()
            mock_agent_ws.recv = AsyncMock(side_effect=Exception("done"))
            mock_agent_ws.send = AsyncMock()
            mock_agent_ws.__aenter__ = AsyncMock(return_value=mock_agent_ws)
            mock_agent_ws.__aexit__ = AsyncMock(return_value=None)
            mock_ws.return_value = mock_agent_ws

            client = TestClient(app)
            with client.websocket_connect(
                "/roast/ws?token=valid.jwt.token"
            ) as ws:
                # Connection accepted — no auth error raised
                pass


class TestAgentRouterRegistration:
    def test_routes_are_registered(self):
        route_paths = [getattr(r, "path", "") for r in router.routes]
        assert "/livekit/token" in route_paths
        assert "/roast/ws" in route_paths
