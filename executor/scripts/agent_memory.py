import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from executor.scripts.transfer_common import agent_memory_path, resolve_client_slug


DEFAULT_MEMORY_LIMIT = 500


def memory_store_path(path="", client_slug=""):
    if path:
        return Path(path)
    resolved_client_slug = resolve_client_slug(client_slug)
    return agent_memory_path(resolved_client_slug)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value, max_length=240):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"arn:aws:[^\s\"']+", "<arn>", text)
    text = re.sub(r"\b\d{12}\b", "<account>", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", text)
    text = re.sub(r"\b(vpc|subnet|sg|rtb|eni|igw|nat)-[0-9a-f]+\b", r"<\1-id>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length]


def build_signature(kind, summary, tags=None):
    normalized = normalize_text(summary)
    tag_part = "|".join(sorted(set(tags or [])))
    raw = f"{kind}|{normalized}|{tag_part}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def load_incidents(path="", client_slug=""):
    store_path = memory_store_path(path, client_slug=client_slug)
    if not store_path.exists():
        return []
    return json.loads(store_path.read_text(encoding="utf-8"))


def save_incidents(incidents, path="", limit=DEFAULT_MEMORY_LIMIT, client_slug=""):
    store_path = memory_store_path(path, client_slug=client_slug)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    trimmed = sorted(
        incidents,
        key=lambda item: (item.get("last_seen", ""), item.get("occurrences", 0)),
        reverse=True,
    )[:limit]
    store_path.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")
    return store_path


def record_incident(
    kind,
    summary,
    *,
    path="",
    client_slug="",
    scope="global",
    tags=None,
    source_env="",
    target_env="",
    resolution="",
    validated=False,
    details=None,
):
    normalized_summary = normalize_text(summary)
    if not normalized_summary:
        return memory_store_path(path, client_slug=client_slug)
    details = details or {}
    tags = sorted(set(tags or []))
    signature = build_signature(kind, normalized_summary, tags)
    incidents = load_incidents(path, client_slug=client_slug)
    now = utc_now()
    existing = next((item for item in incidents if item.get("signature") == signature and item.get("scope") == scope), None)
    if existing:
        existing["last_seen"] = now
        existing["occurrences"] = existing.get("occurrences", 0) + 1
        existing["source_env"] = source_env or existing.get("source_env", "")
        existing["target_env"] = target_env or existing.get("target_env", "")
        existing["tags"] = sorted(set(existing.get("tags", []) + tags))
        if details:
            existing["sample_details"] = details
        if resolution:
            existing["last_resolution"] = normalize_text(resolution, max_length=400)
        if validated:
            existing["validated_fix_count"] = existing.get("validated_fix_count", 0) + 1
            existing["last_validated_at"] = now
    else:
        incidents.append({
            "signature": signature,
            "kind": kind,
            "scope": scope,
            "summary": normalized_summary,
            "tags": tags,
            "source_env": source_env,
            "target_env": target_env,
            "occurrences": 1,
            "validated_fix_count": 1 if validated else 0,
            "first_seen": now,
            "last_seen": now,
            "last_validated_at": now if validated else "",
            "last_resolution": normalize_text(resolution, max_length=400) if resolution else "",
            "sample_details": details,
        })
    return save_incidents(incidents, path, client_slug=client_slug)


def score_incident(query_tokens, incident):
    haystack = " ".join([
        incident.get("summary", ""),
        " ".join(incident.get("tags", [])),
        incident.get("last_resolution", ""),
    ])
    score = 0
    for token in query_tokens:
        if token and token in haystack:
            score += 1
    score += min(incident.get("validated_fix_count", 0), 5)
    score += min(incident.get("occurrences", 0), 5)
    return score


def find_similar_incidents(query, *, path="", limit=5, client_slug=""):
    query_tokens = [token for token in normalize_text(query).split(" ") if token]
    if not query_tokens:
        return []
    incidents = load_incidents(path, client_slug=client_slug)
    ranked = sorted(
        (
            (score_incident(query_tokens, incident), incident)
            for incident in incidents
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    return [incident for score, incident in ranked if score > 0][:limit]


def suggest_known_fixes(query, *, path="", limit=3, client_slug=""):
    suggestions = []
    for incident in find_similar_incidents(query, path=path, limit=limit, client_slug=client_slug):
        suggestions.append({
            "summary": incident.get("summary", ""),
            "resolution": incident.get("last_resolution", ""),
            "validated_fix_count": incident.get("validated_fix_count", 0),
            "occurrences": incident.get("occurrences", 0),
            "tags": incident.get("tags", []),
        })
    return suggestions
