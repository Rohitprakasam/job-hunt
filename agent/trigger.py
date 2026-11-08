"""HTTP trigger daemon for starting hourly job agent cycles via private port."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent import lock, paths

log = logging.getLogger(__name__)

MIN_INTERVAL_SECONDS = 20 * 60  # at least 20 minutes between cycle starts


def _last_start_file() -> Path:
    return paths.agent_home() / "data" / "last_cycle_start.txt"


def get_last_start_time() -> float:
    p = _last_start_file()
    if not p.exists():
        return 0.0
    try:
        return float(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0.0


def set_last_start_time(ts: float) -> None:
    p = _last_start_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(ts), encoding="utf-8")


def is_cycle_busy() -> bool:
    if lock.is_locked():
        return True
    last_start = get_last_start_time()
    if (time.time() - last_start) < MIN_INTERVAL_SECONDS:
        return True
    return False


def dispatch_cycle() -> bool:
    if is_cycle_busy():
        return False

    set_last_start_time(time.time())

    log_dir = paths.agent_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "cycle.log"

    if os.name == "nt":
        subprocess.Popen(
            [sys.executable, "-m", "agent.cli", "run-once", "cycle"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=open(log_file, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
    else:
        cmd = f"setsid nohup agent run-once cycle >> {log_file} 2>&1 < /dev/null &"
        subprocess.Popen(["bash", "-c", cmd], start_new_session=True)

    return True


class TriggerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # Suppress default BaseHTTPRequestHandler stderr logging
        pass

    def do_POST(self) -> None:
        client_ip = self.client_address[0] if self.client_address else "unknown"
        now_iso = datetime.now(UTC).isoformat()

        if self.path != "/cycle":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            sys.stderr.write(f"[{now_iso}] {client_ip} POST {self.path} -> 404\n")
            sys.stderr.flush()
            return

        started = dispatch_cycle()
        status_word = "started" if started else "busy"

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = json.dumps({"status": status_word}).encode("utf-8")
        self.wfile.write(body)

        sys.stderr.write(f"[{now_iso}] {client_ip} POST /cycle -> {status_word}\n")
        sys.stderr.flush()

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"not_found"}')


def run_trigger_server(host: str = "127.0.0.1", port: int = 8787) -> None:
    server = HTTPServer((host, port), TriggerHandler)
    sys.stderr.write(f"Trigger server listening on http://{host}:{port}/cycle\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
