import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Header, HTTPException, Request

from core.config import settings
from core.redis import redis_get, redis_set, redis_exists

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/device/iot", tags=["device", "iot"])


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
            if "/device/iot" in summary_url:
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


# ── WS push helper ──────────────────────────────────────────

async def _push_ws(hw_id: str, event: dict) -> None:
    """Push a JSON event to the app via WebSocket (best-effort)."""
    try:
        from modules.ws.manager import ws_manager
        await ws_manager.broadcast(hw_id, json.dumps(event))
    except Exception as e:
        logger.warning("WS push failed for %s: %s", hw_id, e)


# ── Ping-pong (pure function — reusable by provisioning & reboot) ─

async def ping_pong(hw_id: str, ping: dict, pong_key: str,
                    timeout_s: float = 10.0) -> dict | None:
    """Publish a connectivity.ping and block until pong appears in Redis.

    Returns the pong dict on success, None on timeout.
    Does NOT know about sessions, provisioning, or WS — pure transport.
    """
    from core.aws import publish_mqtt_message

    await publish_mqtt_message(f"pgg/dev/{hw_id}/c2d", ping)

    deadline = datetime.now().timestamp() + timeout_s
    while datetime.now().timestamp() < deadline:
        pong_raw = await redis_get(pong_key)
        if pong_raw:
            return json.loads(pong_raw)
        await asyncio.sleep(0.5)

    return None


async def _wait_for_pong(hw_id: str, request_id: str, session_id: str) -> None:
    """Provisioning: fire-and-forget watchdog that pushes WS error on timeout."""
    await asyncio.sleep(10)
    try:
        key = f"provision:verify:{session_id}:{request_id}"
        if not await redis_exists(key):
            await _push_ws(hw_id, {
                "event": "error",
                "error_code": "PROVISION_VERIFY_TIMEOUT",
                "error_msg": "设备无法连接到服务器",
            })
    except Exception as e:
        logger.warning("_wait_for_pong failed: %s", e)


# ── Message handlers ────────────────────────────────────────

async def _handle_online(hw_id: str, msg: dict) -> None:
    """device.online → Redis + WS, then ping-pong to confirm connectivity.

    Two callers:
      Path A (provisioning): session_id present → use nonce, WS 'verifying',
          wait for pong via background watchdog.
      Path B (post-reboot): no session_id → simple ping, just confirm.
    """
    hw_id = hw_id.strip().lower()
    await redis_set(f"device:online:hw:{hw_id}", "1", ex=90)
    await redis_set(f"device:last_seen:hw:{hw_id}", str(datetime.now().isoformat()), ex=86400)
    await _push_ws(hw_id, {"event": "online", "hardware_id": hw_id})

    session_id = msg.get("session_id")
    request_id = uuid.uuid4()
    ping = {
        "msg_type": "connectivity.ping",
        "request_id": str(request_id),
        "ts": int(datetime.now().timestamp()),
    }

    # ── Path B: post-reboot ──────────────────────────────────
    if not session_id:
        try:
            await ping_pong(hw_id, ping,
                             f"device:connectivity:hw:{hw_id}:{request_id}")
        except Exception:
            logger.exception("_handle_online ping-pong failed for %s", hw_id)
        return

    # ── Path A: provisioning ─────────────────────────────────
    try:
        from sqlalchemy import select
        from core.database import AsyncSessionLocal
        from models.device_provisioning_session import DeviceProvisioningSession

        sid = uuid.UUID(session_id)
        async with AsyncSessionLocal() as db:
            session_result = await db.execute(
                select(DeviceProvisioningSession).where(DeviceProvisioningSession.id == sid)
            )
            session = session_result.scalar_one_or_none()
            if not session or session.status != "created":
                return

            if session.expires_at < datetime.now(timezone.utc):
                session.status = "expired"
                await db.commit()
                await _push_ws(hw_id, {
                    "event": "error",
                    "error_code": "PROVISION_SESSION_EXPIRED",
                    "error_msg": "配网会话已过期",
                })
                return

            session.status = "verifying"
            session.hardware_id = hw_id
            session.request_id = request_id
            await db.commit()

            ping["session_id"] = session_id
            ping["nonce"] = session.challenge_nonce
            ping["deadline_ms"] = 6000
            ping["payload"] = {}

        # Publish the ping (must happen outside the DB session block)
        from core.aws import publish_mqtt_message
        await publish_mqtt_message(f"pgg/dev/{hw_id}/c2d", ping)

        await _push_ws(hw_id, {"event": "verifying"})

        # Fire-and-forget watchdog: waits for pong or pushes timeout error
        asyncio.create_task(_wait_for_pong(hw_id, str(request_id), session_id))

    except Exception as e:
        logger.error("_handle_online failed for %s: %s", hw_id, e)
        await _push_ws(hw_id, {"event": "error", "error_msg": "配网服务异常"})


