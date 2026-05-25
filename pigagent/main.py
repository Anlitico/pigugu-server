# pigagent/main.py
"""Pigugu Voice Agent — CLI entry point.

All session wiring lives in lk/session.py.
All business logic lives in pigagent.py.
"""

from dotenv import load_dotenv
load_dotenv()

import bootstrap.logging  # noqa: F401 — must be after load_dotenv()
from lk.entrypoint import main

if __name__ == "__main__":
    main()
