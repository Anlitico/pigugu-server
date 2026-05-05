from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.modules.device import service
from app.modules.device.schemas import (
    DeviceBindRequest,
    DeviceRenameRequest,
    DeviceResponse,
    DeviceStateRequest,
    LiveKitTokenResponse,
    MqttCredentialRequest,
    MqttCredentialResponse,
    MqttTokenRefreshRequest,
    MqttTokenRefreshResponse,
    ProvisioningSessionResponse,
    VerifyConnectivityRequest,
    VerifyConnectivityResponse,
)

router = APIRouter(prefix="/device", tags=["device"])


@router.post("/provisioning/sessions", response_model=ProvisioningSessionResponse)
async def create_provisioning_session(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.core.rate_limit import check_session_create_limit
    await check_session_create_limit(str(current_user.id))
    return await service.create_provisioning_session(db, current_user.id)


@router.get("/online-status/{hardware_id}")
async def check_online_status(
    hardware_id: str,
    current_user: User = Depends(get_current_user),
):
    """Check if device is online via MQTT (used by App before verify-connectivity)."""
    from app.modules.device.service import get_device_online_status
    is_online = await get_device_online_status(hardware_id)
    return {"hardware_id": hardware_id, "online": is_online}


@router.post("/provisioning/sessions/{session_id}/verify-connectivity", response_model=VerifyConnectivityResponse)
async def verify_connectivity(
    session_id: str,
    body: VerifyConnectivityRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import uuid
    from app.core.rate_limit import check_verify_limit
    try:
        s_id = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    await check_verify_limit(session_id)
    return await service.verify_connectivity(db, s_id, current_user.id, body.hardware_id)


@router.post(
    "/provisioning/sessions/{session_id}/mqtt-credentials",
    response_model=MqttCredentialResponse,
    status_code=201,
)
async def issue_mqtt_creds(
    session_id: str,
    body: MqttCredentialRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import uuid
    from app.core.rate_limit import check_mqtt_creds_limit
    try:
        s_id = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    await check_mqtt_creds_limit(session_id)
    try:
        return await service.issue_mqtt_credentials(db, s_id, current_user.id, body.hardware_id)
    except ValueError as e:
        error_msg = str(e)
        if error_msg == "PROVISION_SESSION_NOT_FOUND":
            raise HTTPException(status_code=404, detail=error_msg)
        if error_msg == "PROVISION_SESSION_EXPIRED":
            raise HTTPException(status_code=410, detail=error_msg)
        raise HTTPException(status_code=400, detail=error_msg)


@router.post(
    "/mqtt-token/refresh",
    response_model=MqttTokenRefreshResponse,
)
async def refresh_mqtt_creds(
    body: MqttTokenRefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await service.refresh_mqtt_token(body.token)
    except ValueError as e:
        error_msg = str(e)
        if error_msg in ("MQTT_TOKEN_EXPIRED_BEYOND_GRACE", "MQTT_TOKEN_MISSING_HW_ID"):
            raise HTTPException(status_code=401, detail=error_msg)
        raise HTTPException(status_code=401, detail="Invalid or expired token")


@router.post("/bind", response_model=DeviceResponse, status_code=201)
async def bind_device(
    body: DeviceBindRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    try:
        return await service.bind_device(db, current_user.id, body)
    except ValueError as e:
        error_msg = str(e)
        if error_msg == "DEVICE_ALREADY_BOUND":
            raise HTTPException(status_code=409, detail=error_msg)
        raise HTTPException(status_code=400, detail=error_msg)


@router.post("/state")
async def report_state(body: DeviceStateRequest, db: AsyncSession = Depends(get_db)):
    await service.update_device_state(body.device_id, body.state)
    return {"status": "ok"}


@router.get("/livekit-token", response_model=LiveKitTokenResponse)
async def get_livekit_token(
    device_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    import uuid
    try:
        d_id = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid device ID")
    
    token, room_name = await service.generate_livekit_token(d_id)
    from app.core.config import settings
    return LiveKitTokenResponse(
        token=token,
        room_name=room_name,
        livekit_url=settings.livekit_url
    )


@router.get("s", response_model=list[DeviceResponse])
async def list_devices(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    return await service.get_devices_for_user(db, current_user.id)


@router.post("/{device_id}/set-active", response_model=DeviceResponse)
async def set_active_device(
    device_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    import uuid
    try:
        d_id = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid device ID")
    
    try:
        return await service.set_active_device(db, current_user.id, d_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{device_id}/connectivity-check", response_model=VerifyConnectivityResponse)
async def connectivity_check(
    device_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    import uuid
    try:
        d_id = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid device ID")
    
    return await service.connectivity_check(db, current_user.id, d_id)


@router.patch("/{device_id}/name", response_model=DeviceResponse)
async def rename_device(
    device_id: str,
    body: DeviceRenameRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    import uuid
    try:
        d_id = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid device ID")
    
    try:
        return await service.rename_device(db, current_user.id, d_id, body.device_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{device_id}", status_code=204)
async def unbind_device(
    device_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    import uuid
    try:
        d_id = uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid device ID")
    
    try:
        await service.unbind_device(db, current_user.id, d_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
