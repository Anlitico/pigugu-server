# pigagent/core/aws_mqtt.py
"""AWS IoT MQTT publish for the agent service.

Mirrors api/core/aws.py publish logic so the agent can send C2D messages
(volume control, etc.) directly without a Redis/HTTP relay hop.
Reads AWS config from AgentConfig (which resolves env vars + .config file).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import boto3

from agent_config import get_config

logger = logging.getLogger(__name__)

_client = None


def _get_iot_data_client():
    """Return a cached boto3 IoT Data Plane client for MQTT publish."""
    global _client
    if _client is not None:
        return _client

    config = get_config()
    endpoint = config.AWS_IOT_ENDPOINT
    if not endpoint:
        raise RuntimeError(
            "AWS_IOT_ENDPOINT is not configured. "
            "Set it in .env or .config for the agent service."
        )

    _client = boto3.client(
        "iot-data",
        region_name=config.AWS_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY") or None,
        endpoint_url=f"https://{endpoint}",
    )
    return _client


async def publish_mqtt_message(topic: str, payload: dict) -> None:
    """Publish a JSON payload to an MQTT topic via AWS IoT Data Plane.

    QoS 1 ensures at-least-once delivery. Runs the boto3 call in a
    thread to avoid blocking the event loop.

    Raises:
        Exception: if the publish fails — callers should handle or propagate.
    """
    def _publish():
        client = _get_iot_data_client()
        client.publish(
            topic=topic,
            qos=1,
            payload=json.dumps(payload),
        )

    await asyncio.to_thread(_publish)
