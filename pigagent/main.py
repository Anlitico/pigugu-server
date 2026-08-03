# pigagent/main.py
"""Pigugu Voice Agent — entry point.

Runs two servers:
  - WebSocket (xiaozhi protocol) on MAIN_PORT (default 8080) — websockets library
  - HTTP REST API (roast inject, health) on API_PORT (default 8081) — FastAPI
"""

import asyncio
import os

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
load_dotenv(find_dotenv(filename=".env.local"), override=True)

import bootstrap.logging  # noqa: F401
from bootstrap.factory import validate_configuration

if __name__ == "__main__":
    if not validate_configuration():
        import sys
        sys.exit(1)

    ws_port = int(os.getenv("MAIN_PORT", "8080"))
    api_port = int(os.getenv("API_PORT", "8081"))

    from voice.server import run_server as run_ws_server

    async def main():
        # Start WebSocket server (xiaozhi protocol)
        ws_task = asyncio.ensure_future(run_ws_server("0.0.0.0", ws_port))

        # Start FastAPI REST API server
        import uvicorn
        api_cfg = uvicorn.Config("app:app", host="0.0.0.0", port=api_port, log_level="info")
        api_server = uvicorn.Server(api_cfg)
        api_task = asyncio.ensure_future(api_server.serve())

        await asyncio.gather(ws_task, api_task)

    asyncio.run(main())
