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
    parser = argparse.ArgumentParser(description="Export an IaC blueprint summary from a discovered snapshot.")
    parser.add_argument("--source-env", default="")
    parser.add_argument("--client-slug", default="")
    return parser.parse_args()


def build_iac_blueprint(snapshot):
    terraform = {
        "aws_iam_role": len(snapshot.get("iam_roles", [])),
        "aws_sqs_queue": len(snapshot.get("sqs_queues", [])),
        "aws_sns_topic": len(snapshot.get("sns_topics", [])),
        "aws_lambda_function": len(snapshot.get("lambda_functions", [])),
        "aws_dynamodb_table": len(snapshot.get("dynamodb_tables", [])),
        "aws_api_gateway_rest_api": len(snapshot.get("api_gateways", [])),
        "aws_ecs_cluster": len(snapshot.get("ecs", {}).get("clusters", [])),
        "aws_ecs_service": len(snapshot.get("ecs", {}).get("services", [])),
        "aws_db_instance": len(snapshot.get("rds", {}).get("instances", [])),
        "aws_rds_cluster": len(snapshot.get("rds", {}).get("clusters", [])),
        "aws_instance": len(snapshot.get("ec2_instances", [])),
    }
    cloudformation = {
        "AWS::IAM::Role": len(snapshot.get("iam_roles", [])),
        "AWS::SQS::Queue": len(snapshot.get("sqs_queues", [])),
        "AWS::SNS::Topic": len(snapshot.get("sns_topics", [])),
        "AWS::Lambda::Function": len(snapshot.get("lambda_functions", [])),
        "AWS::DynamoDB::Table": len(snapshot.get("dynamodb_tables", [])),
        "AWS::ApiGateway::RestApi": len(snapshot.get("api_gateways", [])),
        "AWS::ECS::Cluster": len(snapshot.get("ecs", {}).get("clusters", [])),
        "AWS::ECS::Service": len(snapshot.get("ecs", {}).get("services", [])),
        "AWS::RDS::DBInstance": len(snapshot.get("rds", {}).get("instances", [])),
        "AWS::RDS::DBCluster": len(snapshot.get("rds", {}).get("clusters", [])),
        "AWS::EC2::Instance": len(snapshot.get("ec2_instances", [])),
        "AWS::CloudFormation::Stack": len(snapshot.get("cloudformation_stacks", [])),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_env": snapshot.get("source_env", ""),
        "region": snapshot.get("region", ""),
        "terraform_blueprint": terraform,
        "cloudformation_blueprint": cloudformation,
        "terraform_stub_resources": [
            {
                "type": "aws_s3_bucket",
                "name": item.get("Name", "bucket").replace("-", "_"),
                "bucket": item.get("Name", ""),
            }
            for item in snapshot.get("s3_buckets", [])
        ] + [
            {
                "type": "aws_vpc",
                "name": item.get("VpcId", "vpc").replace("-", "_"),
                "cidr_block": item.get("CidrBlock", ""),
            }
            for item in snapshot.get("vpcs", [])
        ] + [
            {
                "type": "aws_db_instance",
                "name": item.get("DBInstanceIdentifier", "db").replace("-", "_"),
                "identifier": item.get("DBInstanceIdentifier", ""),
                "engine": item.get("Engine", ""),
                "instance_class": item.get("DBInstanceClass", ""),
            }
            for item in snapshot.get("rds", {}).get("instances", [])
        ] + [
            {
                "type": "aws_instance",
                "name": item.get("InstanceId", "instance").replace("-", "_"),
                "instance_type": item.get("InstanceType", ""),
                "subnet_id": item.get("SubnetId", ""),
            }
            for item in snapshot.get("ec2_instances", [])
        ],
        "cloudformation_stub_resources": {
            item.get("Name", f"Bucket{index + 1}"): {
                "Type": "AWS::S3::Bucket",
                "Properties": {"BucketName": item.get("Name", "")},
            }
            for index, item in enumerate(snapshot.get("s3_buckets", []))
        } | {
            (item.get("DBInstanceIdentifier", f"DbInstance{index + 1}")): {
                "Type": "AWS::RDS::DBInstance",
                "Properties": {
                    "DBInstanceIdentifier": item.get("DBInstanceIdentifier", ""),
                    "Engine": item.get("Engine", ""),
                    "DBInstanceClass": item.get("DBInstanceClass", ""),
                },
            }
            for index, item in enumerate(snapshot.get("rds", {}).get("instances", []))
        } | {
            (next((tag.get("Value", "") for tag in item.get("Tags", []) if tag.get("Key") == "Name"), "") or item.get("InstanceId", f"Ec2Instance{index + 1}")): {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "InstanceType": item.get("InstanceType", ""),
                    "SubnetId": item.get("SubnetId", ""),
                },
            }
            for index, item in enumerate(snapshot.get("ec2_instances", []))
        },
        "notes": [
            "This export now includes starter resource stubs for S3, VPC, RDS, and EC2 resources.",
            "Use it as the first step toward real importable Terraform/CloudFormation coverage.",
        ],
    }


def main():
    args = parse_args()
    source_env = args.source_env or "full-account-scan"
    client_slug = resolve_client_slug(args.client_slug, source_env=source_env)
    inventory_dir = inventory_dir_path(source_env, client_slug=client_slug)
    snapshot = json.loads((inventory_dir / "source_snapshot.json").read_text(encoding="utf-8"))
    blueprint = build_iac_blueprint(snapshot)
    blueprint_path = inventory_dir / "iac_blueprint.json"
    blueprint_path.write_text(json.dumps(blueprint, indent=2), encoding="utf-8")
    append_audit_event("export_iac_blueprint", "ok", {"blueprint_path": str(blueprint_path)}, source_env=source_env, client_slug=client_slug)
    print(json.dumps({"status": "ok", "blueprint_path": str(blueprint_path)}, indent=2))


if __name__ == "__main__":
    main()
