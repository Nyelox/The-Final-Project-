import json
import os

_config_path = os.path.join(os.path.dirname(__file__), "client.config.json")

try:
    with open(_config_path, "r") as f:
        _cfg = json.load(f)
    SERVER_IP = _cfg.get("server_ip", "127.0.0.1")
    SERVER_PORT = int(_cfg.get("server_port", 5000))
except Exception:
    SERVER_IP = "127.0.0.1"
    SERVER_PORT = 5000

SERVER_URL = f"http://{SERVER_IP}:{SERVER_PORT}"
