import uuid
from datetime import datetime

from pydantic import BaseModel


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


class DeviceStateRequest(BaseModel):
    device_id: str
    state: str  # listening | thinking | speaking | idle


class MqttCredentialRequest(BaseModel):
    hardware_id: str


class MqttCredentialResponse(BaseModel):
    broker_uri: str
    client_cert: str
    client_key: str


