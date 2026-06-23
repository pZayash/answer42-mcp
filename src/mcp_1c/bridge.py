from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServer, WebSocketServerProtocol

from .protocol import BridgeRequest, BridgeResponse

LOGGER = logging.getLogger(__name__)


class BridgeError(RuntimeError):
    """Raised when the 1C bridge is not connected or returns an error."""


@dataclass(slots=True)
class ConnectedBridge:
    websocket: WebSocketServerProtocol
    peer: str
    pending: dict[str, asyncio.Future[BridgeResponse]] = field(default_factory=dict)


class OneCBridgeServer:
    """JSON-RPC-like WebSocket server for the embedded 1C manager processor."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, request_timeout: float = 60.0):
        self.host = host
        self.port = port
        self.request_timeout = request_timeout
        self._server: WebSocketServer | None = None
        self._bridge: ConnectedBridge | None = None
        self._lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/bridge"

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await websockets.serve(self._handler, self.host, self.port)
        LOGGER.info("1C WebSocket bridge is listening on %s", self.url)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def status(self) -> dict[str, Any]:
        bridge = self._bridge
        return {
            "websocket_url": self.url,
            "connected": bridge is not None,
            "peer": bridge.peer if bridge else None,
            "pending": len(bridge.pending) if bridge else 0,
        }

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        bridge = self._bridge
        if bridge is None:
            raise BridgeError("1C bridge is not connected. Start the 1C manager first.")

        request_id = str(uuid.uuid4())
        future: asyncio.Future[BridgeResponse] = asyncio.get_running_loop().create_future()
        bridge.pending[request_id] = future
        request = BridgeRequest(id=request_id, method=method, params=params or {})

        try:
            await bridge.websocket.send(json.dumps(request.to_json(), ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        finally:
            bridge.pending.pop(request_id, None)

        if not response.ok:
            raise BridgeError(response.error or f"1C bridge command {method!r} failed")
        return response.result

    async def _handler(self, websocket: WebSocketServerProtocol) -> None:
        peer = f"{websocket.remote_address}"
        connected = ConnectedBridge(websocket=websocket, peer=peer)

        async with self._lock:
            if self._bridge is not None:
                await websocket.close(code=1013, reason="Only one 1C bridge connection is supported")
                return
            self._bridge = connected

        try:
            async for raw_message in websocket:
                await self._process_message(connected, raw_message)
        except ConnectionClosed:
            # Normal during session teardown: 1C process may disappear before the
            # websocket close frame is exchanged.  Pending callers still get a
            # BridgeError in finally below.
            pass
        finally:
            async with self._lock:
                if self._bridge is connected:
                    self._bridge = None
            for future in connected.pending.values():
                if not future.done():
                    future.set_exception(BridgeError("1C bridge disconnected"))
            LOGGER.info("WebSocket bridge %s disconnected", peer)

    async def _process_message(self, bridge: ConnectedBridge, raw_message: str | bytes) -> None:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring non-JSON bridge message: %r", raw_message)
            return

        message_type = data.get("type")
        if message_type in {"hello", "event"}:
            LOGGER.info("1C bridge %s: %s", message_type, data)
            return
        if message_type != "response":
            LOGGER.warning("Ignoring unknown bridge message: %s", data)
            return

        response = BridgeResponse.from_json(data)
        future = bridge.pending.get(response.id)
        if future is None:
            LOGGER.warning("Response for unknown request id %s: %s", response.id, data)
            return
        if not future.done():
            future.set_result(response)
