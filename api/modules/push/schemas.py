from pydantic import BaseModel


class FCMTokenRegisterRequest(BaseModel):
    token: str
    device_id: str


class PushSendResponse(BaseModel):
    message_id: str
