"""Unit tests for pigagent/core/aws_mqtt.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestGetIotDataClient:
    def test_client_cached(self):
        """_get_iot_data_client returns cached client on second call."""
        with patch("core.aws_mqtt.boto3.client") as mock_boto, \
             patch("core.aws_mqtt.get_config") as mock_config:
            mock_config.return_value.AWS_IOT_ENDPOINT = "test.iot.us-west-1.amazonaws.com"
            mock_config.return_value.AWS_REGION = "us-west-1"
            mock_boto.return_value = MagicMock()

            from core.aws_mqtt import _get_iot_data_client

            client1 = _get_iot_data_client()
            client2 = _get_iot_data_client()

            assert client1 is client2
            mock_boto.assert_called_once()

    def test_raises_without_endpoint(self):
        """Empty AWS_IOT_ENDPOINT → RuntimeError."""
        with patch("core.aws_mqtt.get_config") as mock_config:
            mock_config.return_value.AWS_IOT_ENDPOINT = ""
            mock_config.return_value.AWS_REGION = "us-west-1"

            from core.aws_mqtt import _get_iot_data_client
            # Reset cache
            import core.aws_mqtt as mod
            mod._client = None

            with pytest.raises(RuntimeError, match="AWS_IOT_ENDPOINT"):
                _get_iot_data_client()


class TestPublishMqttMessage:
    @pytest.mark.asyncio
    async def test_publishes_json_payload(self):
        """Calls boto3 iot-data publish with JSON payload."""
        mock_client = MagicMock()
        mock_client.publish = MagicMock()

        with patch("core.aws_mqtt._get_iot_data_client", return_value=mock_client):
            from core.aws_mqtt import publish_mqtt_message
            await publish_mqtt_message("pgg/dev/test/c2d", {"msg_type": "test"})

        mock_client.publish.assert_called_once()
        call = mock_client.publish.call_args[1]
        assert call["topic"] == "pgg/dev/test/c2d"
        assert call["qos"] == 1
        assert '"msg_type": "test"' in call["payload"]

    @pytest.mark.asyncio
    async def test_raises_on_publish_failure(self):
        """Publish failure → exception propagates to caller."""
        mock_client = MagicMock()
        mock_client.publish = MagicMock(side_effect=RuntimeError("AWS down"))

        with patch("core.aws_mqtt._get_iot_data_client", return_value=mock_client):
            from core.aws_mqtt import publish_mqtt_message
            with pytest.raises(RuntimeError, match="AWS down"):
                await publish_mqtt_message("topic", {"k": "v"})
