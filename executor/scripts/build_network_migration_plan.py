import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import inventory_dir_path, resolve_client_slug


def parse_args():
    parser = argparse.ArgumentParser(description="Build a network migration plan from a discovered snapshot.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def build_network_plan(snapshot):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "region": snapshot.get("region", ""),
        "summary": {
            "vpcs": len(snapshot.get("vpcs", [])),
            "subnets": len(snapshot.get("subnets", [])),
            "route_tables": len(snapshot.get("route_tables", [])),
            "security_groups": len(snapshot.get("security_groups", [])),
        },
        "steps": [
            {"order": 1, "resource": "vpcs", "count": len(snapshot.get("vpcs", [])), "action": "create-or-map"},
            {"order": 2, "resource": "subnets", "count": len(snapshot.get("subnets", [])), "action": "create-or-map"},
            {"order": 3, "resource": "route_tables", "count": len(snapshot.get("route_tables", [])), "action": "create-or-map"},
            {"order": 4, "resource": "security_groups", "count": len(snapshot.get("security_groups", [])), "action": "create-or-map"},
        ],
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    plan = build_network_plan(snapshot)
    plan_path = inventory_dir / "network_migration_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    append_audit_event("build_network_migration_plan", "ok", {"plan_path": str(plan_path)}, source_env=source_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "plan_path": str(plan_path)}, indent=2))


if __name__ == "__main__":
    main()
