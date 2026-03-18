from executor.safe_mode import safe_fallback
import json


def create_plan(goal):

    goal = goal.lower().strip()

    # ===== RULES =====

    if goal.startswith("delete role"):
        parts = goal.split()
        role_name = parts[-1]

        return [
            {"type": "action", "action": "DELETE_ALL_POLICIES", "role_name": role_name},
            {"type": "command", "cmd": f"aws iam delete-role --role-name {role_name}"}
        ]

    if goal.startswith("create role"):
        parts = goal.split()
        role_name = parts[-1]

        return [
            {"type": "action", "action": "CREATE_ROLE", "role_name": role_name}
        ]

    if "check roles" in goal or "list roles" in goal:
        return [
            {"type": "command", "cmd": "aws iam list-roles"}
        ]

    if "check lambda" in goal or "list lambda" in goal:
        return [
            {"type": "command", "cmd": "aws lambda list-functions"}
        ]
    if "create iam role" in goal and "lambda" in goal:

        role_name = "lambda-basic-role"

        return [
        {
            "type": "command",
            "cmd": "echo {\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"lambda.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]} > trust-policy.json"
        },
        {
            "type": "command",
            "cmd": f"aws iam create-role --role-name {role_name} --assume-role-policy-document file://trust-policy.json"
        },
        {
            "type": "command",
            "cmd": f"aws iam attach-role-policy --role-name {role_name} --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        },
        {
            "type": "command",
            "cmd": "rm trust-policy.json || del trust-policy.json"
        }
    ]
    # ===== AI =====

    try:
        from bedrock.bedrock_client import BedrockClient

        print("🧠 TRYING AI...")
        client = BedrockClient()

        response = client.ask(goal)

        print("🧠 RAW AI RESPONSE:", response)

        plan = json.loads(response)

        print("✅ AI PLAN PARSED")

        return plan

    except Exception as e:
        print("⚠️ AI FAILED:", str(e))

    # ===== SAFE MODE =====

    print("🛡️ USING SAFE MODE")

    return safe_fallback(goal)