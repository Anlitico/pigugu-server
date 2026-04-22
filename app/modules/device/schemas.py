from pydantic import BaseModel


class DeviceBindRequest(BaseModel):
    device_name: str


class DeviceRenameRequest(BaseModel):
    device_name: str


class DeviceResponse(BaseModel):
    id: str
    device_name: str
    livekit_room_name: str | None
    is_online: bool = False

    model_config = {"from_attributes": True}


class LiveKitTokenResponse(BaseModel):
    token: str
    room_name: str
    livekit_url: str


class DeviceStateRequest(BaseModel):
    device_id: str
    state: str  # listening | thinking | speaking | idle
