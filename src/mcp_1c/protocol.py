from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MANAGER_PROTOCOL_VERSION = "2026.07.02.4"


@dataclass(slots=True)
class BridgeRequest:
    """Command sent from the Python MCP side to the 1C bridge over WebSocket."""

    id: str
    method: str
    params: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"type": "request", "id": self.id, "method": self.method, "params": self.params}


@dataclass(slots=True)
class BridgeResponse:
    """Response returned by the 1C bridge."""

    id: str
    ok: bool
    result: Any = None
    error: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BridgeResponse":
        return cls(
            id=str(data.get("id", "")),
            ok=bool(data.get("ok", False)),
            result=data.get("result"),
            error=data.get("error"),
        )
