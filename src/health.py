"""Zero-dependency async health check server.

Uses asyncio.start_server (raw TCP) to respond with a JSON health status
to any HTTP request. No web framework needed for a single endpoint.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class HealthServer:
    """Async TCP server that responds 200 JSON to any HTTP request."""

    def __init__(self, port: int = 8080):
        self._port = port
        self._server: asyncio.Server | None = None
        self._started_at: str = datetime.now(timezone.utc).isoformat()
        self._last_message_at: str | None = None
        self._messages_processed: int = 0

    def record_message(self) -> None:
        """Call after each processed message to update counters."""
        self._messages_processed += 1
        self._last_message_at = datetime.now(timezone.utc).isoformat()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle an incoming TCP connection as an HTTP request."""
        try:
            # Read the request (we don't care about its content)
            await reader.read(4096)

            body = json.dumps({
                "status": "ok",
                "started_at": self._started_at,
                "last_message_at": self._last_message_at,
                "messages_processed": self._messages_processed,
            })

            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
                f"{body}"
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self) -> None:
        """Start the health check server."""
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self._port)

    async def stop(self) -> None:
        """Stop the health check server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
