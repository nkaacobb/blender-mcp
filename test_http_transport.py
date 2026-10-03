"""Real HTTP regression checks; starts only the MCP server, never Blender.

Run: python -m unittest -v test_http_transport
"""

import asyncio
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import threading
import time
import unittest

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("blender_server", ROOT / "blender-mcp-server.py")
SERVER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER_MODULE)
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "http-transport-regression", "version": "1.0"},
    },
}
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


class HTTPTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app, _ = SERVER_MODULE._build_app(ROOT, SERVER_MODULE.DEFAULT_ALLOWED_ORIGINS, True)
        cls.listener = socket.socket()
        cls.listener.bind(("127.0.0.1", 0))
        cls.port = cls.listener.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        cls.thread = threading.Thread(
            target=cls.server.run, kwargs={"sockets": [cls.listener]}, daemon=True,
        )
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started:
            if not cls.thread.is_alive() or time.monotonic() > deadline:
                cls.server.should_exit = True
                cls.listener.close()
                raise RuntimeError("Test HTTP server failed to start")
            time.sleep(0.01)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.listener.close()

    def test_initialize_notification_and_tools_on_keepalive_connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)

        def post(message, expected_status):
            connection.request("POST", "/mcp", json.dumps(message), HEADERS)
            response = connection.getresponse()
            self.assertEqual(response.status, expected_status)
            self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
            self.assertIsNone(response.getheader("Content-Length"))
            return response.read()

        result = json.loads(post(INITIALIZE, 200))["result"]
        self.assertEqual(result["serverInfo"]["name"], "Blender MCP")
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        first_socket = connection.sock
        self.assertEqual(post({"jsonrpc": "2.0", "method": "notifications/initialized"}, 202), b"")
        tools = json.loads(post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 200))["result"]["tools"]
        self.assertEqual(len(tools), 51)
        self.assertEqual(len({tool["name"] for tool in tools}), 51)
        self.assertIs(connection.sock, first_socket)

    def test_wire_has_chunk_sizes_and_terminal_zero(self):
        payload = json.dumps(INITIALIZE).encode()
        request = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n"
            f"Connection: close\r\nContent-Length: {len(payload)}\r\n\r\n"
        ).encode() + payload
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            connection.sendall(request)
            parts = []
            while data := connection.recv(65536):
                parts.append(data)
        headers, body = b"".join(parts).split(b"\r\n\r\n", 1)
        self.assertIn(b"transfer-encoding: chunked", headers.lower())
        decoded = bytearray()
        while True:
            size_line, body = body.split(b"\r\n", 1)
            size = int(size_line, 16)
            if size == 0:
                self.assertEqual(body, b"\r\n")
                break
            self.assertEqual(body[size:size + 2], b"\r\n")
            decoded.extend(body[:size])
            body = body[size + 2:]
        self.assertEqual(json.loads(decoded)["result"]["serverInfo"]["name"], "Blender MCP")

    def test_official_mcp_client(self):
        async def check():
            async with httpx.AsyncClient(trust_env=False) as client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{self.port}/mcp", http_client=client,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        result = await session.initialize()
                        self.assertEqual(result.serverInfo.name, "Blender MCP")
                        self.assertEqual(len((await session.list_tools()).tools), 51)
        asyncio.run(check())

    def test_browser_cors_preflight_and_json_response(self):
        with httpx.Client(base_url=f"http://127.0.0.1:{self.port}", trust_env=False) as client:
            origin = "http://127.0.0.1:8080"
            response = client.options("/mcp", headers={
                "Origin": origin, "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,mcp-protocol-version",
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], origin)
            response = client.post("/mcp", json=INITIALIZE, headers={**HEADERS, "Origin": origin})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["access-control-allow-origin"], origin)
            self.assertEqual(response.headers["content-type"], "application/json")
            self.assertEqual(response.json()["id"], 1)


if __name__ == "__main__":
    unittest.main()
