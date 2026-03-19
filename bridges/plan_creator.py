from bridges.dynamo_writer import send_plan
from executor.safe_mode import safe_fallback
import json
import re
import time


def extract_lambda_name(raw_goal):

    match = re.search(r"named\s+(.+?)(?:\s+test lambda|\s+for integration|\s+integration|$)", raw_goal, re.IGNORECASE)

    if not match:
        return "hello-world-fn"

    candidate = match.group(1).strip().lower()
    candidate = re.sub(r"[^a-z0-9-_]+", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-_")

    if not candidate:
        return "hello-world-fn"

    return candidate[:64]


def create_plan(goal):

    raw_goal = goal.strip()
    goal = raw_goal.lower()
    function_name = extract_lambda_name(raw_goal)
    wants_lambda_test = "test lambda" in goal or "integration" in goal

    if "create iam role" in goal and "lambda" in goal:

        role_name = "lambda-basic-role"

        return [
            {
                "type": "command",
                "cmd": "powershell -Command \"Set-Content -Path trust-policy.json -Value '{\\\"Version\\\":\\\"2012-10-17\\\",\\\"Statement\\\":[{\\\"Effect\\\":\\\"Allow\\\",\\\"Principal\\\":{\\\"Service\\\":\\\"lambda.amazonaws.com\\\"},\\\"Action\\\":\\\"sts:AssumeRole\\\"}]}'\""
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
                "cmd": "del trust-policy.json 2>nul"
            }
        ]

    if "create lambda" in goal:

        role_arn = "arn:aws:iam::027087672282:role/lambda-basic-role"

        plan = [
            {
                "type": "command",
                "cmd": "powershell -Command \"Set-Content -Path lambda_function.py -Value @('def handler(event, context):','    return {\\\"statusCode\\\": 200, \\\"body\\\": \\\"Hello World\\\"}')\""
            },
            {
                "type": "command",
                "cmd": "powershell Compress-Archive -Path lambda_function.py -DestinationPath function.zip -Force"
            },
            {
                "type": "command",
                "cmd": f"aws lambda create-function --function-name {function_name} --runtime python3.11 --role {role_arn} --handler lambda_function.handler --zip-file fileb://function.zip"
            },
            {
                "type": "command",
                "cmd": f"aws lambda update-function-code --function-name {function_name} --zip-file fileb://function.zip"
            },
            {
                "type": "command",
                "cmd": f"aws lambda wait function-updated --function-name {function_name}"
            }
        ]

        if wants_lambda_test:
            plan.extend([
                {
                    "type": "command",
                    "cmd": f"aws lambda invoke --function-name {function_name} response.json"
                },
                {
                    "type": "command",
                    "cmd": "type response.json"
                }
            ])

        return plan

    if "test lambda" in goal or "integration" in goal:
        return [
            {
                "type": "command",
                "cmd": f"aws lambda invoke --function-name {function_name} response.json"
            },
            {
                "type": "command",
                "cmd": "type response.json"
            }
        ]

    if "list roles" in goal:
        return [{"type": "command", "cmd": "aws iam list-roles"}]

    if "list lambda" in goal:
        return [{"type": "command", "cmd": "aws lambda list-functions"}]

    try:
        from bedrock.bedrock_client import BedrockClient

        print("TRYING AI...")
        client = BedrockClient()

        response = client.ask(goal)

        print("RAW AI RESPONSE:", response)

        plan = json.loads(response)

        print("AI PLAN PARSED")

        return plan

    except Exception as e:
        print("AI FAILED:", str(e))

    print("USING DEFAULT TEST PLAN")

    return [
        {
            "type": "command",
            "cmd": "echo running test"
        },
        {
            "type": "command",
            "cmd": "aws lambda list-functions"
        }
    ]


def create_fix_plan(description):

    timestamp = int(time.time())
    plan = {
        "pk": f"fix#{timestamp}",
        "sk": "latest",
        "status": "PENDING",
        "type": "AUTO_FIX",
        "createdAt": timestamp,
        "plan": {
            "target": {
                "role": "unknown"
            },
            "policy": {
                "Action": description[:200],
                "Resource": "*"
            }
        }
    }

    try:
        send_plan(plan)
    except Exception as e:
        print("Failed to send fix plan:", str(e))

    return plan
