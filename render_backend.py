import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lambda_function import handler as lambda_handler

FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "availabl-1f709")
STRIPE_SECRET_KEY   = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_PRICE_ID = os.environ.get("STRIPE_PRO_PRICE_ID", "price_1ToiXpE5xonjsdoogiiEaYfW")
FRONTEND_URL        = os.environ.get("FRONTEND_URL", "https://availabl.pages.dev")


def _verify_firebase_token(token):
    """Verify Firebase ID token via Google tokeninfo endpoint. Returns uid or raises."""
    url = f"https://oauth2.googleapis.com/tokeninfo?id_token={token}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if data.get("aud") != FIREBASE_PROJECT_ID:
            raise ValueError("Token audience mismatch")
        return data.get("sub")  # uid
    except urllib.error.HTTPError as e:
        raise ValueError(f"Invalid token: {e.code}")


def _get_uid(headers):
    """Extract and verify Bearer token from Authorization header. Returns uid or None."""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    try:
        return _verify_firebase_token(token)
    except Exception:
        return None


def _stripe_request(method, path, data=None):
    """Make a Stripe API request. Returns parsed JSON."""
    url = f"https://api.stripe.com/v1{path}"
    body = None
    if data:
        body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {STRIPE_SECRET_KEY}")
    if body:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _create_checkout_session(uid, email, price_id):
    """Create a Stripe Checkout session for subscription."""
    data = {
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": f"{FRONTEND_URL}/dashboard.html?upgraded=1",
        "cancel_url":  f"{FRONTEND_URL}/pricing.html",
        "customer_email": email,
        "metadata[uid]": uid,
        "subscription_data[metadata][uid]": uid,
    }
    return _stripe_request("POST", "/checkout/sessions", data)


def _update_firestore_plan(uid, plan):
    """Update user plan in Firestore via REST API."""
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/users/{uid}?updateMask.fieldPaths=plan"
    )
    body = json.dumps({"fields": {"plan": {"stringValue": plan}}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PATCH")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


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
                    "routes": ["/health", "/api/demo", "/api/me", "/api/cloudflare"],
                },
            )
        if self.path == "/api/demo":
            return self._send_json(200, _build_demo_payload())
        if self.path == "/api/me":
            uid = _get_uid(self.headers)
            if not uid:
                return self._send_json(401, {"error": "Unauthorized"})
            return self._send_json(200, {"uid": uid, "authenticated": True})
        if self.path.startswith("/api/checkout"):
            uid = _get_uid(self.headers)
            if not uid:
                return self._send_json(401, {"error": "Unauthorized"})
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            email = qs.get("email", [""])[0]
            plan  = qs.get("plan", ["pro"])[0]
            price_id = STRIPE_PRO_PRICE_ID
            if not STRIPE_SECRET_KEY:
                return self._send_json(503, {"error": "Stripe not configured"})
            try:
                session = _create_checkout_session(uid, email, price_id)
                return self._send_json(200, {"url": session["url"]})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if self.path == "/" or self.path == "/api/cloudflare":
            status, payload = self._invoke_lambda({"action": "describe"})
            return self._send_json(status, payload)
        return self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/api/scan":
            uid = _get_uid(self.headers)
            if not uid:
                return self._send_json(401, {"error": "Unauthorized"})
            try:
                payload = self._read_json()
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "Invalid JSON body"})
            src_account = payload.get("src_account", "")
            src_region  = payload.get("src_region", "us-east-1")
            src_role_arn = payload.get("src_role_arn", "")
            if not src_account or not src_role_arn:
                return self._send_json(400, {"error": "src_account and src_role_arn are required"})
            return self._send_json(202, {
                "status": "queued",
                "uid": uid,
                "src_account": src_account,
                "src_region": src_region,
                "message": "Scan queued. Results will be available in your dashboard.",
            })
        if self.path == "/api/webhook":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length)
            sig_header = self.headers.get("Stripe-Signature", "")
            if STRIPE_WEBHOOK_SECRET:
                try:
                    parts = {k: v for k, v in (p.split("=", 1) for p in sig_header.split(",") if "=" in p)}
                    ts = parts.get("t", "")
                    sig = parts.get("v1", "")
                    signed = f"{ts}.".encode() + raw_body
                    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(expected, sig):
                        return self._send_json(400, {"error": "Invalid signature"})
                except Exception:
                    return self._send_json(400, {"error": "Signature error"})
            try:
                event = json.loads(raw_body.decode("utf-8"))
            except Exception:
                return self._send_json(400, {"error": "Invalid JSON"})
            event_type = event.get("type", "")
            if event_type == "checkout.session.completed":
                session = event.get("data", {}).get("object", {})
                uid = session.get("metadata", {}).get("uid")
                if uid:
                    _update_firestore_plan(uid, "pro")
            elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
                sub = event.get("data", {}).get("object", {})
                uid = sub.get("metadata", {}).get("uid")
                if uid:
                    _update_firestore_plan(uid, "starter")
            return self._send_json(200, {"received": True})
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
