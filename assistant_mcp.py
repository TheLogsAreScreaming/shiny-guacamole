"""Assistant link: a tiny MCP (Model Context Protocol) server over stdio.

An assistant such as Claude Desktop starts this as a subprocess. It does not
touch the radio itself: it forwards read-only questions to the app's
localhost-only API (see ``mesh_integrations.LocalApiServer``), which must be
turned on with ``local_api_enabled = True``. The ``send_message`` tool is
offered only when the app reports ``assistant_send_enabled = True``.

Standard library only; messages are newline-delimited JSON-RPC 2.0.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

try:  # package layout (modules.assistant_mcp) or flat layout (assistant_mcp)
    from . import mesh_integrations as integrations
except ImportError:  # pragma: no cover - depends on how the app is packaged
    import mesh_integrations as integrations  # type: ignore[no-redef]

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "meshtastic-sniffer-tcp-assistant-link"
SERVER_VERSION = "1.0.0"

READ_TOOLS = [
    {
        "name": "get_network_status",
        "description": "Radio link state, how many nodes are known/heard in the last hour, packet counts, and which integrations are on.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_nodes",
        "description": "Mesh nodes heard by this app, most recent first, with names, last heard time (unix seconds), SNR/RSSI, hops and latest telemetry.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}},
            "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "recent_messages",
        "description": "Recent text-message activity (sender, time, channel), oldest first. This monitor never records message text.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            "channel": {"type": "integer", "minimum": 0, "maximum": 7}},
            "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "node_telemetry",
        "description": "Telemetry history (battery, voltage, temperature, humidity, pressure, channel use...) for one node, by node id (!abcd1234), short name or long name.",
        "inputSchema": {"type": "object", "properties": {
            "node": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}},
            "required": ["node"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
]

SEND_TOOL = {
    "name": "send_message",
    "description": "Send a text message on the mesh (max 200 characters). Only available because the owner turned on assistant sending. Confirm with the user before sending.",
    "inputSchema": {"type": "object", "properties": {
        "text": {"type": "string", "maxLength": 200},
        "channel": {"type": "integer", "minimum": 0, "maximum": 7, "default": 0},
        "destination": {"type": "string", "description": "Optional node id or name for a direct message."}},
        "required": ["text"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
}


class ApiError(Exception):
    pass


class LocalApiClient:
    def __init__(self, port: int, token: str, timeout: float = 5.0):
        self.base = f"http://127.0.0.1:{int(port)}"
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 (loopback only)
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except Exception:
                message = None
            raise ApiError(message or f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError("Meshtastic Sniffer TCP is not running or its assistant link is off "
                           "(Radio > Integrations…, tick Assistant link).") from exc

    def get(self, path: str, **params: Any) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._request("GET", path + (f"?{query}" if query else ""))

    def post(self, path: str, body: Mapping[str, Any]) -> dict:
        return self._request("POST", path, body)


class McpServer:
    def __init__(self, client_factory: Callable[[], LocalApiClient]):
        self._client_factory = client_factory
        self._client: LocalApiClient | None = None

    @property
    def client(self) -> LocalApiClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _send_allowed(self) -> bool:
        try:
            status = self.client.get("/api/status")
            return bool(status.get("integrations", {}).get("assistant_send_enabled"))
        except Exception:
            return False

    def tools(self) -> list[dict]:
        return READ_TOOLS + ([SEND_TOOL] if self._send_allowed() else [])

    def call_tool(self, name: str, args: Mapping[str, Any]) -> Any:
        c = self.client
        if name == "get_network_status":
            return c.get("/api/status")
        if name == "list_nodes":
            return c.get("/api/nodes", limit=args.get("limit", 50))
        if name == "recent_messages":
            return c.get("/api/messages", limit=args.get("limit", 20), channel=args.get("channel"))
        if name == "node_telemetry":
            return c.get("/api/telemetry", node=str(args.get("node", "")), limit=args.get("limit", 50))
        if name == "send_message":
            return c.post("/api/send", {"text": args.get("text", ""), "channel": args.get("channel", 0),
                                        "destination": args.get("destination")})
        raise KeyError(name)

    def handle(self, message: Mapping[str, Any]) -> dict | None:
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:  # notification (e.g. notifications/initialized): never reply
            return None
        try:
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result: Any = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "Read-only view of what a local Meshtastic Sniffer TCP monitor has heard. Message text is not available.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                if name not in {t["name"] for t in READ_TOOLS + [SEND_TOOL]}:
                    return _error(msg_id, -32602, f"Unknown tool: {name}")
                try:
                    data = self.call_tool(name, params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(data, indent=2, sort_keys=True)}],
                              "isError": False}
                except ApiError as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return _error(msg_id, -32601, f"Method not found: {method}")
        except Exception as exc:  # keep the stdio loop alive
            return _error(msg_id, -32603, str(exc) or type(exc).__name__)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                reply: dict | None = _error(None, -32700, "Parse error")
            else:
                reply = self.handle(message) if isinstance(message, dict) else _error(None, -32600, "Invalid request")
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def client_from_dir(data_dir: str | Path) -> LocalApiClient:
    """Port and token are written by the running app into its integrations folder."""
    data_dir = Path(data_dir)
    try:
        port = int(json.loads(integrations.port_file(data_dir).read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        port = 1  # nothing listening: every call reports that the link is off
    try:
        token = integrations.token_path(data_dir).read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    return LocalApiClient(port, token)


def main(data_dir: str | Path) -> int:
    McpServer(lambda: client_from_dir(data_dir)).serve()
    return 0
