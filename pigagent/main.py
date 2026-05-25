# pigagent/main.py
"""Pigugu Voice Agent  -  CLI entry point.

Starts the HTTP API server and LiveKit workers in the same process.
All session wiring in lk/session.py, all business logic in pigagent.py.
"""

import os
import threading

from dotenv import load_dotenv
load_dotenv()

import bootstrap.logging  # noqa: F401  -  must be after load_dotenv()
from lk.entrypoint import main as lk_main

if __name__ == "__main__":
    # Start FastAPI HTTP server in background thread
    import uvicorn
    from api.server import create_app

    api_port = int(os.getenv("API_PORT", "8080"))
    http_thread = threading.Thread(
        target=uvicorn.run,
        args=(create_app(),),
        kwargs={"host": "0.0.0.0", "port": api_port, "log_level": "info"},
        daemon=True,
    )
    http_thread.start()

    # Start LiveKit workers (blocks)
    lk_main()
