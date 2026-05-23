import asyncio
import json
import logging
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Header, HTTPException, Request

from core.config import settings
from core.redis import redis_set

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/aws-iot", tags=["internal", "aws-iot"])


def _get_iot_control_client():
    """IoT control plane client for destination management."""
    return boto3.client(
        "iot",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id if settings.aws_access_key_id else None,
        aws_secret_access_key=settings.aws_secret_access_key if settings.aws_secret_access_key else None,
    )


async def _confirm_and_enable_destination(confirmation_token: str, dest_arn: str | None):
    """Confirm the destination and enable it so the Rule can forward messages."""
    client = _get_iot_control_client()

    def _confirm():
        client.confirm_topic_rule_destination(
            confirmationToken=confirmation_token
        )
    await asyncio.to_thread(_confirm)
    logger.info("Destination confirmed: %s", dest_arn or "(from token)")

    # After confirmation we need the ARN to enable. If not provided via header,
    # try to list destinations and find the one matching our URL.
    if not dest_arn:
        def _list_destinations():
            return client.list_topic_rule_destinations()
        result = await asyncio.to_thread(_list_destinations)
        for d in result.get("destinationSummaries", []):
            summary_url = d.get("httpUrlSummary", {}).get("confirmationUrl", "")
            if "api.pigugu.com" in summary_url:
                dest_arn = d["arn"]
                break

    if dest_arn:
        def _enable():
            client.update_topic_rule_destination(
                arn=dest_arn,
                status="ENABLED"
            )
        await asyncio.to_thread(_enable)
        logger.info("Destination enabled: %s", dest_arn)
    else:
        logger.warning("Could not find destination ARN to enable")


@router.post("/webhook")
async def aws_iot_webhook(
    request: Request,
    payload: dict | None = None,
    x_aws_secret: str | None = Header(None, alias="x-aws-secret")
):
    # 1. Handle AWS IoT Topic Rule Destination subscription confirmation.
    # Confirmation requests do NOT include the Rule's custom headers,
    # so this MUST run before secret validation.
    # Ref: https://docs.aws.amazon.com/iot/latest/developerguide/http-action-destination.html
    confirmation_token = request.query_params.get("confirmationToken")
    if confirmation_token:
        dest_arn = request.headers.get("x-amz-rules-engine-destination-arn")

        # Fire-and-forget: confirm + enable in the background so we can
        # respond immediately (echo the token).
        asyncio.create_task(
            _confirm_and_enable_destination(confirmation_token, dest_arn)
        )

        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(confirmation_token)

    # Alternative: SNS-style SubscriptionConfirmation body
    if payload and payload.get("messageType") == "DestinationConfirmation":
        token = payload.get("confirmationToken", "")
        dest_arn = payload.get("arn", "")
        if token:
            asyncio.create_task(
                _confirm_and_enable_destination(token, dest_arn)
            )
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(token)

    # Verify Secret for regular D2C message processing
    if x_aws_secret != settings.aws_iot_webhook_secret:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 3. Extract Topic
    # AWS IoT rules can pass topic in headers or payload depending on config.
    # For now, we assume it's passed in the payload by our rule SQL:
    # SELECT * AS payload, topic() AS topic
    topic = payload.get("topic") if payload else None
    if not topic:
        # Fallback to header if configured that way
        topic = request.headers.get("x-amz-sns-topic-arn", "")

    if not topic:
        raise HTTPException(status_code=400, detail="Missing topic")
        
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != "pgg" or parts[1] != "dev":
        raise HTTPException(status_code=400, detail="Invalid topic format")
        
    hw_id = parts[2]
    
    # Payload is either directly payload, or nested if we used AS payload
    msg = payload.get("payload", payload)
    
    msg_type = msg.get("msg_type")
    request_id = msg.get("request_id")
    session_id = msg.get("session_id")

    if msg_type == "connectivity.pong":
        if session_id and request_id:
            msg["rtt_ms"] = int((datetime.now().timestamp() - msg.get("ts", 0)) * 1000)
            await redis_set(
                f"provision:verify:{session_id}:{request_id}",
                json.dumps(msg),
                ex=300
            )

        if request_id:
             await redis_set(
                f"device:connectivity:hw:{hw_id}:{request_id}",
                json.dumps(msg),
                ex=300
            )

        # Persist last RTT to Device record (best-effort)
        try:
            from sqlalchemy import update
            from core.database import AsyncSessionLocal
            from models.device import Device
            async with AsyncSessionLocal() as db:
                rtt = int((datetime.now().timestamp() - msg.get("ts", 0)) * 1000)
                await db.execute(
                    update(Device)
                    .where(Device.hardware_id == hw_id)
                    .values(last_rtt_ms=rtt, last_seen_at=datetime.now(timezone.utc))
                )
                await db.commit()
        except Exception as e:
            logger.warning("Failed to persist device RTT for %s: %s", hw_id, e)

    elif msg_type in ("device.heartbeat", "device.online"):
        await redis_set(f"device:online:hw:{hw_id}", "1", ex=90)
        await redis_set(f"device:last_seen:hw:{hw_id}", str(datetime.now().isoformat()), ex=86400)

    return {"status": "ok"}
