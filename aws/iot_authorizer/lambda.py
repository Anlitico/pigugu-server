"""
AWS IoT Custom Authorizer Lambda — validates MQTT JWT tokens.
"""
import json
import os
import base64
import jwt

SECRET_KEY = os.environ["MQTT_JWT_SECRET_KEY"]
ALGORITHM = os.environ.get("MQTT_JWT_ALGORITHM", "HS256")

# IoT policy attached to authenticated devices
IOT_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": "iot:Connect", "Resource": "*"},
        {"Effect": "Allow", "Action": "iot:Publish",
         "Resource": "arn:aws:iot:us-west-1:*:topic/pgg/dev/*/d2c"},
        {"Effect": "Allow", "Action": "iot:Subscribe",
         "Resource": "arn:aws:iot:us-west-1:*:topicfilter/pgg/dev/*/c2d"},
        {"Effect": "Allow", "Action": "iot:Receive",
         "Resource": "arn:aws:iot:us-west-1:*:topic/pgg/dev/*/c2d"},
    ]
}


def lambda_handler(event, context):
    print("Event:", json.dumps(event))

    try:
        mqtt_data = event.get("protocolData", {}).get("mqtt", {})
        pwd_b64 = mqtt_data.get("password", "")

        if not pwd_b64:
            return {"isAuthenticated": False}

        # AWS IoT base64-encodes MQTT password per official docs
        token = base64.b64decode(pwd_b64).decode("utf-8")

        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM],
                             options={"verify_exp": True})

        hw_id = payload.get("hw_id") or payload.get("sub", "")
        if not hw_id:
            return {"isAuthenticated": False}

        if payload.get("token_type") != "mqtt":
            return {"isAuthenticated": False}

        return {
            "isAuthenticated": True,
            "principalId": hw_id,
            "disconnectAfterInSeconds": 86400,
            "refreshAfterInSeconds": 300,
            "policyDocuments": [json.dumps(IOT_POLICY)],
        }

    except Exception as e:
        print(f"Auth failed: {e}")
        return {"isAuthenticated": False}
