import uuid
import json
from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.models.device import Device
from app.models.device_provisioning_session import DeviceProvisioningSession
from app.modules.device.schemas import DeviceBindRequest
from app.modules.device.service import (
    create_provisioning_session,
    verify_connectivity,
    bind_device,
    set_active_device,
    unbind_device,
)

@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="test_device@example.com",
        hashed_password="hash",
        display_name="Test User",
    )
    db_session.add(user)
    await db_session.flush()
    return user

@pytest.mark.asyncio
async def test_create_provisioning_session(db_session: AsyncSession, test_user: User):
    session = await create_provisioning_session(db_session, test_user.id)
    
    assert session is not None
    assert session.user_id == test_user.id
    assert session.status == "created"
    assert session.challenge_nonce is not None
    assert session.expires_at > datetime.now()

@pytest.mark.asyncio
@patch("app.core.aws.publish_mqtt_message")
@patch("app.modules.device.service.get_redis")
async def test_verify_connectivity_success(
    mock_get_redis, mock_publish, db_session: AsyncSession, test_user: User
):
    # 1. Create a session
    session = await create_provisioning_session(db_session, test_user.id)
    
    # 2. Mock Redis behavior (return pong immediately)
    mock_redis = AsyncMock()
    pong_payload = {"nonce": session.challenge_nonce, "rtt_ms": 120}
    mock_redis.get.return_value = json.dumps(pong_payload)
    mock_get_redis.return_value = mock_redis
    
    # 3. Verify
    res = await verify_connectivity(db_session, session.id, test_user.id, "TEST-HW-123")
    
    assert res.verified is True
    assert res.rtt_ms == 120
    assert session.status == "verified"
    mock_publish.assert_called_once()
    
@pytest.mark.asyncio
async def test_bind_device_success_first_device(db_session: AsyncSession, test_user: User):
    # Setup verified session
    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="verified",
        challenge_nonce="nonce",
        expires_at=datetime.now() + timedelta(minutes=10)
    )
    db_session.add(session)
    await db_session.flush()
    
    # Bind
    req = DeviceBindRequest(session_id=session.id, hardware_id="TEST-HW-123", device_name="My Pigugu")
    device = await bind_device(db_session, test_user.id, req)
    
    assert device is not None
    assert device.user_id == test_user.id
    assert device.hardware_id == "TEST-HW-123"
    assert device.active_state == "active"
    assert device.binding_status == "bound"
    assert session.status == "bound"

@pytest.mark.asyncio
async def test_bind_device_conflict(db_session: AsyncSession, test_user: User):
    other_user = User(id=uuid.uuid4(), email="other@example.com", hashed_password="h")
    db_session.add(other_user)
    
    existing_device = Device(
        id=uuid.uuid4(),
        user_id=other_user.id,
        hardware_id="test-hw-456",
        device_name="Someone else's",
        active_state="active",
        binding_status="bound"
    )
    db_session.add(existing_device)
    
    session = DeviceProvisioningSession(
        id=uuid.uuid4(), user_id=test_user.id, status="verified", challenge_nonce="x", expires_at=datetime.now() + timedelta(minutes=10)
    )
    db_session.add(session)
    await db_session.flush()
    
    req = DeviceBindRequest(session_id=session.id, hardware_id="TEST-HW-456", device_name="Stolen")
    
    with pytest.raises(ValueError, match="DEVICE_ALREADY_BOUND"):
        await bind_device(db_session, test_user.id, req)

@pytest.mark.asyncio
async def test_bind_device_soft_unbind_transfer(db_session: AsyncSession, test_user: User):
    other_user = User(id=uuid.uuid4(), email="other2@example.com", hashed_password="h")
    db_session.add(other_user)
    
    # Device belongs to other user but is UNBOUND (soft deleted)
    unbound_device = Device(
        id=uuid.uuid4(),
        user_id=other_user.id,
        hardware_id="test-hw-789",
        device_name="Discarded",
        active_state="standby",
        binding_status="unbound"
    )
    db_session.add(unbound_device)
    
    session = DeviceProvisioningSession(
        id=uuid.uuid4(), user_id=test_user.id, status="verified", challenge_nonce="y", expires_at=datetime.now() + timedelta(minutes=10)
    )
    db_session.add(session)
    await db_session.flush()
    
    req = DeviceBindRequest(session_id=session.id, hardware_id="TEST-HW-789", device_name="Rescued")
    device = await bind_device(db_session, test_user.id, req)
    
    assert device.id == unbound_device.id
    assert device.user_id == test_user.id  # Ownership transferred
    assert device.binding_status == "bound"
    assert device.active_state == "active"

