"""Unit tests for LiveKit token endpoint."""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestLiveKitTokenEndpoint:
    def test_missing_credentials_returns_500(self):
        from api.livekit_token import router

        app = FastAPI()
        app.include_router(router)

        # Ensure no API keys are set
        with patch.dict(os.environ, {}, clear=True):
            client = TestClient(app)
            response = client.get("/livekit/token?user_id=u1&room_name=test-room")

        assert response.status_code == 500
        assert "not configured" in response.json()["detail"]

    def test_valid_token_returned_when_credentials_set(self):
        from api.livekit_token import router

        app = FastAPI()
        app.include_router(router)

        with patch.dict(os.environ, {
            "LIVEKIT_API_KEY": "test-api-key",
            "LIVEKIT_API_SECRET": "test-api-secret",
        }), patch("api.livekit_token.get_config") as mock_cfg:
            mock_cfg.return_value = MagicMock()
            mock_cfg.return_value.LIVEKIT_URL = "wss://test.livekit.cloud"

            client = TestClient(app)
            response = client.get("/livekit/token?user_id=alice&room_name=my-room")

        assert response.status_code == 200
        data = response.json()
        assert "token" in data
        assert data["url"] == "wss://test.livekit.cloud"
        # JWT should have 3 parts (header.payload.signature)
        assert len(data["token"].split(".")) == 3

    def test_default_room_name(self):
        from api.livekit_token import router

        app = FastAPI()
        app.include_router(router)

        with patch.dict(os.environ, {
            "LIVEKIT_API_KEY": "test-api-key",
            "LIVEKIT_API_SECRET": "test-api-secret",
        }), patch("api.livekit_token.get_config") as mock_cfg:
            mock_cfg.return_value = MagicMock()
            mock_cfg.return_value.LIVEKIT_URL = "wss://lk.example.com"

            client = TestClient(app)
            # Omit room_name — should default to "roast-room"
            response = client.get("/livekit/token?user_id=bob")

        assert response.status_code == 200
        data = response.json()
        assert data["url"] == "wss://lk.example.com"
        assert len(data["token"].split(".")) == 3

    def test_token_generation_exception_returns_500(self):
        from api.livekit_token import router

        app = FastAPI()
        app.include_router(router)

        with patch.dict(os.environ, {
            "LIVEKIT_API_KEY": "bad-key",
            "LIVEKIT_API_SECRET": "bad-secret",
        }), patch("api.livekit_token.api.AccessToken") as mock_token_cls:
            mock_token_cls.side_effect = ValueError("Invalid key format")

            client = TestClient(app)
            response = client.get("/livekit/token?user_id=u1")

        assert response.status_code == 500

    def test_identity_prefixed_with_app(self):
        """The token identity should be 'app-{user_id}' to distinguish from hardware."""
        from api.livekit_token import router

        app = FastAPI()
        app.include_router(router)

        with patch.dict(os.environ, {
            "LIVEKIT_API_KEY": "tkey",
            "LIVEKIT_API_SECRET": "tsec",
        }):
            # We verify identity via patching AccessToken
            with patch("api.livekit_token.api.AccessToken") as mock_token_cls:
                mock_token = MagicMock()
                mock_token.to_jwt.return_value = "h.p.s"
                mock_token_cls.return_value = mock_token

                client = TestClient(app)
                client.get("/livekit/token?user_id=charlie&room_name=r1")

                # Check that with_identity was called with app- prefix
                mock_token.with_identity.assert_called_once_with("app-charlie")
