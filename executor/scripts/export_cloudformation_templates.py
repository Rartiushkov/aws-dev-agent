import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.transfer_common import config_override, inventory_dir_path, load_transfer_config, resolve_client_slug, session_for


def parse_args():
    parser = argparse.ArgumentParser(description="Export CloudFormation templates for discovered stacks.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def safe_filename(value):
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-_.") or "stack"


def export_templates(snapshot, cf_client):
    exported = []
    for stack in snapshot.get("cloudformation_stacks", []):
        stack_name = stack.get("StackName", "")
        if not stack_name:
            continue
        try:
            response = cf_client.get_template(StackName=stack_name)
            exported.append({
                "stack_name": stack_name,
                "template_body": response.get("TemplateBody", ""),
                "stages_available": response.get("StagesAvailable", []),
            })
        except Exception as exc:
            exported.append({
                "stack_name": stack_name,
                "error": str(exc),
            })
    return exported


def serialize_template_body(template_body):
    if isinstance(template_body, str):
        return template_body
    if isinstance(template_body, OrderedDict):
        return json.dumps(template_body, indent=2)
    return json.dumps(template_body, indent=2)


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, args.inventory_key, client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    source_external_id = args.source_external_id or config_override(config, "source_external_id", "")
    session = session_for(args.region, args.source_role_arn, external_id=source_external_id)
    exported = export_templates(snapshot, session.client("cloudformation"))
    target_dir = inventory_dir / "cloudformation_templates"
    target_dir.mkdir(parents=True, exist_ok=True)

    manifest = {"stacks": []}
    for item in exported:
        stack_name = item["stack_name"]
        if item.get("template_body"):
            output_path = target_dir / f"{safe_filename(stack_name)}.json"
            output_path.write_text(serialize_template_body(item["template_body"]), encoding="utf-8")
            manifest["stacks"].append({"stack_name": stack_name, "template_path": str(output_path)})
        else:
            manifest["stacks"].append({"stack_name": stack_name, "error": item.get("error", "export failed")})

    manifest_path = inventory_dir / "cloudformation_template_exports.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    append_audit_event("export_cloudformation_templates", "ok", {"manifest_path": str(manifest_path)}, source_env=source_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "manifest_path": str(manifest_path), "stack_count": len(manifest["stacks"])}, indent=2))


if __name__ == "__main__":
    main()
