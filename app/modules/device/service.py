import asyncio
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.core.security import create_mqtt_token, decode_mqtt_token
from app.models.device import Device
from app.models.device_provisioning_session import DeviceProvisioningSession
from app.modules.device.schemas import (
    DeviceBindRequest,
    MqttCredentialResponse,
    MqttTokenRefreshResponse,
    VerifyConnectivityResponse,
)


async def issue_mqtt_credentials(
    db: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID, hardware_id: str
):
    """Issue MQTT credentials for a hardware_id within a valid provisioning session."""
    hw_id = hardware_id.strip().lower()
    if not hw_id:
        raise ValueError("HARDWARE_ID_EMPTY")

    result = await db.execute(
        select(DeviceProvisioningSession).where(
            DeviceProvisioningSession.id == session_id,
            DeviceProvisioningSession.user_id == user_id,
        )
    )
    session = result.scalar_one_or_none()

    if not session:
        raise ValueError("PROVISION_SESSION_NOT_FOUND")

    if session.expires_at < datetime.now(timezone.utc):
        session.status = "expired"
        raise ValueError("PROVISION_SESSION_EXPIRED")

    if session.status in ("expired", "bound", "cancelled"):
        raise ValueError(f"PROVISION_SESSION_INVALID_STATE:{session.status}")

    token, jti, expires_at = create_mqtt_token(hw_id)
    session.hardware_id = hw_id

    return MqttCredentialResponse(
        broker_uri=settings.mqtt_broker_uri,
        username=hw_id,
        password=token,
        expires_at=expires_at,
    )


async def refresh_mqtt_token(old_token: str):
    """Refresh an MQTT token. Accepts tokens within a 1-hour grace period after expiry."""
    try:
        payload = decode_mqtt_token(old_token, verify_exp=True)
    except ValueError:
        payload = decode_mqtt_token(old_token, verify_exp=False)
        exp_ts = payload.get("exp", 0)
        now_ts = datetime.now().timestamp()
        if now_ts - exp_ts > 3600:
            raise ValueError("MQTT_TOKEN_EXPIRED_BEYOND_GRACE")

    hw_id = payload.get("hw_id") or payload.get("sub")
    if not hw_id:
        raise ValueError("MQTT_TOKEN_MISSING_HW_ID")

    token, jti, expires_at = create_mqtt_token(hw_id)
    return MqttTokenRefreshResponse(password=token, expires_at=expires_at)


async def create_provisioning_session(
    db: AsyncSession, user_id: uuid.UUID
) -> DeviceProvisioningSession:
    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=user_id,
        status="created",
        challenge_nonce=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    db.add(session)
    await db.flush()
    return session


async def verify_connectivity(
    db: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID, hardware_id: str | None = None
) -> VerifyConnectivityResponse:
    # 1. Check session
    result = await db.execute(
        select(DeviceProvisioningSession).where(
            DeviceProvisioningSession.id == session_id, DeviceProvisioningSession.user_id == user_id
        )
    )
    session = result.scalar_one_or_none()

    if not session:
        return VerifyConnectivityResponse(verified=False, error_code="PROVISION_SESSION_NOT_FOUND")

    if session.expires_at < datetime.now(timezone.utc):
        session.status = "expired"
        return VerifyConnectivityResponse(verified=False, error_code="PROVISION_SESSION_EXPIRED")

    # Update hardware_id if provided
    if hardware_id:
        session.hardware_id = hardware_id
    
    if not session.hardware_id:
        return VerifyConnectivityResponse(verified=False, error_code="HARDWARE_ID_REQUIRED")

    hw_id = session.hardware_id.strip().lower()
    
    # 2. Publish ping and wait for pong
    request_id = uuid.uuid4()
    session.request_id = request_id
    session.status = "verifying"
    await db.flush()

    redis = await get_redis()
    
    ping_payload = {
        "msg_type": "connectivity.ping",
        "request_id": str(request_id),
        "session_id": str(session_id),
        "nonce": session.challenge_nonce,
        "ts": int(datetime.now().timestamp()),
        "deadline_ms": 6000,
        "payload": {},
    }

    from app.core.aws import publish_mqtt_message
    await publish_mqtt_message(f"pgg/dev/{hw_id}/c2d", ping_payload)

    for retry in range(2):  # Try twice as per doc (6s + 1 retry)
        start_time = datetime.now()
        while (datetime.now() - start_time).total_seconds() < 6:
            pong_data = await redis.get(f"provision:verify:{session_id}:{request_id}")
            if pong_data:
                pong = json.loads(pong_data)
                if pong.get("nonce") == session.challenge_nonce:
                    session.status = "verified"
                    rtt = pong.get("rtt_ms")
                    return VerifyConnectivityResponse(verified=True, rtt_ms=rtt)
            await asyncio.sleep(0.5)
        
        if retry == 0:
            await publish_mqtt_message(f"pgg/dev/{hw_id}/c2d", ping_payload)

    session.status = "failed"
    session.failure_code = "PROVISION_VERIFY_TIMEOUT"
    return VerifyConnectivityResponse(verified=False, error_code="PROVISION_VERIFY_TIMEOUT")


