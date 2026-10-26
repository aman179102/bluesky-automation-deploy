from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.config import ROOT, load_settings
from app.remote_state import validate_remote_config
from app.scheduler import main as scheduler_main

logger = logging.getLogger("server")


def _health_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "bluesky-automation",
        "time": datetime.now(timezone.utc).isoformat(),
    }
    schedule_path = Path(os.getenv("STATE_DIR", str(ROOT / "state"))) / "scheduler.json"
    try:
        schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
        payload["schedule_date"] = schedule.get("date")
        pending = [
            event
            for event in schedule.get("events", [])
            if not event.get("done")
        ]
        payload["pending_events"] = len(pending)
        if pending:
            upcoming = min(pending, key=lambda event: str(event.get("at") or ""))
            payload["next_event"] = {
                "kind": upcoming.get("kind"),
                "at": upcoming.get("at"),
            }
    except (OSError, ValueError, TypeError):
        payload["schedule_date"] = None
    return payload


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "BlueskyHealth/1.0"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] in {"/", "/health", "/healthz"}:
            self._send(200, _health_payload())
        else:
            self._send(404, {"status": "not_found"})

    def do_HEAD(self) -> None:
        if self.path.split("?", 1)[0] in {"/", "/health", "/healthz"}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("health request: " + format, *args)


def _start_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    return server


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ok, message = validate_remote_config()
    if not ok:
        logger.error("Invalid remote state configuration: %s", message)
        return 1
    logger.info("State backend: %s", message)
    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError:
        logger.error("PORT must be an integer")
        return 1
    settings = load_settings()
    try:
        settings.require_bluesky()
    except Exception as exc:
        logger.error("Invalid Bluesky configuration: %s", exc)
        return 1
    try:
        server = _start_health_server(port)
    except OSError as exc:
        logger.error("Could not bind health server on port %s: %s", port, exc)
        return 1
    logger.info("Health server listening on 0.0.0.0:%s", port)
    try:
        return scheduler_main([])
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
