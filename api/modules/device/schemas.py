from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DeviceBindRequest(BaseModel):
    session_id: uuid.UUID
    hardware_id: str
    device_name: str


class DeviceRenameRequest(BaseModel):
    device_name: str


class DeviceResponse(BaseModel):
    id: uuid.UUID
    device_name: str
    hardware_id: str
    active_state: str
    is_online: bool = False
    last_seen_at: datetime | None = None
    last_rtt_ms: int | None = None
    firmware: DeviceFirmwareSummary | None = None

    model_config = {"from_attributes": True}


class ProvisioningSessionResponse(BaseModel):
    id: uuid.UUID
    challenge_nonce: str
    expires_at: datetime
    status: str

    model_config = {"from_attributes": True}


class VerifyConnectivityRequest(BaseModel):
    hardware_id: str | None = None


class VerifyConnectivityResponse(BaseModel):
    verified: bool
    rtt_ms: int | None = None
    error_code: str | None = None


class LiveKitTokenResponse(BaseModel):
    token: str
    room_name: str
    livekit_url: str


class AgentConfigResponse(BaseModel):
    """xiaozhi WebSocket config for firmware provisioning."""
    ws_url: str
    token: str


class DeviceStateRequest(BaseModel):
    device_id: str
    state: str  # listening | thinking | speaking | idle


class DeviceVolumeSetRequest(BaseModel):
    volume: int = Field(ge=0, le=100)


class DeviceVolumeResponse(BaseModel):
    """The device's speaker level as the App should render it.

    ``volume`` is None when the device has never reported one — the App must
    show "unknown" rather than 0, which would read as muted.
    ``stale`` is True when this is a remembered value rather than a fresh
    answer from the device, so the App can say how old it is instead of
    implying it is live.
    """
    volume: int | None = None
    stale: bool = True
    synced_at: int | None = None


class MqttCredentialRequest(BaseModel):
    hardware_id: str


class MqttCredentialResponse(BaseModel):
    broker_uri: str
    client_cert: str
    client_key: str
    ws_url: str = ""
    ws_version: int = 1


# ── Firmware OTA ─────────────────────────────────────────────

class DeviceFirmwareSummary(BaseModel):
    current_version: str | None = None
    update_available: bool = False
    target_version: str | None = None
    status: str | None = None
    progress_pct: int | None = None


class FirmwareUpdateDetail(BaseModel):
    target_version: str | None = None
    release_notes: str | None = None
    published_at: datetime | None = None
    force: bool = False
    status: str | None = None
    progress_pct: int | None = None


class DeviceFirmwareDetailResponse(BaseModel):
    current_version: str | None = None
    update: FirmwareUpdateDetail | None = None


class FirmwareUpgradeRequest(BaseModel):
    firmware_version_id: uuid.UUID | None = None  # default = latest released
    force: bool = False  # internal-only (secret header required)


class FirmwareUpgradeResponse(BaseModel):
    job_id: uuid.UUID
    device_id: uuid.UUID
    firmware_version_id: uuid.UUID
    status: str
    requested_at: datetime


class FirmwareVersionView(BaseModel):
    id: uuid.UUID
    board_target: str
    version: str
    git_sha: str | None = None
    file_key: str
    size_bytes: int
    sha256: str
    release_notes: str | None = None
    visibility: str
    released_by: str | None = None
    released_at: datetime | None = None
    created_at: datetime | None = None  # None only for a row created this request (server default)

    model_config = {"from_attributes": True}


class FirmwareUploadRequest(BaseModel):
    version: str
    board_target: str = "lichuang-dev"
    git_sha: str | None = None
    file_key: str
    size_bytes: int
    sha256: str
    signature: str
    release_notes: str | None = None
    released_by: str | None = None
