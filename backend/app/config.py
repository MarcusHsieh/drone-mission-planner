"""Settings, read once from the environment (and backend/.env if present)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# The one place the agent's model is chosen. Haiku keeps the demo cheap;
# set AGENT_MODEL=claude-sonnet-5-5 in .env to try a stronger model.
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-haiku-5-5")
AGENT_EFFORT = os.getenv("AGENT_EFFORT", "medium")  # low | medium | high
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "8"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
W3W_API_KEY = os.getenv("W3W_API_KEY", "")
# Optional: Google Map Tiles API key for photorealistic 3D tiles in the 3D view.
# It is sent to the browser, so restrict it to the Map Tiles API and to
# http://localhost:8000/* and http://localhost:5173/* in Google Cloud.
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# Simulated flight runs faster than real time so a demo flight takes about a minute.
SIM_SPEEDUP = float(os.getenv("SIM_SPEEDUP", "5"))

# Which drone the Fly button flies: "sim" (the built-in point-mass sim) or
# "sitl" (ArduPilot SITL over MAVLink, see docs/concepts/sitl.md).
FLIGHT_BACKEND = os.getenv("FLIGHT_BACKEND", "sim").strip().lower()
# Where SITL's MAVLink port is. SITL's first TCP port is 5760; a ground
# control station can use the second one (5762) at the same time.
SITL_CONNECTION = os.getenv("SITL_CONNECTION", "tcp:127.0.0.1:5760")

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "var"))
