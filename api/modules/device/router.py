import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db

logger = logging.getLogger(__name__)
from core.deps import get_current_user
from models.device import Device
from models.user import User
from modules.device import service
from modules.device.schemas import (
    DeviceBindRequest,
    DeviceRenameRequest,
    DeviceResponse,
    DeviceStateRequest,
    AgentConfigResponse,
    MqttCredentialRequest,
    MqttCredentialResponse,
    ProvisioningSessionResponse,
    VerifyConnectivityRequest,
    VerifyConnectivityResponse,
    DeviceFirmwareDetailResponse,
    FirmwareUpgradeRequest,
    FirmwareUpgradeResponse,
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
            await ws_manager.broadcast_to_user(
                str(current_user.id),
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


@router.get("/agent-config", response_model=AgentConfigResponse)
async def get_agent_config(
    current_user: User = Depends(get_current_user),
):
    """Return xiaozhi WS URL + auth token. Firmware stores these in NVS
    and connects directly to the WebSocket endpoint."""
    try:
        config = await service.generate_agent_config(str(current_user.id))
    except Exception:
        logger.exception("Agent config generation failed for user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="Failed to generate agent config")
    return AgentConfigResponse(**config)


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

# ── OTA / Provisioning (xiaozhi firmware) ─────────────────────────

@router.post("/ota")
async def get_ota_config(request: Request, db: AsyncSession = Depends(get_db)):
    """Return provisioning config + (OTA-enabled) firmware directive.

    Firmware calls this after WiFi connect / on ota.check / periodically. It
    reports its own version (Firmware-Version / Firmware-Git headers), which we
    persist, then we decide whether an active upgrade job targets this device.

    Unauthenticated — identified by Device-Id / Client-Id headers (status quo).
    Only OTA-enabled firmware is affected: the websocket block is unchanged and
    the firmware/server_time blocks are new, additive fields.
    """
    from datetime import datetime, timezone as _tz

    from core.security import create_access_token
    from sqlalchemy import select

    # Device-Id arrives as the xiaozhi-style colon MAC; devices.hardware_id is
    # stored colon-less (12 hex). Normalize so the row always matches.
    device_id = (
        (request.headers.get("device-id") or request.headers.get("Device-Id", ""))
        .replace(":", "")
        .strip()
        .lower()
    )
    client_id = (request.headers.get("client-id") or request.headers.get("Client-Id", "")).strip()

    ws_url = getattr(settings, "ws_url", "wss://api.pigugu.net/v1/agent")
    token = create_access_token(subject=client_id or device_id)

    payload = {
        "websocket": {
            "url": ws_url,
            "token": token,
            "version": 1,
        },
        "server_time": {
            "timestamp": int(datetime.now(_tz.utc).timestamp()),
            "timezone_offset": 0,
        },
    }

    # Version report + firmware pushdown (OTA-enabled devices only).
    if device_id:
        try:
            from models.device import Device
            from modules.device.ota import process_check

            result = await db.execute(select(Device).where(Device.hardware_id.ilike(device_id)))
            device = result.scalar_one_or_none()
            if device is not None:
                firmware = await process_check(
                    db, device,
                    request.headers.get("Firmware-Version"),
                    request.headers.get("Firmware-Git"),
                )
                if firmware is not None:
                    payload["firmware"] = firmware
        except Exception:
            logger.exception("OTA check processing failed for device=%s", device_id)

    logger.info("OTA config: device=%s client=%s url=%s", device_id, client_id, ws_url)
    return payload


# ── Firmware upgrade (App / internal) ─────────────────────────────

async def _resolve_owned_device(
    db: AsyncSession, user_id, device_id: str, require_internal: bool = False
) -> Device:
    import uuid as _uuid

    try:
        d_id = _uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid device ID")
    stmt = select(Device).where(Device.id == d_id)
    if not require_internal:
        stmt = stmt.where(Device.user_id == user_id)
    result = await db.execute(stmt)
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="DEVICE_NOT_FOUND")
    return device


@router.get("/{device_id}/firmware", response_model=DeviceFirmwareDetailResponse)
async def get_device_firmware(
    device_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upgrade-page detail for the App: current version + whether an update is due."""
    from modules.device.ota import get_device_firmware_detail

    device = await _resolve_owned_device(db, current_user.id, device_id)
    return await get_device_firmware_detail(db, device)


@router.post("/{device_id}/firmware/upgrade", response_model=FirmwareUpgradeResponse)
async def trigger_firmware_upgrade(
    device_id: str,
    body: FirmwareUpgradeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """App-triggered upgrade (or internal force/draft target with secret header).

    Creates a requested job (works even if the device is offline → reservation),
    then nudges the device to check immediately via c2d (no-op if offline).
    """
    from modules.device.iot import notify_ota_check
    from modules.device.ota import create_upgrade_job

    # Fails closed: without a configured secret there IS no internal path.
    internal = bool(settings.ota_internal_secret) and (
        request.headers.get("x-ota-internal-secret", "") == settings.ota_internal_secret
    )
    device = await _resolve_owned_device(db, current_user.id, device_id, require_internal=internal)

    if body.force and not internal:
        raise HTTPException(status_code=403, detail="FORCE_FORBIDDEN")

    requested_by = f"script:{current_user.email}" if internal else str(current_user.id)
    try:
        job = await create_upgrade_job(
            db, device,
            firmware_version_id=body.firmware_version_id,
            force=body.force,
            requested_by=requested_by,
        )
    except ValueError as e:
        code = str(e)
        if code == "OTA_JOB_ACTIVE":
            raise HTTPException(status_code=409, detail=code)
        raise HTTPException(status_code=404, detail=code)

    # Commit BEFORE nudging the device so a fast check can never race the
    # uncommitted job (get_db commits again at teardown — harmless no-op).
    await db.commit()
    await notify_ota_check(device.hardware_id)
    return FirmwareUpgradeResponse(
        job_id=job.id,
        device_id=device.id,
        firmware_version_id=job.firmware_version_id,
        status=job.status,
        requested_at=job.requested_at,
    )


# ── FCM Push Token ───────────────────────────────────────────────

@router.post("/fcm-token", status_code=201)
async def register_fcm_token(
    body: FcmTokenRequest,
    current_user: User = Depends(get_current_user),
):
    """Register or refresh an FCM push token for the current user."""
    from modules.device.fcm import register_token
    await register_token(current_user.id, body.token, body.platform)
    return {"status": "ok"}
