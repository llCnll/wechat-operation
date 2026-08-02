"""Django-side client for the local Supervisor control protocol."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from config.settings import RUNTIME_DIR


class SupervisorUnavailableError(RuntimeError):
    """Raised when Supervisor is absent or its local credentials are unusable."""


class SupervisorClient:
    """Call only the fixed, token-protected local Supervisor API."""

    def __init__(self, runtime_dir: Path = RUNTIME_DIR):
        self.runtime_dir = runtime_dir
        self.root = runtime_dir / "supervisor"
        self._status_payload: dict | None = None

    def status(self) -> dict:
        return self._read_status_payload()["services"]

    def diagnostics(self) -> dict:
        return self._read_status_payload().get("diagnostics", {})

    def _read_status_payload(self) -> dict:
        if self._status_payload is None:
            self._status_payload = self._request("GET", "/v1/status")
        return self._status_payload

    def logs(self, service: str, lines: int = 80) -> list[str]:
        return self._request("GET", f"/v1/logs/{service}?lines={lines}")["lines"]

    def control(self, service: str, action: str) -> dict:
        return self._request("POST", f"/v1/services/{service}/{action}")

    def _request(self, method: str, path: str) -> dict:
        try:
            endpoint = (self.root / "endpoint.json").read_text(encoding="utf-8")
            token = (self.root / "control.token").read_text(encoding="utf-8").strip()
            target = json.loads(endpoint)
            host, port = target["host"], int(target["port"])
        except (FileNotFoundError, KeyError, TypeError, ValueError, OSError) as exc:
            raise SupervisorUnavailableError("Supervisor 未启动或运行态文件不可用") from exc
        try:
            with httpx.Client(trust_env=False, timeout=0.7) as client:
                response = client.request(
                    method,
                    f"http://{host}:{port}{path}",
                    headers={"X-Operations-Supervisor-Token": token},
                )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SupervisorUnavailableError("Supervisor 控制通道不可用") from exc
