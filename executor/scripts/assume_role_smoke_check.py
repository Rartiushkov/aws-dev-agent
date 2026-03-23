import argparse
import json
import os
import sys

import boto3


def parse_args():
    parser = argparse.ArgumentParser(description="Assume a target AWS role and list key recreated resources.")
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--region", default="us-east-1")
    return parser.parse_args()


def main():
    args = parse_args()
    session = boto3.session.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
        region_name=args.region,
    )
    sts = session.client("sts")
    assumed = sts.assume_role(RoleArn=args.role_arn, RoleSessionName="roman-art-local-check")["Credentials"]
    assumed_session = boto3.session.Session(
        aws_access_key_id=assumed["AccessKeyId"],
        aws_secret_access_key=assumed["SecretAccessKey"],
        aws_session_token=assumed["SessionToken"],
        region_name=args.region,
    )

    identity = assumed_session.client("sts").get_caller_identity()
    lambda_names = [
        item["FunctionName"]
        for item in assumed_session.client("lambda").list_functions().get("Functions", [])
    ]
    table_names = assumed_session.client("dynamodb").list_tables().get("TableNames", [])
    queue_urls = assumed_session.client("sqs").list_queues().get("QueueUrls", [])

    print(json.dumps({
        "identity": identity,
        "lambda_functions": lambda_names,
        "dynamodb_tables": table_names,
        "sqs_queues": queue_urls,
    }, indent=2))


if __name__ == "__main__":
    main()