@pytest.mark.asyncio
@patch("app.modules.device.service.get_redis")
async def test_get_devices_for_user(mock_get_redis, db_session: AsyncSession, test_user: User):
    from app.modules.device.service import get_devices_for_user
    
    device1 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="D1", binding_status="bound")
    device2 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev2", device_name="D2", binding_status="bound")
    device3 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev3", device_name="D3", binding_status="unbound")
    db_session.add_all([device1, device2, device3])
    await db_session.flush()

    mock_redis = AsyncMock()
    # Let dev1 be online (id-based exists returns 1), dev2 offline
    mock_redis.exists.side_effect = lambda key: 1 if "dev1" in key or str(device1.id) in key else 0
    mock_get_redis.return_value = mock_redis

    devices = await get_devices_for_user(db_session, test_user.id)
    
    assert len(devices) == 2  # unbound device should not be returned
    d1 = next(d for d in devices if d.id == device1.id)
    d2 = next(d for d in devices if d.id == device2.id)
    
    assert d1.is_online is True
    assert d2.is_online is False

@pytest.mark.asyncio
async def test_set_active_device(db_session: AsyncSession, test_user: User):
    device1 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="D1", active_state="active", binding_status="bound")
    device2 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev2", device_name="D2", active_state="standby", binding_status="bound")
    db_session.add_all([device1, device2])
    await db_session.flush()

    updated = await set_active_device(db_session, test_user.id, device2.id)
    
    assert updated.id == device2.id
    assert updated.active_state == "active"
    
    # Check that device1 is now standby
    await db_session.refresh(device1)
    assert device1.active_state == "standby"

@pytest.mark.asyncio
@patch("app.modules.device.service.get_devices_for_user")
async def test_unbind_device(mock_get_devices, db_session: AsyncSession, test_user: User):
    # device1 is active, device2 is standby
    device1 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="D1", active_state="active", binding_status="bound")
    device2 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev2", device_name="D2", active_state="standby", binding_status="bound")
    db_session.add_all([device1, device2])
    await db_session.flush()

    # Mock get_devices_for_user to return device2 as online
    device2.is_online = True
    mock_get_devices.return_value = [device1, device2]

    await unbind_device(db_session, test_user.id, device1.id)
    
    assert device1.binding_status == "unbound"
    assert device1.active_state == "standby"
    
    # Since device1 was active, device2 should be promoted to active
    assert device2.active_state == "active"

@pytest.mark.asyncio
async def test_rename_device(db_session: AsyncSession, test_user: User):
    from app.modules.device.service import rename_device
    device = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="Old", binding_status="bound")
    db_session.add(device)
    await db_session.flush()

    updated = await rename_device(db_session, test_user.id, device.id, "New Name")
    assert updated.device_name == "New Name"

@pytest.mark.asyncio
@patch("app.core.aws.publish_mqtt_message")
@patch("app.modules.device.service.get_redis")
async def test_connectivity_check(mock_get_redis, mock_publish, db_session: AsyncSession, test_user: User):
    from app.modules.device.service import connectivity_check
    device = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="D1", binding_status="bound")
    db_session.add(device)
    await db_session.flush()

    mock_redis = AsyncMock()
    pong_payload = {"rtt_ms": 45}
    mock_redis.get.return_value = json.dumps(pong_payload)
    mock_get_redis.return_value = mock_redis

    res = await connectivity_check(db_session, test_user.id, device.id)

    assert res.verified is True
    assert res.rtt_ms == 45
    mock_publish.assert_called_once()


# ---------------------------------------------------------------------------
# MQTT credential issuance tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_issue_mqtt_credentials_success(db_session: AsyncSession, test_user: User):
    from app.modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="nonce",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.flush()

    resp = await issue_mqtt_credentials(db_session, session.id, test_user.id, "TEST-HW-123")

    assert resp.broker_uri.startswith("mqtts://")
    assert resp.username == "test-hw-123"
    assert resp.password != ""
    assert resp.expires_at > datetime.now(timezone.utc)
    assert session.hardware_id == "test-hw-123"