async def _handle_pong(hw_id: str, msg: dict, session_id: str | None, request_id: str | None) -> None:
    """connectivity.pong → persist RTT + WS 'verified'."""
    hw_id = hw_id.strip().lower()

    if session_id and request_id:
        await redis_set(
            f"provision:verify:{session_id}:{request_id}",
            json.dumps(msg),
            ex=300,
        )

    if request_id:
        await redis_set(
            f"device:connectivity:hw:{hw_id}:{request_id}",
            json.dumps(msg),
            ex=300,
        )

    # Persist RTT to Device (best-effort)
    rtt_ms = None
    try:
        from sqlalchemy import update
        from core.database import AsyncSessionLocal
        from models.device import Device
        async with AsyncSessionLocal() as db:
            rtt_ms = int((datetime.now().timestamp() - msg.get("ts", 0)) * 1000)
            await db.execute(
                update(Device)
                .where(Device.hardware_id == hw_id)
                .values(last_rtt_ms=rtt_ms, last_seen_at=datetime.now(timezone.utc))
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to persist device RTT for %s: %s", hw_id, e)

    await _push_ws(hw_id, {"event": "verified", "rtt_ms": rtt_ms})


async def _handle_register(hw_id: str, msg: dict) -> None:
    """device.register → bind device to user + WS 'bound' or 'error'."""
    hw_id = hw_id.strip().lower()
    session_id = msg.get("session_id")
    if not session_id:
        return

    try:
        from sqlalchemy import select, exists as _exists, and_
        from core.database import AsyncSessionLocal
        from models.device import Device
        from models.device_provisioning_session import DeviceProvisioningSession

        sid = uuid.UUID(session_id)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(DeviceProvisioningSession).where(DeviceProvisioningSession.id == sid)
            )
            session = result.scalar_one_or_none()
            if not session or session.status != "verifying":
                return

            # Check existing device
            result = await db.execute(
                select(Device).where(Device.hardware_id.ilike(hw_id))
            )
            existing = result.scalar_one_or_none()

            device_name = f"Pigugu {hw_id[-4:]}" if len(hw_id) >= 4 else f"Pigugu {hw_id}"

            if existing:
                if existing.binding_status != "bound":
                    existing.user_id = session.user_id
                    existing.device_name = device_name
                    existing.binding_status = "bound"
                    if session.certificate_arn:
                        existing.certificate_arn = session.certificate_arn
                    existing.thing_name = hw_id
                elif existing.user_id != session.user_id:
                    await _push_ws(hw_id, {"event": "error", "error_code": "DEVICE_ALREADY_BOUND", "error_msg": "该设备已被其他账号绑定"})
                    return
            else:
                has_active = (await db.execute(
                    select(_exists().where(and_(
                        Device.user_id == session.user_id,
                        Device.active_state == "active",
                        Device.binding_status == "bound",
                    )))
                )).scalar()

                device = Device(
                    id=uuid.uuid4(),
                    user_id=session.user_id,
                    device_name=device_name,
                    hardware_id=hw_id,
                    active_state="active" if not has_active else "standby",
                    binding_status="bound",
                    certificate_arn=session.certificate_arn,
                    thing_name=hw_id,
                )
                db.add(device)

            session.status = "bound"
            await db.commit()

            await _push_ws(hw_id, {
                "event": "bound",
                "hardware_id": hw_id,
                "device_name": device_name,
            })

    except Exception as e:
        logger.exception("_handle_register failed for %s: %s", hw_id, e)
        await _push_ws(hw_id, {"event": "error", "error_msg": "绑定设备失败"})


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

    # SNS-style SubscriptionConfirmation body (V2 Destination)
    if payload and payload.get("messageType") == "DestinationConfirmation":
        token = payload.get("confirmationToken", "")
        dest_arn = payload.get("arn", "")
        if token:
            asyncio.create_task(
                _confirm_and_enable_destination(token, dest_arn)
            )
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(token)

    # V1 HTTP Action confirmation (legacy inline HTTP action)
    # When the rule's URL changes, AWS IoT sends a POST with
    # {"confirmationToken":"..."} in the body — no x-aws-secret header.
    # Must respond with token as plain text BEFORE the secret check.
    if payload and "confirmationToken" in payload and "messageType" not in payload:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(str(payload["confirmationToken"]))

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
    body = payload or {}
    msg = body.get("payload", body)
    
    msg_type = msg.get("msg_type")
    request_id = msg.get("request_id")
    session_id = msg.get("session_id")

    if msg_type == "device.online":
        await _handle_online(hw_id, msg)

    elif msg_type == "connectivity.pong":
        await _handle_pong(hw_id, msg, session_id, request_id)

    elif msg_type == "device.register":
        await _handle_register(hw_id, msg)

    elif msg_type == "device.heartbeat":
        await redis_set(f"device:online:hw:{hw_id}", "1", ex=90)
        await redis_set(f"device:last_seen:hw:{hw_id}", str(datetime.now().isoformat()), ex=86400)

    return {"status": "ok"}
