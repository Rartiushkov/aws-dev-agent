from bridges.dynamo_writer import send_plan
from executor.safe_mode import safe_fallback
import json
import re
import time


FEATURE_TEST_PATTERNS = {
    "lambda": "test_plan_creator.py",
    "cloudwatch": "test_runner_cloudwatch.py",
    "runner": "test_runner_cloudwatch.py",
    "plan": "test_plan_creator.py",
}

CODEBUILD_PROJECT_NAME = "aws-dev-agent-tests"


def extract_clone_request(raw_goal):
    source_cluster_match = re.search(r"cluster(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
    source_service_match = re.search(r"service(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
    target_env_match = re.search(r"(?:new env|environment|env)(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)
    team_match = re.search(r"team(?:\s+named)?\s+([a-zA-Z0-9-_]+)", raw_goal, re.IGNORECASE)

    return {
        "source_cluster": source_cluster_match.group(1) if source_cluster_match else "",
        "source_service": source_service_match.group(1) if source_service_match else "",
        "target_env": target_env_match.group(1) if target_env_match else "",
        "team": team_match.group(1) if team_match else "",
    }


def autotest_step(goal_text=None):
    goal_text = (goal_text or "").lower()

    for feature, pattern in FEATURE_TEST_PATTERNS.items():
        if feature in goal_text:
            return {
                "type": "command",
                "cmd": f"python -m unittest discover -s . -p \"{pattern}\""
            }

    return {
        "type": "command",
        "cmd": "python -m unittest discover -s . -p \"test_*.py\""
    }


def append_autotest(plan, goal_text=None):
    extended_plan = list(plan)
    extended_plan.append(autotest_step(goal_text))
    return extended_plan


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

        return append_autotest([
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
        ], goal)

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
                "cmd": f"aws lambda wait function-active-v2 --function-name {function_name}"
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

        return append_autotest(plan, goal)

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

    if "run autotest in aws" in goal or "run tests in aws" in goal:
        return [
            {
                "type": "command",
                "cmd": f"aws codebuild start-build --project-name {CODEBUILD_PROJECT_NAME}"
            }
        ]

    if "clone env" in goal or "recreate env" in goal or "new env" in goal and "service" in goal and "cluster" in goal:
        clone_request = extract_clone_request(raw_goal)
        if clone_request["source_cluster"] and clone_request["source_service"] and clone_request["target_env"]:
            command = (
                f"python executor/scripts/clone_ecs_env.py "
                f"--source-cluster {clone_request['source_cluster']} "
                f"--source-service {clone_request['source_service']} "
                f"--target-env {clone_request['target_env']}"
            )
            if clone_request["team"]:
                command += f" --team {clone_request['team']}"
            return append_autotest([{"type": "command", "cmd": command}], goal)

    if "list codebuild projects" in goal or "list codebuild" in goal:
        return [
            {
                "type": "command",
                "cmd": "aws codebuild list-projects"
            }
        ]

    if "autotest" in goal or "run tests" in goal or "run autotest" in goal:
        return [
            autotest_step(goal)
        ]

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