@pytest.mark.asyncio
async def test_issue_mqtt_credentials_session_not_found(db_session: AsyncSession, test_user: User):
    from app.modules.device.service import issue_mqtt_credentials

    with pytest.raises(ValueError, match="PROVISION_SESSION_NOT_FOUND"):
        await issue_mqtt_credentials(db_session, uuid.uuid4(), test_user.id, "HW")


@pytest.mark.asyncio
async def test_issue_mqtt_credentials_session_expired(db_session: AsyncSession, test_user: User):
    from app.modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="nonce",
        expires_at=datetime.now() - timedelta(minutes=1),
    )
    db_session.add(session)
    await db_session.flush()

    with pytest.raises(ValueError, match="PROVISION_SESSION_EXPIRED"):
        await issue_mqtt_credentials(db_session, session.id, test_user.id, "HW")


@pytest.mark.asyncio
async def test_issue_mqtt_credentials_session_bound(db_session: AsyncSession, test_user: User):
    from app.modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="bound",
        challenge_nonce="nonce",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.flush()

    with pytest.raises(ValueError, match="PROVISION_SESSION_INVALID_STATE"):
        await issue_mqtt_credentials(db_session, session.id, test_user.id, "HW")


@pytest.mark.asyncio
async def test_issue_mqtt_credentials_empty_hardware_id(db_session: AsyncSession, test_user: User):
    from app.modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="nonce",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.flush()

    with pytest.raises(ValueError, match="HARDWARE_ID_EMPTY"):
        await issue_mqtt_credentials(db_session, session.id, test_user.id, "")


# ---------------------------------------------------------------------------
# MQTT token refresh tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_mqtt_token_success():
    from app.core.security import create_mqtt_token
    from app.modules.device.service import refresh_mqtt_token

    token, jti, expire = create_mqtt_token("my-hw-id")
    resp = await refresh_mqtt_token(token)

    assert resp.password != ""
    assert resp.password != token
    assert resp.expires_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_refresh_mqtt_token_within_grace():
    from app.core.config import settings
    from app.core.security import create_mqtt_token
    from app.modules.device.service import refresh_mqtt_token

    # Create a token that expired 30 minutes ago (well within the 1-hour grace window)
    original_expire = settings.mqtt_jwt_expire_minutes
    settings.mqtt_jwt_expire_minutes = -30  # Expired 30 min ago
    try:
        token, jti, _ = create_mqtt_token("my-hw-id")
    finally:
        settings.mqtt_jwt_expire_minutes = original_expire

    resp = await refresh_mqtt_token(token)
    assert resp.password != ""
    assert resp.password != token


@pytest.mark.asyncio
async def test_refresh_mqtt_token_beyond_grace():
    from app.core.config import settings
    from app.core.security import create_mqtt_token
    from app.modules.device.service import refresh_mqtt_token

    # Create a token that expired 2 hours ago (beyond the 1-hour grace window)
    original_expire = settings.mqtt_jwt_expire_minutes
    settings.mqtt_jwt_expire_minutes = -180  # Expired 3 hours ago
    try:
        token, jti, _ = create_mqtt_token("my-hw-id")
    finally:
        settings.mqtt_jwt_expire_minutes = original_expire

    with pytest.raises(ValueError, match="MQTT_TOKEN_EXPIRED_BEYOND_GRACE"):
        await refresh_mqtt_token(token)


@pytest.mark.asyncio
async def test_refresh_mqtt_token_rejects_user_auth_token(test_user: User):
    from app.core.security import create_access_token
    from app.modules.device.service import refresh_mqtt_token

    user_token = create_access_token(str(test_user.id))
    with pytest.raises(ValueError, match="MQTT token"):
        await refresh_mqtt_token(user_token)


@pytest.mark.asyncio
async def test_refresh_mqtt_token_malformed():
    from app.modules.device.service import refresh_mqtt_token

    with pytest.raises(ValueError):
        await refresh_mqtt_token("not-a-valid-jwt")
