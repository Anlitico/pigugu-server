import asyncio
import json
import logging
import boto3
from botocore.exceptions import ClientError
from app.core.config import settings

logger = logging.getLogger(__name__)

# ── IoT Data Plane (MQTT publish) ──────────────────────────────────────────

def _get_iot_data_client():
    return boto3.client(
        "iot-data",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id if settings.aws_access_key_id else None,
        aws_secret_access_key=settings.aws_secret_access_key if settings.aws_secret_access_key else None,
        endpoint_url=f"https://{settings.aws_iot_endpoint}"
    )

async def publish_mqtt_message(topic: str, payload: dict) -> None:
    def _publish():
        client = _get_iot_data_client()
        client.publish(
            topic=topic,
            qos=1,
            payload=json.dumps(payload)
        )

    await asyncio.to_thread(_publish)


# ── IoT Control Plane (certificate / thing / policy management) ────────────

def _get_iot_client():
    kwargs = {
        "region_name": settings.aws_region,
    }
    if settings.aws_access_key_id:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("iot", **kwargs)


def create_device_certificate() -> dict:
    client = _get_iot_client()
    return client.create_keys_and_certificate(setAsActive=True)


def ensure_device_thing(thing_name: str) -> str:
    client = _get_iot_client()
    try:
        response = client.create_thing(thingName=thing_name)
        return response["thingArn"]
    except client.exceptions.ResourceAlreadyExistsException:
        response = client.describe_thing(thingName=thing_name)
        return response["thingArn"]


def attach_cert_to_thing(thing_name: str, cert_arn: str) -> None:
    client = _get_iot_client()
    client.attach_thing_principal(thingName=thing_name, principal=cert_arn)


def attach_policy_to_cert(cert_arn: str) -> None:
    client = _get_iot_client()
    client.attach_policy(policyName=settings.aws_iot_policy_name, target=cert_arn)


def detach_cert_from_thing(thing_name: str, cert_arn: str) -> None:
    client = _get_iot_client()
    try:
        client.detach_thing_principal(thingName=thing_name, principal=cert_arn)
    except ClientError as e:
        logger.warning(f"Failed to detach cert {cert_arn} from thing {thing_name}: {e}")


def deactivate_certificate(cert_id: str) -> None:
    client = _get_iot_client()
    client.update_certificate(certificateId=cert_id, newStatus="INACTIVE")


def cleanup_old_certificates(thing_name: str) -> None:
    client = _get_iot_client()
    try:
        paginator = client.get_paginator("list_thing_principals")
        for page in paginator.paginate(thingName=thing_name, maxResults=25):
            for principal in page.get("principals", []):
                try:
                    client.detach_thing_principal(thingName=thing_name, principal=principal)
                    cert_id = principal.rsplit("/", 1)[-1]
                    client.update_certificate(certificateId=cert_id, newStatus="INACTIVE")
                    logger.info(f"Deactivated old cert: {cert_id}")
                except ClientError as e:
                    logger.warning(f"Failed to clean up principal {principal}: {e}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            pass  # Thing doesn't exist yet — first provisioning
        else:
            logger.warning(f"Failed to list thing principals for {thing_name}: {e}")
