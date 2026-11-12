"""
Exercise the repository's own Telnyx call sites against a local fake Telnyx API.

The fake server is a real HTTP server on 127.0.0.1, so the installed SDK (classic
or 4.x) makes real requests with its own objects; nothing in the SDK is mocked.
"""
import ast
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (os.path.join(ROOT, "src"), os.path.join(ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

REQUESTS = []


class _FakeTelnyx(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        REQUESTS.append((self.command, self.path, body, dict(self.headers)))
        path = self.path.split("?")[0]
        if path == "/v2/messages":
            self._reply(200, {"data": {
                "record_type": "message",
                "direction": "outbound",
                "id": "40385f64-5717-4562-b3fc-2c963f66afa6",
                "type": "SMS",
                "messaging_profile_id": body.get("messaging_profile_id")
                or "4001932a-5ae6-4d2b-9d4c-0b6a1f3f7c11",
                "from": {"phone_number": body.get("from"), "carrier": "T-Mobile", "line_type": "Wireless"},
                "to": [{"phone_number": body.get("to"), "status": "queued", "carrier": "T-Mobile", "line_type": "Wireless"}],
                "text": body.get("text"),
                "parts": 1,
                "encoding": "GSM-7",
                "created_at": "2024-01-01T00:00:00.000+00:00",
            }})
        elif path == "/v2/calls":
            self._reply(200, {"data": {
                "record_type": "call",
                "call_control_id": "v3:abcdefghijklmnopqrstuvwxyz0123456789",
                "call_leg_id": "428c31b6-7af4-4bcb-b7f5-5013ef9657c1",
                "call_session_id": "428c31b6-abf3-11ea-9b1f-02420a0f6e0f",
                "is_alive": False,
            }})
        elif "/actions/" in path:
            self._reply(200, {"data": {"result": "ok"}})
        else:
            self._reply(404, {"errors": [{"code": "10005", "title": "Resource not found"}]})


@pytest.fixture(scope="module")
def fake_api():
    server = HTTPServer(("127.0.0.1", 0), _FakeTelnyx)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d" % server.server_port

    saved = {k: os.environ.get(k) for k in (
        "TELNYX_API_BASE", "TELNYX_BASE_URL", "TELNYX_API_KEY", "TELNYX_PHONE_NUMBER",
        "TELNYX_CONNECTION_ID", "TELNYX_WEBHOOK_URL", "TELNYX_MESSAGING_PROFILE_ID",
        "TELNYX_FROM_NUMBER", "NO_PROXY", "no_proxy")}
    os.environ["TELNYX_API_BASE"] = url
    os.environ["TELNYX_BASE_URL"] = url + "/v2"
    os.environ["TELNYX_API_KEY"] = "KEY_test_not_a_real_key"
    os.environ["TELNYX_PHONE_NUMBER"] = "+13125550100"
    os.environ["TELNYX_CONNECTION_ID"] = "1234567890"
    os.environ["TELNYX_WEBHOOK_URL"] = "http://127.0.0.1/webhook"
    os.environ["TELNYX_MESSAGING_PROFILE_ID"] = "4001932a-5ae6-4d2b-9d4c-0b6a1f3f7c11"
    os.environ["TELNYX_FROM_NUMBER"] = "+13125550100"
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"

    import telnyx
    telnyx.api_base = url
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_handler_sends_sms_and_dials_call(fake_api):
    from telnyx_handler import TelnyxHandler

    REQUESTS.clear()
    handler = TelnyxHandler()

    sms = asyncio.run(handler.send_alert_sms("+13125550123", "Alert text", "child-1", "HIGH"))
    assert sms == {
        "status": "sent",
        "message_id": "40385f64-5717-4562-b3fc-2c963f66afa6",
        "to": "+13125550123",
    }

    call = asyncio.run(handler.initiate_alert_call("+13125550123", "child-1", "HIGH"))
    assert call == {
        "status": "initiated",
        "call_id": "v3:abcdefghijklmnopqrstuvwxyz0123456789",
        "to": "+13125550123",
    }

    paths = [p for _, p, _, _ in REQUESTS]
    assert "/v2/messages" in paths
    assert "/v2/calls" in paths
    msg_body = next(b for _, p, b, _ in REQUESTS if p == "/v2/messages")
    assert msg_body["from"] == "+13125550100"
    assert msg_body["to"] == "+13125550123"
    assert msg_body["text"] == "Alert text"
    auth = next(h for _, p, _, h in REQUESTS if p == "/v2/messages")
    assert "KEY_test_not_a_real_key" in auth.get("Authorization", "")


def test_api_send_sms_alert(fake_api):
    # vigilancepilot.api can't be imported here (fastapi and the rest of its imports
    # aren't available), so load the repository's own _send_sms_alert from its source.
    path = os.path.join(ROOT, "src", "vigilancepilot", "api.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    func = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_sms_alert"
    )
    module = ast.Module(body=[func], type_ignores=[])
    namespace = {"os": os, "Dict": Dict, "Any": Any}
    exec(compile(module, path, "exec"), namespace)
    _send_sms_alert = namespace["_send_sms_alert"]

    REQUESTS.clear()
    result = asyncio.run(_send_sms_alert("Risk alert", "+13125550123"))
    assert result == {
        "status": "sent",
        "telnyx_id": "40385f64-5717-4562-b3fc-2c963f66afa6",
    }

    sent = [b for _, p, b, _ in REQUESTS if p == "/v2/messages"]
    assert len(sent) == 1
    assert sent[0]["to"] == "+13125550123"
    assert sent[0]["text"] == "Risk alert"
    assert sent[0]["messaging_profile_id"] == "4001932a-5ae6-4d2b-9d4c-0b6a1f3f7c11"
