"""Tests for the async health check server."""

import asyncio
import json

import pytest
import pytest_asyncio

from src.health import HealthServer


@pytest_asyncio.fixture
async def health_server():
    """Start a health server on a random available port, stop after test."""
    server = HealthServer(port=0)  # port 0 = OS picks available port
    await server.start()
    # Get the actual port assigned
    port = server._server.sockets[0].getsockname()[1]
    server._port = port
    yield server
    await server.stop()


@pytest.mark.asyncio
async def test_health_server_responds_200(health_server):
    reader, writer = await asyncio.open_connection("127.0.0.1", health_server._port)
    writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()

    response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
    writer.close()
    await writer.wait_closed()

    response_str = response.decode()
    assert "200 OK" in response_str
    assert "application/json" in response_str


@pytest.mark.asyncio
async def test_health_response_json_body(health_server):
    reader, writer = await asyncio.open_connection("127.0.0.1", health_server._port)
    writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()

    response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
    writer.close()
    await writer.wait_closed()

    # Extract JSON body after headers
    body = response.decode().split("\r\n\r\n", 1)[1]
    data = json.loads(body)

    assert data["status"] == "ok"
    assert "started_at" in data
    assert data["messages_processed"] == 0
    assert data["last_message_at"] is None


@pytest.mark.asyncio
async def test_message_counter(health_server):
    health_server.record_message()
    health_server.record_message()
    health_server.record_message()

    reader, writer = await asyncio.open_connection("127.0.0.1", health_server._port)
    writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()

    response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
    writer.close()
    await writer.wait_closed()

    body = response.decode().split("\r\n\r\n", 1)[1]
    data = json.loads(body)

    assert data["messages_processed"] == 3
    assert data["last_message_at"] is not None


@pytest.mark.asyncio
async def test_server_start_stop():
    server = HealthServer(port=0)
    await server.start()
    assert server._server is not None
    await server.stop()
    assert server._server is None
