import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db

logger = logging.getLogger(__name__)
from core.deps import get_current_user
from models.user import User
from modules.device import service
from modules.device.schemas import (
    DeviceBindRequest,
    DeviceRenameRequest,
    DeviceResponse,
    DeviceStateRequest,
    LiveKitTokenResponse,
    MqttCredentialRequest,
    MqttCredentialResponse,
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
    from core.rate_limit import check_session_create_limit
    await check_session_create_limit(str(current_user.id))
    return await service.create_provisioning_session(db, current_user.id)


@router.get("/online-status/{hardware_id}")
async def check_online_status(
    hardware_id: str,
    current_user: User = Depends(get_current_user),
):
    """Check if device is online via MQTT (used by App before verify-connectivity)."""
    from modules.device.service import get_device_online_status
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
    from core.rate_limit import check_verify_limit
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
    from core.rate_limit import check_mqtt_creds_limit
    try:
        s_id = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    await check_mqtt_creds_limit(session_id)
    try:
        result = await service.issue_mqtt_credentials(db, s_id, current_user.id, body.hardware_id)
        # Push WS so app knows firmware has fetched its credentials
        try:
            from modules.ws.manager import ws_manager
            import json
            await ws_manager.broadcast(
                body.hardware_id.strip().lower(),
                json.dumps({"type": "credentials_ready", "hardware_id": body.hardware_id.strip().lower()}),
            )
        except Exception:
            pass  # best-effort
        return result
    except ValueError as e:
        error_msg = str(e)
        # Push error via WS so the App knows provisioning failed
        try:
            from modules.ws.manager import ws_manager
            import json
            await ws_manager.broadcast_to_user(
                str(current_user.id),
                json.dumps({"type": "error", "error_code": error_msg,
                            "error_msg": f"MQTT 凭证获取失败: {error_msg}"}),
            )
        except Exception:
            pass
        if error_msg == "PROVISION_SESSION_NOT_FOUND":
            raise HTTPException(status_code=404, detail=error_msg)
        if error_msg == "PROVISION_SESSION_EXPIRED":
            raise HTTPException(status_code=410, detail=error_msg)
        if error_msg == "IOT_RESOURCE_CREATION_FAILED":
            raise HTTPException(status_code=503, detail=error_msg)
        raise HTTPException(status_code=400, detail=error_msg)


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
    current_user: User = Depends(get_current_user),
):
    """Return a long-lived (3650d) token + room info. Hardware stores this
    during provisioning and uses it for all future wake-word joins."""
    try:
        token, room_name = await service.generate_livekit_token(str(current_user.id))
    except Exception as e:
        logger.exception("LiveKit token generation failed for user=%s", current_user.id)
        try:
            from modules.ws.manager import ws_manager
            import json
            await ws_manager.broadcast_to_user(
                str(current_user.id),
                json.dumps({"type": "error", "error_code": "LIVEKIT_TOKEN_FAILED",
                            "error_msg": "LiveKit token 获取失败"}),
            )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to generate LiveKit token")

    from core.config import settings
    return LiveKitTokenResponse(
        token=token,
        room_name=room_name,
        livekit_url=settings.livekit_url,
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


@router.post("/join-room", status_code=204)
async def join_room(
    hw_id: str = Query(..., description="Hardware MAC address"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fallback: ensure room + agent ready. Only the active device can join."""
    try:
        await service.join_room(db=db, user_id=current_user.id, hw_id=hw_id)
    except ValueError as e:
        error_msg = str(e)
        if error_msg == "DEVICE_NOT_FOUND":
            raise HTTPException(status_code=404, detail=error_msg)
        if error_msg == "DEVICE_NOT_ACTIVE":
            raise HTTPException(status_code=403, detail=error_msg)
        raise


@router.get("/room-status")
async def room_status(
    hw_id: str = Query(..., description="Hardware MAC address"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Check if the user's LiveKit room is alive.

    Returns {room_name, alive} where alive=true means the room exists
    and the agent is present. Only the active device can query.
    """
    try:
        return await service.room_status(db=db, user_id=current_user.id, hw_id=hw_id)
    except ValueError as e:
        error_msg = str(e)
        if error_msg == "DEVICE_NOT_FOUND":
            raise HTTPException(status_code=404, detail=error_msg)
        if error_msg == "DEVICE_NOT_ACTIVE":
            raise HTTPException(status_code=403, detail=error_msg)
        raise


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


# ── FCM Push Token ───────────────────────────────────────────────

from pydantic import BaseModel

class FcmTokenRequest(BaseModel):
    token: str
    platform: str | None = None

@router.post("/fcm-token", status_code=201)
async def register_fcm_token(
    body: FcmTokenRequest,
    current_user: User = Depends(get_current_user),
):
    """Register or refresh an FCM push token for the current user."""
    from modules.device.fcm import register_token
    await register_token(current_user.id, body.token, body.platform)
    return {"status": "ok"}
