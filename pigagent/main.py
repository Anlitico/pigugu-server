# pigagent/main.py
"""Pigugu Voice Agent  -  entry point.

Starts the HTTP API server and xiaozhi WebSocket server in the same process.
"""

import os
import threading

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
# Override with local dev values if present
load_dotenv(find_dotenv(filename=".env.local"), override=True)

import bootstrap.logging  # noqa: F401  -  must be after load_dotenv()
from bootstrap.factory import validate_configuration

if __name__ == "__main__":
    if not validate_configuration():
        import sys
        sys.exit(1)

    # Start FastAPI HTTP server with xiaozhi WS endpoint
    import uvicorn

    api_port = int(os.getenv("API_PORT", "8080"))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=api_port,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=10,
        reload=True,
        reload_dirs=["/pigagent"],
    )
