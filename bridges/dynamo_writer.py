import boto3

ddb = boto3.client("dynamodb", region_name="us-east-1")

TABLE = "agent_fix_plans"


def send_plan(plan):

    ddb.put_item(
        TableName=TABLE,
        Item={
            "pk": {"S": plan["pk"]},
            "sk": {"S": plan["sk"]},
            "status": {"S": plan["status"]},
            "type": {"S": plan["type"]},
            "createdAt": {"N": str(plan["createdAt"])},
            "plan": {
                "M": {
                    "actions": {
                        "L": [
                            {
                                "M": {
                                    "type": {"S": "IAM_POLICY_UPDATE"},
                                    "role": {"S": plan["plan"]["target"]["role"]},
                                    "action": {"S": plan["plan"]["policy"]["Action"]},
                                    "resource": {"S": plan["plan"]["policy"]["Resource"]},
                                }
                            }
                        ]
                    }
                }
            },
        },
    )

    print("✅ PLAN SENT TO AWS:", plan["pk"])