import uuid
import json
from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession
from models.user import User
from models.device import Device
from models.device_provisioning_session import DeviceProvisioningSession
from modules.device.schemas import DeviceBindRequest
from modules.device.service import (
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
    assert session.expires_at > datetime.now(timezone.utc)

@pytest.mark.asyncio
@patch("core.aws.publish_mqtt_message")
@patch("modules.device.service.redis_get")
async def test_verify_connectivity_success(
    mock_redis_get, mock_publish, db_session: AsyncSession, test_user: User
):
    # 1. Create a session
    session = await create_provisioning_session(db_session, test_user.id)

    # 2. Mock redis_get behavior (return pong immediately)
    pong_payload = {"nonce": session.challenge_nonce, "rtt_ms": 120}
    mock_redis_get.return_value = json.dumps(pong_payload)

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
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)
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
@patch("modules.device.service.redis_exists", return_value=False)
async def test_bind_device_conflict(mock_exists, db_session: AsyncSession, test_user: User):
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
        id=uuid.uuid4(), user_id=test_user.id, status="verified", challenge_nonce="x", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)
    )
    db_session.add(session)
    await db_session.flush()
    
    req = DeviceBindRequest(session_id=session.id, hardware_id="TEST-HW-456", device_name="Stolen")
    
    with pytest.raises(ValueError, match="DEVICE_ALREADY_BOUND"):
        await bind_device(db_session, test_user.id, req)

@pytest.mark.asyncio
@patch("modules.device.service.redis_exists", return_value=False)
async def test_bind_device_soft_unbind_transfer(mock_exists, db_session: AsyncSession, test_user: User):
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
        id=uuid.uuid4(), user_id=test_user.id, status="verified", challenge_nonce="y", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)
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
@patch("modules.device.service.redis_exists")
async def test_get_devices_for_user(mock_exists, db_session: AsyncSession, test_user: User):
    from modules.device.service import get_devices_for_user

    device1 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="D1", binding_status="bound")
    device2 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev2", device_name="D2", binding_status="bound")
    device3 = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev3", device_name="D3", binding_status="unbound")
    db_session.add_all([device1, device2, device3])
    await db_session.flush()

    # dev1 is online, dev2 is offline
    mock_exists.side_effect = lambda key: "dev1" in key

    devices = await get_devices_for_user(db_session, test_user.id)

    assert len(devices) == 2  # unbound device should not be returned
    d1 = next(d for d in devices if d.id == device1.id)
    d2 = next(d for d in devices if d.id == device2.id)

    assert d1.is_online is True
    assert d2.is_online is False

@pytest.mark.asyncio
@patch("modules.device.service.redis_exists", return_value=False)
async def test_set_active_device(mock_exists, db_session: AsyncSession, test_user: User):
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
@patch("modules.device.service.get_devices_for_user")
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
@patch("modules.device.service.redis_exists", return_value=False)
async def test_rename_device(mock_exists, db_session: AsyncSession, test_user: User):
    from modules.device.service import rename_device
    device = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="Old", binding_status="bound")
    db_session.add(device)
    await db_session.flush()

    updated = await rename_device(db_session, test_user.id, device.id, "New Name")
    assert updated.device_name == "New Name"

@pytest.mark.asyncio
@patch("core.aws.publish_mqtt_message")
@patch("modules.device.service.redis_get")
async def test_connectivity_check(mock_redis_get, mock_publish, db_session: AsyncSession, test_user: User):
    from modules.device.service import connectivity_check
    device = Device(id=uuid.uuid4(), user_id=test_user.id, hardware_id="dev1", device_name="D1", binding_status="bound")
    db_session.add(device)
    await db_session.flush()

    pong_payload = {"rtt_ms": 45}
    mock_redis_get.return_value = json.dumps(pong_payload)

    res = await connectivity_check(db_session, test_user.id, device.id)

    assert res.verified is True
    assert res.rtt_ms == 45
    mock_publish.assert_called_once()


# ---------------------------------------------------------------------------
# MQTT credential issuance tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_issue_mqtt_credentials_success(db_session: AsyncSession, test_user: User):
    from modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="nonce",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
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
    from modules.device.service import issue_mqtt_credentials

    with pytest.raises(ValueError, match="PROVISION_SESSION_NOT_FOUND"):
        await issue_mqtt_credentials(db_session, uuid.uuid4(), test_user.id, "HW")


@pytest.mark.asyncio
async def test_issue_mqtt_credentials_session_expired(db_session: AsyncSession, test_user: User):
    from modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="nonce",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db_session.add(session)
    await db_session.flush()

    with pytest.raises(ValueError, match="PROVISION_SESSION_EXPIRED"):
        await issue_mqtt_credentials(db_session, session.id, test_user.id, "HW")


@pytest.mark.asyncio
async def test_issue_mqtt_credentials_session_bound(db_session: AsyncSession, test_user: User):
    from modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="bound",
        challenge_nonce="nonce",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.flush()

    with pytest.raises(ValueError, match="PROVISION_SESSION_INVALID_STATE"):
        await issue_mqtt_credentials(db_session, session.id, test_user.id, "HW")


@pytest.mark.asyncio
async def test_issue_mqtt_credentials_empty_hardware_id(db_session: AsyncSession, test_user: User):
    from modules.device.service import issue_mqtt_credentials

    session = DeviceProvisioningSession(
        id=uuid.uuid4(),
        user_id=test_user.id,
        status="created",
        challenge_nonce="nonce",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.flush()

    with pytest.raises(ValueError, match="HARDWARE_ID_EMPTY"):
        await issue_mqtt_credentials(db_session, session.id, test_user.id, "")




