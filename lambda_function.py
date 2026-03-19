import json
import os
import urllib.error
import urllib.parse
import urllib.request


CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"


def _json_response(status_code, body):
    return {
        "statusCode": status_code,
        "body": json.dumps(body),
    }


def _cloudflare_token():
    token = os.getenv("CLOUDFLARE_API_TOKEN")
    if not token:
        raise ValueError("CLOUDFLARE_API_TOKEN is required")
    return token


def _resolve_zone_id(event):
    return (
        event.get("zone_id")
        or event.get("zoneId")
        or os.getenv("CLOUDFLARE_ZONE_ID")
    )


def _request_cloudflare(method, path, payload=None, query=None):
    token = _cloudflare_token()
    url = f"{CLOUDFLARE_API_BASE}{path}"

    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"success": False, "errors": [{"message": body}]}
        raise RuntimeError(json.dumps(parsed))


def list_zones(event):
    query = {}
    if event.get("zone_name"):
        query["name"] = event["zone_name"]
    response = _request_cloudflare("GET", "/zones", query=query)
    return _json_response(200, response)


def list_dns_records(event):
    zone_id = _resolve_zone_id(event)
    if not zone_id:
        raise ValueError("zone_id or CLOUDFLARE_ZONE_ID is required")

    query = {}
    if event.get("name"):
        query["name"] = event["name"]
    if event.get("type"):
        query["type"] = event["type"]

    response = _request_cloudflare("GET", f"/zones/{zone_id}/dns_records", query=query)
    return _json_response(200, response)


def upsert_dns_record(event):
    zone_id = _resolve_zone_id(event)
    if not zone_id:
        raise ValueError("zone_id or CLOUDFLARE_ZONE_ID is required")

    record_type = event.get("type")
    record_name = event.get("name")
    record_content = event.get("content")

    if not record_type or not record_name or not record_content:
        raise ValueError("type, name, and content are required")

    payload = {
        "type": record_type,
        "name": record_name,
        "content": record_content,
        "ttl": event.get("ttl", 1),
    }

    if "proxied" in event:
        payload["proxied"] = bool(event["proxied"])

    existing = _request_cloudflare(
        "GET",
        f"/zones/{zone_id}/dns_records",
        query={"name": record_name, "type": record_type},
    )

    records = existing.get("result", [])
    if records:
        record_id = records[0]["id"]
        response = _request_cloudflare(
            "PUT",
            f"/zones/{zone_id}/dns_records/{record_id}",
            payload=payload,
        )
        return _json_response(200, {"operation": "updated", "cloudflare": response})

    response = _request_cloudflare(
        "POST",
        f"/zones/{zone_id}/dns_records",
        payload=payload,
    )
    return _json_response(200, {"operation": "created", "cloudflare": response})


def delete_dns_record(event):
    zone_id = _resolve_zone_id(event)
    record_id = event.get("record_id") or event.get("recordId")

    if not zone_id:
        raise ValueError("zone_id or CLOUDFLARE_ZONE_ID is required")
    if not record_id:
        raise ValueError("record_id is required")

    response = _request_cloudflare(
        "DELETE",
        f"/zones/{zone_id}/dns_records/{record_id}",
    )
    return _json_response(200, response)


def handler(event, context):
    event = event or {}
    action = (event.get("action") or "describe").lower()

    try:
        if action == "describe":
            return _json_response(
                200,
                {
                    "service": "cloudflare-integration",
                    "actions": [
                        "describe",
                        "list_zones",
                        "list_dns_records",
                        "upsert_dns_record",
                        "delete_dns_record",
                    ],
                    "required_env": [
                        "CLOUDFLARE_API_TOKEN",
                    ],
                    "optional_env": [
                        "CLOUDFLARE_ZONE_ID",
                    ],
                },
            )

        if action == "list_zones":
            return list_zones(event)

        if action == "list_dns_records":
            return list_dns_records(event)

        if action == "upsert_dns_record":
            return upsert_dns_record(event)

        if action == "delete_dns_record":
            return delete_dns_record(event)

        return _json_response(400, {"error": f"Unsupported action: {action}"})

    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})
    except RuntimeError as exc:
        return _json_response(502, {"error": "Cloudflare API request failed", "details": str(exc)})
    except Exception as exc:
        return _json_response(500, {"error": "Unhandled exception", "details": str(exc)})
