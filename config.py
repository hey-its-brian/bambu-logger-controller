"""Load configuration from environment variables or .env file."""

import os
import sys

def _load_dotenv():
    """Parse .env file if it exists (simple key=value format)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if not os.environ.get(key):
                os.environ[key] = value

_load_dotenv()

BAMBU_IP = os.environ.get("BAMBU_IP", "")
BAMBU_SERIAL = os.environ.get("BAMBU_SERIAL", "")
BAMBU_ACCESS_CODE = os.environ.get("BAMBU_ACCESS_CODE", "")

def validate():
    """Check that all required config values are set."""
    missing = []
    if not BAMBU_IP:
        missing.append("BAMBU_IP")
    if not BAMBU_SERIAL:
        missing.append("BAMBU_SERIAL")
    if not BAMBU_ACCESS_CODE:
        missing.append("BAMBU_ACCESS_CODE")
    if missing:
        print(f"Error: Missing required config: {', '.join(missing)}")
        print("Set them as environment variables or in a .env file.")
        print("See .env.example for the template.")
        sys.exit(1)