async def connectivity_check(
    db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID
) -> VerifyConnectivityResponse:
    # 1. Check device
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        return VerifyConnectivityResponse(verified=False, error_code="DEVICE_NOT_FOUND")
        
    hw_id = device.hardware_id.strip().lower()
    
    # 2. Publish ping and wait for pong
    request_id = uuid.uuid4()
    
    redis = await get_redis()
    
    ping_payload = {
        "msg_type": "connectivity.ping",
        "request_id": str(request_id),
        "ts": int(datetime.now().timestamp()),
        "deadline_ms": 6000,
        "payload": {},
    }

    from app.core.aws import publish_mqtt_message
    await publish_mqtt_message(f"pgg/dev/{hw_id}/c2d", ping_payload)

    for retry in range(2):
        start_time = datetime.now()
        while (datetime.now() - start_time).total_seconds() < 6:
            pong_data = await redis.get(f"device:connectivity:hw:{hw_id}:{request_id}")
            if pong_data:
                pong = json.loads(pong_data)
                rtt = pong.get("rtt_ms")
                return VerifyConnectivityResponse(verified=True, rtt_ms=rtt)
            await asyncio.sleep(0.5)
        
        if retry == 0:
            await publish_mqtt_message(f"pgg/dev/{hw_id}/c2d", ping_payload)

    return VerifyConnectivityResponse(verified=False, error_code="DEVICE_UNREACHABLE")


async def bind_device(db: AsyncSession, user_id: uuid.UUID, body: DeviceBindRequest) -> Device:
    # 1. Verify session
    result = await db.execute(
        select(DeviceProvisioningSession).where(
            DeviceProvisioningSession.id == body.session_id,
            DeviceProvisioningSession.user_id == user_id,
        )
    )
    session = result.scalar_one_or_none()
    
    if not session or session.status != "verified":
        raise ValueError("PROVISION_SESSION_NOT_VERIFIED")
    
    hw_id_normalized = body.hardware_id.strip().lower()
    
    # 2. Check existing device
    result = await db.execute(
        select(Device).where(Device.hardware_id.ilike(hw_id_normalized))
    )
    existing_device = result.scalar_one_or_none()
    
    if existing_device:
        if existing_device.user_id == user_id:
            # Idempotent return
            existing_device.device_name = body.device_name
            existing_device.binding_status = "bound"
            session.status = "bound"
            return existing_device
        elif existing_device.binding_status != "bound":
            # Rebind an unbound device
            existing_device.user_id = user_id
            existing_device.device_name = body.device_name
            existing_device.binding_status = "bound"
            session.status = "bound"
            
            # Check if this is the first device to set active for this user
            result = await db.execute(select(Device).where(Device.user_id == user_id, Device.id != existing_device.id))
            has_devices = result.first() is not None
            existing_device.active_state = "standby" if has_devices else "active"
            return existing_device
        else:
            raise ValueError("DEVICE_ALREADY_BOUND")
            
    # 3. Create new device
    # Check if this is the first device to set active
    result = await db.execute(select(Device).where(Device.user_id == user_id))
    has_devices = result.first() is not None
    
    device = Device(
        id=uuid.uuid4(),
        user_id=user_id,
        device_name=body.device_name,
        hardware_id=body.hardware_id.strip(),
        active_state="active" if not has_devices else "standby",
        binding_status="bound"
    )
    db.add(device)
    session.status = "bound"
    return device


async def get_devices_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[Device]:
    result = await db.execute(
        select(Device).where(Device.user_id == user_id, Device.binding_status == "bound")
    )
    devices = list(result.scalars().all())
    
    redis = await get_redis()
    for device in devices:
        # Check both ID-based and HW-based online keys
        id_online = await redis.exists(f"device:online:{device.id}")
        hw_online = await redis.exists(f"device:online:hw:{device.hardware_id.strip().lower()}")
        device.is_online = (id_online > 0) or (hw_online > 0)
        
    return devices


async def set_active_device(db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID) -> Device:
    # Set all other devices to standby
    await db.execute(
        update(Device)
        .where(Device.user_id == user_id, Device.id != device_id)
        .values(active_state="standby")
    )
    
    # Set target device to active
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise ValueError("DEVICE_NOT_FOUND")
        
    device.active_state = "active"
    return device


async def unbind_device(db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID) -> None:
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise ValueError("DEVICE_NOT_FOUND")
        
    is_active = device.active_state == "active"
    device.binding_status = "unbound"
    device.active_state = "standby"
    
    if is_active:
        # Try to promote another online device
        devices = await get_devices_for_user(db, user_id)
        for d in devices:
            if d.id != device_id and d.is_online:
                d.active_state = "active"
                break


async def rename_device(db: AsyncSession, user_id: uuid.UUID, device_id: uuid.UUID, name: str) -> Device:
    result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == user_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise ValueError("DEVICE_NOT_FOUND")
        
    device.device_name = name
    return device


async def generate_livekit_token(device_id: uuid.UUID) -> tuple[str, str]:
    """Returns (token, room_name)."""
    # Placeholder for actual LiveKit implementation
    return "token", f"room_{device_id}"


async def update_device_state(device_id: str, state: str) -> None:
    """Write state to Redis with 60s TTL."""
    redis = await get_redis()
