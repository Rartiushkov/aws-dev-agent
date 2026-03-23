import json
from datetime import datetime, timezone

from executor.scripts.transfer_common import audit_log_path, resolve_client_slug


def append_audit_event(action, status, details=None, target_env="", source_env="", client_slug="", config=None):
    resolved_client_slug = resolve_client_slug(client_slug, config, source_env=source_env, target_env=target_env)
    audit_path = audit_log_path(resolved_client_slug)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "status": status,
        "client_slug": resolved_client_slug,
        "source_env": source_env,
        "target_env": target_env,
        "details": details or {},
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, default=str) + "\n")
    return audit_path
