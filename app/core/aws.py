import asyncio
import json
import boto3
from app.core.config import settings

def _get_iot_client():
    return boto3.client(
        "iot-data",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id if settings.aws_access_key_id else None,
        aws_secret_access_key=settings.aws_secret_access_key if settings.aws_secret_access_key else None,
        endpoint_url=f"https://{settings.aws_iot_endpoint}"
    )

async def publish_mqtt_message(topic: str, payload: dict) -> None:
    def _publish():
        client = _get_iot_client()
        client.publish(
            topic=topic,
            qos=1,
            payload=json.dumps(payload)
        )
    
    await asyncio.to_thread(_publish)
