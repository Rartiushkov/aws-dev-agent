import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lambda_function import handler as lambda_handler


def _json_bytes(payload):
    return json.dumps(payload).encode("utf-8")


class RenderBackendHandler(BaseHTTPRequestHandler):
    server_version = "AvailablRenderBackend/1.0"

    def _send_json(self, status, payload, extra_headers=None):
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def _invoke_lambda(self, payload):
        response = lambda_handler(payload, None)
        status = int(response.get("statusCode", 200))
        body = response.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = {"raw": body}
        return status, body if isinstance(body, dict) else {"result": body}

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})

    def do_GET(self):
        if self.path == "/health":
            return self._send_json(
                200,
                {
                    "ok": True,
                    "service": "availabl-backend",
                    "routes": ["/health", "/api/cloudflare"],
                },
            )
        if self.path == "/" or self.path == "/api/cloudflare":
            status, payload = self._invoke_lambda({"action": "describe"})
            return self._send_json(status, payload)
        return self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/cloudflare":
            return self._send_json(404, {"error": "Not found"})
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            return self._send_json(400, {"error": "Invalid JSON body"})
        status, result = self._invoke_lambda(payload)
        return self._send_json(status, result)


def main():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), RenderBackendHandler)
    print(f"Render backend listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
