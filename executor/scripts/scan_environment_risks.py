import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from executor.scripts.transfer_common import inventory_dir_path, resolve_client_slug


SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze a discovered AWS snapshot for migration risks.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def resolve_inventory_dir(source_env, region, client_slug=""):
    base_dir = inventory_dir_path(client_slug=client_slug).parent
    direct = base_dir / source_env
    regional = base_dir / f"{source_env}-{region}"

    def has_signal_resources(path):
        summary_path = path / "summary.json"
        if not summary_path.exists():
            return False
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if summary.get("has_signal_resources"):
            return True
        return bool(summary.get("signal_resource_count", 0))

    if has_signal_resources(direct):
        return direct
    if has_signal_resources(regional):
        return regional
    if direct.exists():
        return direct
    return regional


def _is_public_cidr(cidr):
    return cidr in {"0.0.0.0/0", "::/0"}


def _add_finding(findings, severity, category, title, resource_id="", details=""):
    findings.append({
        "severity": severity,
        "category": category,
        "title": title,
        "resource_id": resource_id,
        "details": details,
    })


def analyze_snapshot(snapshot):
    findings = []

    role_arns = {role.get("Arn"): role for role in snapshot.get("iam_roles", [])}
    for fn in snapshot.get("lambda_functions", []):
        if not fn.get("VpcConfig", {}).get("SubnetIds"):
            _add_finding(
                findings,
                "medium",
                "lambda-networking",
                "Lambda function is not attached to a VPC",
                fn.get("FunctionArn", ""),
                f"{fn.get('FunctionName', '')} may need manual networking validation if it depends on private resources.",
            )
        role = role_arns.get(fn.get("Role"))
        if not role:
            continue
        for policy in role.get("InlinePolicies", []):
            document = policy.get("PolicyDocument", {})
            for statement in document.get("Statement", []):
                actions = statement.get("Action", [])
                resources = statement.get("Resource", [])
                if isinstance(actions, str):
                    actions = [actions]
                if isinstance(resources, str):
                    resources = [resources]
                if "*" in resources:
                    _add_finding(
                        findings,
                        "medium",
                        "iam-policy",
                        "IAM inline policy contains wildcard resource",
                        role.get("Arn", ""),
                        f"Policy {policy.get('PolicyName', '')} on {role.get('RoleName', '')} uses Resource '*'.",
                    )
                if any(action == "*" or action.endswith(":*") for action in actions):
                    _add_finding(
                        findings,
                        "high",
                        "iam-policy",
                        "IAM inline policy contains broad actions",
                        role.get("Arn", ""),
                        f"Policy {policy.get('PolicyName', '')} on {role.get('RoleName', '')} uses broad actions.",
                    )

    for sg in snapshot.get("security_groups", []):
        for permission in sg.get("IpPermissions", []):
            for ip_range in permission.get("IpRanges", []):
                if _is_public_cidr(ip_range.get("CidrIp")):
                    port = permission.get("FromPort", "all")
                    _add_finding(
                        findings,
                        "high",
                        "network-exposure",
                        "Security group allows ingress from the public internet",
                        sg.get("GroupId", ""),
                        f"{sg.get('GroupName', sg.get('GroupId', ''))} exposes port {port} to 0.0.0.0/0.",
                    )

    for route_table in snapshot.get("route_tables", []):
        for route in route_table.get("Routes", []):
            gateway = route.get("GatewayId", "")
            if gateway.startswith("igw-") and _is_public_cidr(route.get("DestinationCidrBlock")):
                _add_finding(
                    findings,
                    "medium",
                    "network-topology",
                    "Route table contains a public internet route",
                    route_table.get("RouteTableId", ""),
                    f"Route table {route_table.get('RouteTableId', '')} has 0.0.0.0/0 via {gateway}.",
                )

    for db in snapshot.get("rds", {}).get("instances", []):
        identifier = db.get("DBInstanceIdentifier", "")
        if db.get("PubliclyAccessible"):
            _add_finding(
                findings,
                "high",
                "rds-exposure",
                "RDS instance is publicly accessible",
                identifier,
                f"{identifier} is marked PubliclyAccessible=true.",
            )
        if db.get("StorageEncrypted") is False:
            _add_finding(
                findings,
                "medium",
                "rds-encryption",
                "RDS instance storage is not encrypted",
                identifier,
                f"{identifier} has StorageEncrypted=false.",
            )
        if not db.get("BackupRetentionPeriod"):
            _add_finding(
                findings,
                "medium",
                "rds-backups",
                "RDS instance has no backup retention",
                identifier,
                f"{identifier} has BackupRetentionPeriod=0.",
            )

    for cluster in snapshot.get("rds", {}).get("clusters", []):
        identifier = cluster.get("DBClusterIdentifier", "")
        if cluster.get("StorageEncrypted") is False:
            _add_finding(
                findings,
                "medium",
                "rds-encryption",
                "RDS cluster storage is not encrypted",
                identifier,
                f"{identifier} has StorageEncrypted=false.",
            )

    if snapshot.get("cloudformation_stacks"):
        _add_finding(
            findings,
            "medium",
            "iac-review",
            "CloudFormation stacks require manual migration review",
            "",
            f"{len(snapshot.get('cloudformation_stacks', []))} CloudFormation stack(s) were discovered.",
        )

    if snapshot.get("s3_buckets"):
        _add_finding(
            findings,
            "low",
            "s3-review",
            "S3 buckets require manual data migration review",
            "",
            f"{len(snapshot.get('s3_buckets', []))} bucket(s) were discovered.",
        )

    if snapshot.get("git_repositories"):
        _add_finding(
            findings,
            "low",
            "git-backup",
            "Git repositories were discovered for backup planning",
            "",
            f"{len(snapshot.get('git_repositories', []))} repository reference(s) were found.",
        )

    findings.sort(key=lambda item: SEVERITY_ORDER.get(item["severity"], 0), reverse=True)
    summary = {
        "high": sum(1 for item in findings if item["severity"] == "high"),
        "medium": sum(1 for item in findings if item["severity"] == "medium"),
        "low": sum(1 for item in findings if item["severity"] == "low"),
    }
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "account_id": snapshot.get("account_id", ""),
        "region": snapshot.get("region", ""),
        "summary": summary,
        "findings": findings,
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env)
    base_dir = resolve_inventory_dir(source_env, args.region or "us-east-1", client_slug)
    snapshot_path = base_dir / "source_snapshot.json"
    report_path = base_dir / "risk_report.json"

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    report = analyze_snapshot(snapshot)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "report_path": str(report_path),
        "summary": report["summary"],
        "finding_count": len(report["findings"]),
    }, indent=2))


if __name__ == "__main__":
    main()
