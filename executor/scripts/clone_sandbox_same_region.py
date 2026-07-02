import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.transfer_common import deployment_dir_path, inventory_dir_path, load_transfer_config, resolve_client_slug


def parse_args():
    parser = argparse.ArgumentParser(description="Safely clone one sandbox into another in the same AWS region.")
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--client-slug", default="")
    parser.add_argument("--skip-discovery", action="store_true")
    return parser.parse_args()


def run_step(command):
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.stdout:
        print(completed.stdout)
    if completed.stderr:
        print(completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(command)}")
    return completed


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def inventory_paths(source_env, client_slug=""):
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    return {
        "snapshot": inventory_dir / "source_snapshot.json",
        "summary": inventory_dir / "summary.json",
        "graph": inventory_dir / "dependency_graph.json",
    }


def deployment_paths(target_env, client_slug=""):
    deployment_dir = deployment_dir_path(target_env, client_slug=client_slug)
    return {
        "dir": deployment_dir,
        "plan": deployment_dir / "deployment_plan.json",
        "manifest": deployment_dir / "deployment_manifest.json",
        "validation": deployment_dir / "validation_report.json",
    }


def assert_same_region_clone_is_enabled(config):
    if not config.get("overrides", {}).get("allow_same_scope", False):
        raise RuntimeError("Config must set overrides.allow_same_scope=true for same-region sandbox clone")


def assert_safe_clone_plan(plan, source_env, target_env, region):
    if plan.get("mode") != "read-only-assessment":
        raise RuntimeError("Expected read-only assessment plan")
    if plan.get("source_env") != source_env:
        raise RuntimeError("Plan source_env mismatch")
    if plan.get("target_env") != target_env:
        raise RuntimeError("Plan target_env mismatch")
    if plan.get("region") != region:
        raise RuntimeError("Plan region mismatch")

    checks = {item.get("name"): item for item in plan.get("preflight_checks", [])}
    scope = checks.get("scope", {})
    details = scope.get("details", {})
    if not (details.get("same_account") and details.get("same_region")):
        raise RuntimeError("Expected same-account same-region plan for sandbox clone")

    hardcoded_refs = checks.get("hardcoded-source-account-references", {})
    if hardcoded_refs.get("status") not in {"ok", "warning"}:
        raise RuntimeError("Unexpected hardcoded reference check state")

    manual_review = plan.get("manual_review", {})
    if any(int(manual_review.get(key, 0) or 0) > 0 for key in manual_review):
        raise RuntimeError("Plan includes manual-review resources; aborting automatic sandbox clone")


def main():
    args = parse_args()
    config = load_transfer_config(args.config)
    assert_same_region_clone_is_enabled(config)
    client_slug = resolve_client_slug(args.client_slug, config, source_env=args.source_env, target_env=args.target_env)

    if not args.skip_discovery:
        run_step([
            "python",
            "executor/scripts/discover_aws_environment.py",
            "--source-env",
            args.source_env,
            "--region",
            args.region,
            "--config",
            args.config,
            "--client-slug",
            client_slug,
        ])

    paths = inventory_paths(args.source_env, client_slug=client_slug)
    for required_path in paths.values():
        if not required_path.exists():
            raise FileNotFoundError(f"Missing discovery artifact: {required_path}")

    run_step([
        "python",
        "executor/scripts/deploy_discovered_env.py",
        "--source-env",
        args.source_env,
        "--target-env",
        args.target_env,
        "--region",
        args.region,
        "--config",
        args.config,
        "--read-only-plan",
        "--client-slug",
        client_slug,
    ])

    deploy_paths = deployment_paths(args.target_env, client_slug=client_slug)
    plan = load_json(deploy_paths["plan"])
    assert_safe_clone_plan(plan, args.source_env, args.target_env, args.region)

    run_step([
        "python",
        "executor/scripts/deploy_discovered_env.py",
        "--source-env",
        args.source_env,
        "--target-env",
        args.target_env,
        "--region",
        args.region,
        "--config",
        args.config,
        "--client-slug",
        client_slug,
    ])

    run_step([
        "python",
        "executor/scripts/validate_deployed_env.py",
        "--target-env",
        args.target_env,
        "--region",
        args.region,
        "--config",
        args.config,
        "--client-slug",
        client_slug,
    ])

    manifest = load_json(deploy_paths["manifest"])
    validation = load_json(deploy_paths["validation"])

    print(json.dumps({
        "status": "ok",
        "source_env": args.source_env,
        "target_env": args.target_env,
        "region": args.region,
        "plan_path": str(deploy_paths["plan"]),
        "manifest_path": str(deploy_paths["manifest"]),
        "validation_path": str(deploy_paths["validation"]),
        "failed_resource_count": sum(len(items) for items in manifest.get("failures", {}).values()),
        "issues_found": validation.get("issues_found", True),
    }, indent=2))


if __name__ == "__main__":
    main()
