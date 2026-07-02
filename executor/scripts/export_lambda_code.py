import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.audit_log import append_audit_event
from executor.scripts.deploy_discovered_env import download_lambda_zip, ensure_zip_is_readable
from executor.scripts.transfer_common import (
    config_override,
    inventory_dir_path,
    load_transfer_config,
    resolve_client_slug,
    session_for,
    should_exclude,
)


SENSITIVE_ENV_NAME_PATTERN = re.compile(
    r"(secret|token|password|passwd|pwd|credential|access[_-]?key|private[_-]?key|authorization|auth)",
    re.IGNORECASE,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Export Lambda deployment ZIPs from a discovered AWS inventory.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--config", default="")
    parser.add_argument("--source-role-arn", default="")
    parser.add_argument("--source-external-id", default="")
    parser.add_argument("--inventory-key", default="")
    parser.add_argument("--client-slug", default="")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def safe_filename(value):
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip())
    return cleaned.strip("-_.") or "lambda-function"


def redact_environment(configuration):
    updated = dict(configuration or {})
    environment = updated.get("Environment")
    variables = (environment or {}).get("Variables")
    if not isinstance(variables, dict):
        return updated
    updated_environment = dict(environment)
    updated_environment["Variables"] = {
        key: "[REDACTED]" if SENSITIVE_ENV_NAME_PATTERN.search(str(key)) else value
        for key, value in variables.items()
    }
    updated["Environment"] = updated_environment
    return updated


def lambda_code_metadata(code):
    metadata = dict(code or {})
    metadata.pop("Location", None)
    return metadata


def export_lambda_artifacts(snapshot, lambda_client, output_dir, *, config=None, downloader=download_lambda_zip):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    failed = []

    for fn in snapshot.get("lambda_functions", []):
        function_name = fn.get("FunctionName", "")
        if not function_name:
            continue
        if should_exclude("lambda_functions", function_name, config or {}):
            continue

        function_dir = output_dir / safe_filename(function_name)
        function_dir.mkdir(parents=True, exist_ok=True)
        try:
            details = lambda_client.get_function(FunctionName=function_name)
            configuration = details.get("Configuration", fn)
            code = details.get("Code", {})
            package_type = configuration.get("PackageType") or fn.get("PackageType") or "Zip"

            artifact = {
                "function_name": function_name,
                "function_arn": configuration.get("FunctionArn", fn.get("FunctionArn", "")),
                "package_type": package_type,
                "artifact_dir": str(function_dir),
                "zip_path": "",
                "configuration_path": str(function_dir / "configuration.redacted.json"),
                "code_metadata_path": str(function_dir / "code.json"),
                "status": "ok",
            }

            (function_dir / "configuration.redacted.json").write_text(
                json.dumps(redact_environment(configuration), indent=2, default=str),
                encoding="utf-8",
            )
            (function_dir / "code.json").write_text(
                json.dumps(lambda_code_metadata(code), indent=2, default=str),
                encoding="utf-8",
            )

            if package_type == "Zip":
                location = code.get("Location")
                if not location:
                    raise RuntimeError("Lambda Code.Location is missing; cannot download deployment ZIP")
                downloaded_zip = downloader(location)
                ensure_zip_is_readable(downloaded_zip)
                target_zip = function_dir / "function.zip"
                shutil.copy2(downloaded_zip, target_zip)
                artifact["zip_path"] = str(target_zip)
            else:
                artifact["status"] = "metadata-only"
                artifact["note"] = "Container-image Lambda stores code in ECR; Code metadata was exported instead of a ZIP."

            exported.append(artifact)
        except Exception as exc:
            failed.append({
                "function_name": function_name,
                "artifact_dir": str(function_dir),
                "error": str(exc),
            })

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "account_id": snapshot.get("account_id", ""),
        "region": snapshot.get("region", ""),
        "exported": exported,
        "failed": failed,
        "exported_count": len(exported),
        "failed_count": len(failed),
    }
    manifest_path = output_dir / "lambda_code_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    config = load_transfer_config(args.config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, args.inventory_key, client_slug)
    snapshot_path = inventory_dir / "source_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    source_external_id = args.source_external_id or config_override(config, "source_external_id", "")
    session = session_for(args.region, args.source_role_arn, external_id=source_external_id)
    output_dir = Path(args.output_dir) if args.output_dir else inventory_dir / "lambda_code"
    manifest = export_lambda_artifacts(
        snapshot,
        session.client("lambda"),
        output_dir,
        config=config,
    )

    append_audit_event(
        "export_lambda_code",
        "ok" if not manifest["failed"] else "partial",
        {
            "manifest_path": str(output_dir / "lambda_code_manifest.json"),
            "exported_count": manifest["exported_count"],
            "failed_count": manifest["failed_count"],
        },
        source_env=source_env,
        client_slug=client_slug,
    )
    print(json.dumps({
        "status": "ok" if not manifest["failed"] else "partial",
        "output_dir": str(output_dir),
        "manifest_path": str(output_dir / "lambda_code_manifest.json"),
        "exported_count": manifest["exported_count"],
        "failed_count": manifest["failed_count"],
    }, indent=2))


if __name__ == "__main__":
    main()
