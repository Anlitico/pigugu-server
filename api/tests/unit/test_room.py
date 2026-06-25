"""Unit tests for api/modules/device/room.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.device.room import build_room_name, _server_url


class TestBuildRoomName:
    def test_normal_uuid(self):
        name = build_room_name("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
        assert name == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_short_id(self):
        name = build_room_name("abc")
        assert name == "abc"

    def test_strips_whitespace(self):
        name = build_room_name("  a1b2c3d4-e5f6-7890-abcd-ef1234567890  ")
        assert name == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestServerUrl:
    def test_wss_to_https(self):
        assert _server_url("wss://livekit.example.com") == "https://livekit.example.com"

    def test_ws_to_http(self):
        assert _server_url("ws://livekit.example.com") == "http://livekit.example.com"

    def test_https_unchanged(self):
        assert _server_url("https://livekit.example.com") == "https://livekit.example.com"

    def test_http_unchanged(self):
        assert _server_url("http://livekit.example.com") == "http://livekit.example.com"

    def test_bare_hostname_with_scheme_fallback(self):
        # urlparse treats bare hostname as path; scheme defaults to https
        result = _server_url("livekit.example.com")
        assert result.startswith("https")
        assert "livekit.example.com" in result

    def test_with_port(self):
        assert _server_url("wss://livekit.example.com:7880") == "https://livekit.example.com:7880"


class TestEnsureRoom:
    @pytest.mark.asyncio
    async def test_creates_new_room(self):
        mock_room = MagicMock()
        mock_room.sid = "RM_test123"

        with patch("modules.device.room._lk_client") as mock_lk_client:
            mock_lk = MagicMock()
            mock_lk.room.create_room = AsyncMock(return_value=mock_room)
            mock_lk_client.return_value.__aenter__ = AsyncMock(return_value=mock_lk)
            mock_lk_client.return_value.__aexit__ = AsyncMock(return_value=None)

            from modules.device.room import ensure_room
            await ensure_room(user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

            mock_lk.room.create_room.assert_called_once()

    @pytest.mark.asyncio
    async def test_reuses_existing_room(self):
        from livekit.api import TwirpError

        with patch("modules.device.room._lk_client") as mock_lk_client:
            mock_lk = MagicMock()
            mock_lk.room.create_room = AsyncMock(
                side_effect=TwirpError(code="already_exists", msg="Room exists", status=409)
            )
            mock_lk_client.return_value.__aenter__ = AsyncMock(return_value=mock_lk)
            mock_lk_client.return_value.__aexit__ = AsyncMock(return_value=None)

            from modules.device.room import ensure_room
            await ensure_room(user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

            mock_lk.room.create_room.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_on_other_twirp_error(self):
        from livekit.api import TwirpError

        with patch("modules.device.room._lk_client") as mock_lk_client:
            mock_lk = MagicMock()
            mock_lk.room.create_room = AsyncMock(
                side_effect=TwirpError(code="internal", msg="Boom", status=500)
            )
            mock_lk_client.return_value.__aenter__ = AsyncMock(return_value=mock_lk)
            mock_lk_client.return_value.__aexit__ = AsyncMock(return_value=None)

            from modules.device.room import ensure_room
            with pytest.raises(TwirpError):
                await ensure_room(user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")


class TestCheckRoomAlive:
    @pytest.mark.asyncio
    async def test_room_alive_with_participants(self):
        """Room exists with participants → alive."""
        mock_room = MagicMock()
        mock_room.num_participants = 1

        mock_response = MagicMock()
        mock_response.rooms = [mock_room]

        with patch("modules.device.room._lk_client") as mock_lk_client:
            mock_lk = MagicMock()
            mock_lk.room.list_rooms = AsyncMock(return_value=mock_response)
            mock_lk_client.return_value.__aenter__ = AsyncMock(return_value=mock_lk)
            mock_lk_client.return_value.__aexit__ = AsyncMock(return_value=None)

            from modules.device.room import check_room_alive
            assert await check_room_alive("test") is True

    @pytest.mark.asyncio
    async def test_room_dead_empty(self):
        """Room exists with 0 participants → not alive."""
        mock_room = MagicMock()
        mock_room.num_participants = 0

        mock_response = MagicMock()
        mock_response.rooms = [mock_room]

        with patch("modules.device.room._lk_client") as mock_lk_client:
            mock_lk = MagicMock()
            mock_lk.room.list_rooms = AsyncMock(return_value=mock_response)
            mock_lk_client.return_value.__aenter__ = AsyncMock(return_value=mock_lk)
            mock_lk_client.return_value.__aexit__ = AsyncMock(return_value=None)

            from modules.device.room import check_room_alive
            assert await check_room_alive("test") is False

    @pytest.mark.asyncio
    async def test_room_not_found(self):
        """Room doesn't exist → not alive."""
        mock_response = MagicMock()
        mock_response.rooms = []

        with patch("modules.device.room._lk_client") as mock_lk_client:
            mock_lk = MagicMock()
            mock_lk.room.list_rooms = AsyncMock(return_value=mock_response)
            mock_lk_client.return_value.__aenter__ = AsyncMock(return_value=mock_lk)
            mock_lk_client.return_value.__aexit__ = AsyncMock(return_value=None)

            from modules.device.room import check_room_alive
            assert await check_room_alive("test") is False

class TestGenerateLivekitToken:
    """service.py generate_livekit_token — long-lived token for hardware."""

    @pytest.mark.asyncio
    async def test_returns_token_and_room_name(self):
        from unittest.mock import patch

        with patch("modules.device.service.settings") as mock_cfg:
            mock_cfg.livekit_api_key = "test-key"
            mock_cfg.livekit_api_secret = "test-secret"
            mock_cfg.livekit_url = "wss://test.livekit.io"

            from modules.device.service import generate_livekit_token
            token, room_name = await generate_livekit_token(
                user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )

        assert token.startswith("eyJ")
        assert room_name == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    @pytest.mark.asyncio

    @pytest.mark.asyncio
    async def test_token_identity_is_dev_prefixed(self):
        from unittest.mock import patch

        with patch("modules.device.service.settings") as mock_cfg:
            mock_cfg.livekit_api_key = "test-key"
            mock_cfg.livekit_api_secret = "test-secret"
            mock_cfg.livekit_url = "wss://test.livekit.io"

            from modules.device.service import generate_livekit_token
            token, _ = await generate_livekit_token(
                user_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            )

        import base64, json as _json
        payload_b64 = token.split(".")[1] + "=="
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        assert payload.get("sub") == "dev-a1b2c3d4-e5f6-7890-abcd-ef1234567890"
