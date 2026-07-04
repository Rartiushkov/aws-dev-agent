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
ALLOWED_ORIGINS     = {
    "https://availabl.pages.dev",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
}


def _verify_firebase_token(token):
    """Verify Firebase ID token via Firebase Auth REST API. Returns uid or raises."""
    url = (
        f"https://identitytoolkit.googleapis.com/v1/accounts:lookup"
        f"?key=AIzaSyC2s8vy7THhcs9YO5Ro5lwenICXZpzmgD8"
    )
    body = json.dumps({"idToken": token}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        users = data.get("users", [])
        if not users:
            raise ValueError("Token invalid: no user found")
        return users[0].get("localId")  # uid
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise ValueError(f"Invalid token: {e.code} {body}")


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


def _firestore_patch(path, fields):
    """PATCH a Firestore document with given fields dict."""
    field_names = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{path}?{field_names}"
    )
    body = json.dumps({"fields": fields}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PATCH")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _firestore_add(collection_path, fields):
    """POST a new document to a Firestore collection."""
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{collection_path}"
    )
    body = json.dumps({"fields": fields}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def _ts_value(iso_str):
    return {"timestampValue": iso_str}


def _str_value(s):
    return {"stringValue": str(s)}


def _bool_value(b):
    return {"booleanValue": bool(b)}


def _int_value(n):
    return {"integerValue": str(int(n))}


def _update_firestore_plan(uid, plan, extra=None):
    """Update user plan + billing fields in Firestore."""
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    fields = {
        "plan":          _str_value(plan),
        "planStatus":    _str_value("active" if plan != "starter" else "canceled"),
        "planUpdatedAt": _ts_value(now),
    }
    if extra:
        fields.update(extra)
    return _firestore_patch(f"users/{uid}", fields)


def _add_billing_event(uid, event_type, amount=0, currency="usd", invoice_id="", subscription_id=""):
    """Write a billing event to users/{uid}/billing_events."""
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    fields = {
        "type":           _str_value(event_type),
        "amount":         _int_value(amount),
        "currency":       _str_value(currency),
        "invoiceId":      _str_value(invoice_id),
        "subscriptionId": _str_value(subscription_id),
        "createdAt":      _ts_value(now),
    }
    return _firestore_add(f"users/{uid}/billing_events", fields)


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

    def _cors_origin(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            return origin
        if origin.endswith(".availabl.pages.dev") and origin.startswith("https://"):
            return origin
        return FRONTEND_URL

    def _send_json(self, status, payload, extra_headers=None):
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self._cors_origin())
        self.send_header("Vary", "Origin")
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
            import datetime

            def _iso(ts):
                if not ts:
                    return ""
                try:
                    return datetime.datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
                except Exception:
                    return ""

            event_type = event.get("type", "")

            if event_type == "checkout.session.completed":
                session = event.get("data", {}).get("object", {})
                uid = session.get("metadata", {}).get("uid")
                sub_id = session.get("subscription", "")
                customer_id = session.get("customer", "")
                amount = session.get("amount_total", 0)
                currency = session.get("currency", "usd")
                if uid:
                    extra = {
                        "stripeCustomerId":     _str_value(customer_id),
                        "stripeSubscriptionId": _str_value(sub_id),
                        "cancelAtPeriodEnd":    _bool_value(False),
                        "trialEnd":             _str_value(""),
                        "lastPaymentAmount":    _int_value(amount),
                        "lastPaymentCurrency":  _str_value(currency),
                        "lastPaymentAt":        _ts_value(_iso(None) or datetime.datetime.utcnow().isoformat() + "Z"),
                    }
                    _update_firestore_plan(uid, "pro", extra=extra)
                    _add_billing_event(uid, "subscription_created",
                                       amount=amount, currency=currency,
                                       subscription_id=sub_id)

            elif event_type == "customer.subscription.created":
                sub = event.get("data", {}).get("object", {})
                uid = sub.get("metadata", {}).get("uid")
                if uid:
                    period_start_iso = _iso(sub.get("current_period_start"))
                    period_end_iso   = _iso(sub.get("current_period_end"))
                    trial_end_iso    = _iso(sub.get("trial_end"))
                    item = (sub.get("items", {}).get("data") or [{}])[0]
                    price = item.get("price", {})
                    extra = {
                        "stripeSubscriptionId": _str_value(sub.get("id", "")),
                        "stripeCustomerId":     _str_value(sub.get("customer", "")),
                        "planStatus":           _str_value(sub.get("status", "active")),
                        "currentPeriodStart":   _ts_value(period_start_iso) if period_start_iso else _str_value(""),
                        "currentPeriodEnd":     _ts_value(period_end_iso)   if period_end_iso   else _str_value(""),
                        "cancelAtPeriodEnd":    _bool_value(sub.get("cancel_at_period_end", False)),
                        "trialEnd":             _ts_value(trial_end_iso) if trial_end_iso else _str_value(""),
                        "planInterval":         _str_value(price.get("recurring", {}).get("interval", "month")),
                        "planPrice":            _int_value(price.get("unit_amount", 0)),
                        "planCurrency":         _str_value(price.get("currency", "usd")),
                        "stripePriceId":        _str_value(price.get("id", "")),
                    }
                    _update_firestore_plan(uid, "pro", extra=extra)

            elif event_type == "customer.subscription.updated":
                sub = event.get("data", {}).get("object", {})
                uid = sub.get("metadata", {}).get("uid")
                if uid:
                    period_start_iso = _iso(sub.get("current_period_start"))
                    period_end_iso   = _iso(sub.get("current_period_end"))
                    trial_end_iso    = _iso(sub.get("trial_end"))
                    item = (sub.get("items", {}).get("data") or [{}])[0]
                    price = item.get("price", {})
                    extra = {
                        "stripeSubscriptionId": _str_value(sub.get("id", "")),
                        "planStatus":           _str_value(sub.get("status", "active")),
                        "currentPeriodStart":   _ts_value(period_start_iso) if period_start_iso else _str_value(""),
                        "currentPeriodEnd":     _ts_value(period_end_iso)   if period_end_iso   else _str_value(""),
                        "cancelAtPeriodEnd":    _bool_value(sub.get("cancel_at_period_end", False)),
                        "trialEnd":             _ts_value(trial_end_iso) if trial_end_iso else _str_value(""),
                        "planInterval":         _str_value(price.get("recurring", {}).get("interval", "month")),
                        "planPrice":            _int_value(price.get("unit_amount", 0)),
                        "planCurrency":         _str_value(price.get("currency", "usd")),
                        "stripePriceId":        _str_value(price.get("id", "")),
                    }
                    plan = "pro" if sub.get("status") in ("active", "trialing") else "starter"
                    _update_firestore_plan(uid, plan, extra=extra)

            elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
                sub = event.get("data", {}).get("object", {})
                uid = sub.get("metadata", {}).get("uid")
                if uid:
                    canceled_at_iso = _iso(sub.get("canceled_at"))
                    extra = {
                        "stripeSubscriptionId": _str_value(""),
                        "cancelAtPeriodEnd":    _bool_value(False),
                        "canceledAt":           _ts_value(canceled_at_iso) if canceled_at_iso else _str_value(""),
                        "currentPeriodEnd":     _str_value(""),
                    }
                    _update_firestore_plan(uid, "starter", extra=extra)
                    _add_billing_event(uid, event_type.split(".")[-1],
                                       subscription_id=sub.get("id", ""))

            elif event_type == "invoice.payment_succeeded":
                invoice = event.get("data", {}).get("object", {})
                uid = invoice.get("subscription_details", {}).get("metadata", {}).get("uid", "")
                paid_at_iso = _iso(invoice.get("status_transitions", {}).get("paid_at"))
                period_end_iso = _iso(invoice.get("period_end"))
                amount_paid = invoice.get("amount_paid", 0)
                currency = invoice.get("currency", "usd")
                if uid:
                    # Update user doc with latest payment info + next billing date
                    _firestore_patch(f"users/{uid}", {
                        "lastPaymentAt":       _ts_value(paid_at_iso) if paid_at_iso else _ts_value(datetime.datetime.utcnow().isoformat() + "Z"),
                        "lastPaymentAmount":   _int_value(amount_paid),
                        "lastPaymentCurrency": _str_value(currency),
                        "lastInvoiceId":       _str_value(invoice.get("id", "")),
                        "currentPeriodEnd":    _ts_value(period_end_iso) if period_end_iso else _str_value(""),
                        "planStatus":          _str_value("active"),
                    })
                    _add_billing_event(uid, "payment_succeeded",
                                       amount=amount_paid, currency=currency,
                                       invoice_id=invoice.get("id", ""),
                                       subscription_id=invoice.get("subscription", ""))

            elif event_type == "invoice.payment_failed":
                invoice = event.get("data", {}).get("object", {})
                uid = invoice.get("subscription_details", {}).get("metadata", {}).get("uid", "")
                if uid:
                    _firestore_patch(f"users/{uid}", {
                        "planStatus":          _str_value("past_due"),
                        "lastFailedPaymentAt": _ts_value(datetime.datetime.utcnow().isoformat() + "Z"),
                        "lastFailedAmount":    _int_value(invoice.get("amount_due", 0)),
                        "lastFailedInvoiceId": _str_value(invoice.get("id", "")),
                    })
                    _add_billing_event(uid, "payment_failed",
                                       amount=invoice.get("amount_due", 0),
                                       currency=invoice.get("currency", "usd"),
                                       invoice_id=invoice.get("id", ""))

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
