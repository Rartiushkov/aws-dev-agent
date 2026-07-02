import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lambda_function import handler as lambda_handler


ROOT_DIR = Path(__file__).resolve().parent
DEMO_SUMMARY_PATH = ROOT_DIR / "state" / "clients" / "sandbox-demo" / "aws_inventory" / "sandbox1" / "summary.json"
DEMO_MANIFEST_PATH = ROOT_DIR / "state" / "clients" / "sandbox-demo" / "deployments" / "sandbox2" / "deployment_manifest.json"
DEMO_VALIDATION_PATH = ROOT_DIR / "state" / "clients" / "sandbox-demo" / "deployments" / "sandbox2" / "validation_report.json"
DEMO_ECS_CLONE_PATH = ROOT_DIR / "state" / "ecs_cluster_clones" / "cluster2b-demo" / "clone_result.json"


def _json_bytes(payload):
    return json.dumps(payload).encode("utf-8")


def _read_json_file(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_demo_payload():
    inventory = _read_json_file(DEMO_SUMMARY_PATH)
    deployment_manifest = _read_json_file(DEMO_MANIFEST_PATH)
    validation_report = _read_json_file(DEMO_VALIDATION_PATH)
    ecs_clone = _read_json_file(DEMO_ECS_CLONE_PATH)

    issue_checks = [check for check in validation_report["smoke_checks"] if check.get("status") != "ok"]
    ok_checks = [check for check in validation_report["smoke_checks"] if check.get("status") == "ok"]

    summary = {
        "created_resources": (
            len(deployment_manifest["roles"])
            + len(deployment_manifest["sqs_queues"])
            + len(deployment_manifest["dynamodb_tables"])
            + len(deployment_manifest["lambda_functions"])
            + len(deployment_manifest["vpcs"])
            + len(deployment_manifest["subnets"])
            + len(deployment_manifest["route_tables"])
        ),
        "copied_items": sum(item.get("copied_item_count", 0) for item in deployment_manifest["dynamodb_table_items"]),
        "validation_issue_checks": len(issue_checks),
        "ok_smoke_checks": len(ok_checks),
    }

    timeline = [
        f"Inventory loaded for {inventory['source_env']} in {inventory['region']} from account {inventory['account_id']}.",
        f"Discovered {inventory['counts']['dependency_nodes']} dependency nodes and {inventory['counts']['dependency_edges']} dependency edges.",
        f"Created target environment {deployment_manifest['target_env']} with {len(deployment_manifest['subnets'])} subnets and {len(deployment_manifest['vpcs'])} VPC.",
        f"Cloned Lambda {deployment_manifest['lambda_functions'][0]['source_function']} into {deployment_manifest['lambda_functions'][0]['target_function']}.",
        f"Copied {summary['copied_items']} DynamoDB item from {deployment_manifest['dynamodb_tables'][0]['source_table']} to {deployment_manifest['dynamodb_tables'][0]['target_table']}.",
        f"Validation finished with {summary['ok_smoke_checks']} ok checks and {summary['validation_issue_checks']} checks flagged for review.",
    ]

    return {
        "inventory": inventory,
        "deployment_manifest": deployment_manifest,
        "validation_report": validation_report,
        "ecs_clone": ecs_clone,
        "summary": summary,
        "timeline": timeline,
    }


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
                    "routes": ["/health", "/api/demo", "/api/cloudflare"],
                },
            )
        if self.path == "/api/demo":
            return self._send_json(200, _build_demo_payload())
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
