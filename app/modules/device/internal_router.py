import json
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request

from app.core.config import settings
from app.core.redis import get_redis

router = APIRouter(prefix="/internal/aws-iot", tags=["internal", "aws-iot"])


@router.post("/webhook")
@router.get("/webhook")  # AWS IoT Rule sends subscription confirmation via GET
async def aws_iot_webhook(
    request: Request,
    payload: dict | None = None,
    x_aws_secret: str | None = Header(None, alias="x-aws-secret")
):
    # 1. Verify Secret (skip for GET confirmation requests which don't include headers)
    secret_valid = x_aws_secret == settings.aws_iot_webhook_secret

    # 2. Handle AWS IoT Rule subscription confirmation
    # AWS IoT sends a confirmation request when a rule is created/updated.
    # The request body contains a "Type" field set to "SubscriptionConfirmation".
    if payload and payload.get("Type") == "SubscriptionConfirmation":
        token = payload.get("Token", "")
        if token:
            # Echo the token back to confirm
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(token)
        # No token but still confirmation type — return 200 to accept
        return {"status": "ok"}

    # GET requests from AWS IoT for confirmation have a ?token= query param
    token = request.query_params.get("token")
    if token:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(token)

    if not secret_valid:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 3. Extract Topic
    # AWS IoT rules can pass topic in headers or payload depending on config.
    # For now, we assume it's passed in the payload by our rule SQL:
    # SELECT * AS payload, topic() AS topic
    topic = payload.get("topic") if payload else None
    if not topic:
        # Fallback to header if configured that way
        topic = request.headers.get("x-amz-sns-topic-arn", "")

    if not topic:
        raise HTTPException(status_code=400, detail="Missing topic")
        
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != "pgg" or parts[1] != "dev":
        raise HTTPException(status_code=400, detail="Invalid topic format")
        
    hw_id = parts[2]
    
    # Payload is either directly payload, or nested if we used AS payload
    msg = payload.get("payload", payload)
    
    msg_type = msg.get("msg_type")
    request_id = msg.get("request_id")
    session_id = msg.get("session_id")

    redis = await get_redis()

    if msg_type == "connectivity.pong":
        if session_id and request_id:
            msg["rtt_ms"] = int((datetime.now().timestamp() - msg.get("ts", 0)) * 1000)
            await redis.set(
                f"provision:verify:{session_id}:{request_id}",
                json.dumps(msg),
                ex=300
            )
        
        if request_id:
             await redis.set(
                f"device:connectivity:hw:{hw_id}:{request_id}",
                json.dumps(msg),
                ex=300
            )

    elif msg_type in ("device.heartbeat", "device.online"):
        await redis.set(f"device:online:hw:{hw_id}", "1", ex=90)
        await redis.set(f"device:last_seen:hw:{hw_id}", str(datetime.now().isoformat()), ex=86400)

    return {"status": "ok"}
