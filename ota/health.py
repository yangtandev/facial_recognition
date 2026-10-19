"""Loopback-only health; progress comes from the actual GUI and worker loops."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import uuid

from ota.core import read_json, version_key


def build_info(root=None):
    root = Path(root or Path(__file__).resolve().parent.parent)
    try:
        info = read_json(root / "BUILD_INFO.json")
        version_key(info["version"])
        if not info.get("build_id"):
            raise ValueError("Missing build identity")
        return info
    except (OSError, ValueError, KeyError):
        return {"version": "unknown", "build_id": "unknown"}


BUILD_IDENTITY = build_info()


class Health:
    def __init__(self, root=None):
        self.identity = build_info(root) if root is not None else dict(BUILD_IDENTITY)
        self.boot_id = str(uuid.uuid4())
        self.lock = threading.Lock()
        self.checks = {"config": False, "models": False, "index": False,
                       "inference": False, "mqtt_connected": False,
                       "mqtt_subscribed": False, "sync": False}
        self.progress = {}
        self.cameras = []

    def set(self, **checks):
        with self.lock:
            self.checks.update(checks)

    def pulse(self, name):
        with self.lock:
            old = self.progress.get(name, (0, 0))
            self.progress[name] = (old[0] + 1, time.monotonic())

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            names = ["gui"] + [f"{task}:{camera}" for camera in self.cameras
                               for task in ("capture", "detector", "comparison")]
            live = {name: bool(self.progress.get(name) and now - self.progress[name][1] < 15)
                    for name in names}
            core = all(self.checks[name] for name in ("config", "models", "index", "inference")) and all(live.values())
            external = all(self.checks[name] for name in ("mqtt_connected", "mqtt_subscribed", "sync"))
            cameras = {str(camera): bool(self.progress.get(f"frame:{camera}")
                       and now - self.progress[f"frame:{camera}"][1] < 15) for camera in self.cameras}
            return {**self.identity, "boot_id": self.boot_id, "pid": os.getpid(),
                    "observed_at": time.time(), "core_ok": core, "external_ok": external,
                    "camera_ok": all(cameras.values()), "cameras": cameras,
                    "update_ready": core and external,
                    "healthy": core and external and all(cameras.values()), "checks": dict(self.checks),
                    "live": live, "counters": {name: self.progress[name][0] for name in names if name in self.progress}}

    def start(self, port=8765):
        health = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/health":
                    self.send_error(404)
                    return
                body = json.dumps(health.snapshot()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="ota-health").start()
