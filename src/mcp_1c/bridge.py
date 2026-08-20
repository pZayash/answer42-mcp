from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServer, WebSocketServerProtocol

from .protocol import BridgeRequest, BridgeResponse

LOGGER = logging.getLogger(__name__)
PROGRESS_GRACE_TIMEOUT = float(os.getenv("ONEC_MCP_PROGRESS_GRACE_TIMEOUT", "10"))


class BridgeError(RuntimeError):
    """Raised when the 1C bridge is not connected or returns an error."""


class BridgeTimeoutError(BridgeError):
    """Raised when a 1C bridge command does not return before its deadline."""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        timeout: float,
        progress: dict[str, Any] | None = None,
        progress_history: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.timeout = timeout
        self.progress = progress
        self.progress_history = progress_history or []


def _bridge_error_text(response: BridgeResponse, method: str) -> str:
    """Return a non-empty error text for a failed bridge response."""
    error = response.error.strip() if isinstance(response.error, str) else ""
    return error or f"1C bridge command {method!r} failed without error text"


@dataclass(slots=True)
class ConnectedBridge:
    websocket: WebSocketServerProtocol
    peer: str
    pending: dict[str, asyncio.Future[BridgeResponse]] = field(default_factory=dict)
    progress: dict[str, dict[str, Any]] = field(default_factory=dict)
    progress_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class OneCBridgeServer:
    """JSON-RPC-like WebSocket server for the embedded 1C manager processor."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, request_timeout: float = 180.0):
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

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        bridge = self._bridge
        if bridge is None:
            raise BridgeError("1C bridge is not connected. Start the 1C manager first.")

        request_timeout = self.request_timeout if timeout is None else timeout
        request_id = str(uuid.uuid4())
        future: asyncio.Future[BridgeResponse] = asyncio.get_running_loop().create_future()
        bridge.pending[request_id] = future
        bridge.progress.pop(request_id, None)
        bridge.progress_history.pop(request_id, None)
        request = BridgeRequest(id=request_id, method=method, params=params or {})

        try:
            await bridge.websocket.send(json.dumps(request.to_json(), ensure_ascii=False))
            response = await self._wait_response_with_progress_grace(
                bridge,
                request_id,
                method,
                future,
                request_timeout,
            )
        finally:
            bridge.pending.pop(request_id, None)
            bridge.progress.pop(request_id, None)
            bridge.progress_history.pop(request_id, None)

        if not response.ok:
            raise BridgeError(_bridge_error_text(response, method))
        return response.result

    async def _wait_response_with_progress_grace(
        self,
        bridge: ConnectedBridge,
        request_id: str,
        method: str,
        future: asyncio.Future[BridgeResponse],
        request_timeout: float,
    ) -> BridgeResponse:
        """Wait for a response, extending the deadline briefly after fresh progress.

        1C-side handlers send progress events between expensive UI/test-client
        steps.  The public watchdog timeout is a user-facing deadline for an
        apparently silent operation, not a reason to cancel a request a few
        seconds after the manager reported that it had just completed a step.
        ``asyncio.wait_for`` cancels the future on timeout, so use ``shield``
        and make the final timeout decision ourselves.
        """
        started = time.monotonic()
        absolute_deadline = started + request_timeout
        while True:
            progress = bridge.progress.get(request_id)
            deadline = _effective_timeout_deadline(absolute_deadline, progress)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                progress_history = list(bridge.progress_history.get(request_id) or [])
                suffix = _format_timeout_progress(progress)
                raise BridgeTimeoutError(
                    f"1C bridge command {method!r} timed out after {request_timeout:g}s without response{suffix}",
                    method=method,
                    timeout=request_timeout,
                    progress=progress,
                    progress_history=progress_history,
                )
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
            except TimeoutError:
                if future.done():
                    return future.result()
                continue

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


    def _record_event_progress(self, bridge: ConnectedBridge, data: dict[str, Any]) -> None:
        request_id = str(data.get("id") or "")
        if not request_id or request_id not in bridge.pending:
            return
        if str(data.get("event") or "") != "progress":
            return
        progress = dict(data)
        progress["received_monotonic"] = time.monotonic()
        bridge.progress[request_id] = progress
        history = bridge.progress_history.setdefault(request_id, [])
        history.append(progress)
        if len(history) > 20:
            del history[:-20]

    async def _process_message(self, bridge: ConnectedBridge, raw_message: str | bytes) -> None:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            LOGGER.warning("Ignoring non-JSON bridge message: %r", raw_message)
            return

        message_type = data.get("type")
        if message_type == "hello":
            LOGGER.info("1C bridge %s: %s", message_type, data)
            return
        if message_type == "event":
            self._record_event_progress(bridge, data)
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


def _format_timeout_progress(progress: dict[str, Any] | None) -> str:
    if not progress:
        return ""
    phase = str(progress.get("phase") or "")
    method = str(progress.get("method") or "")
    elapsed_ms = progress.get("elapsed_ms")
    age = time.monotonic() - float(progress.get("received_monotonic") or time.monotonic())
    parts = []
    if phase:
        parts.append(f"phase={phase!r}")
    if method:
        parts.append(f"method={method!r}")
    if elapsed_ms is not None:
        parts.append(f"elapsed_ms={elapsed_ms}")
    parts.append(f"last_progress_age={age:.1f}s")
    return "; last progress: " + ", ".join(parts)


def _effective_timeout_deadline(absolute_deadline: float, progress: dict[str, Any] | None) -> float:
    if not progress:
        return absolute_deadline
    try:
        received = float(progress.get("received_monotonic") or 0.0)
    except (TypeError, ValueError):
        received = 0.0
    if received <= 0:
        return absolute_deadline
    return max(absolute_deadline, received + PROGRESS_GRACE_TIMEOUT)
